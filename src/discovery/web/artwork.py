"""Optional, cached store artwork; never changes recommendation snapshots."""

import hashlib
import json
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from urllib.parse import urlsplit

import anyio
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from discovery.catalog.web.fetch import FetchBlocked

from .aoty_artwork import fetch_artwork
from .deps import get_user
from .schemas import Artwork
from .streaming import (
    FAILURES,
    StreamingDiscovery,
    album_name,
    album_url,
    direct_link,
    matches_credit,
    merge_links,
)

router = APIRouter(prefix="/api", tags=["artwork"])


def cover_url(value: object, service: str) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        url = urlsplit(value)
        if url.scheme != "https" or url.username or url.password or url.port is not None:
            return None
        if service == "Apple Music" and (url.hostname or "").endswith(".mzstatic.com"):
            return re.sub(r"/100x100bb\.", "/600x600bb.", value)
        if (service == "Deezer" and (url.hostname or "").endswith(".dzcdn.net")
                and url.path.startswith("/images/cover/")):
            return value
    except ValueError:
        pass
    return None


def unique_album(matches: list[Artwork]) -> Artwork:
    pages = {(urlsplit(m.listening_url).hostname, urlsplit(m.listening_url).path.rstrip("/")): m
             for m in matches}
    # Exact names can still point to several editions/entities. Do not choose
    # the first provider result as proof of the intended release.
    return next(iter(pages.values())) if len(pages) == 1 else Artwork()


def match_deezer(rows: list, title: str, artists: list[str]) -> Artwork:
    matches = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("artist"), dict):
            continue
        if (not album_name(title) or album_name(str(row.get("title", ""))) != album_name(title)
                or not matches_credit(str(row["artist"].get("name", "")), artists)):
            continue
        link = album_url(row.get("link"), "Deezer")
        if link:
            matches.append(Artwork(artwork=cover_url(row.get("cover_big"), "Deezer"),
                                   listening_url=link, listening_service="Deezer"))
    return unique_album(matches)


def match_artwork(rows: list, title: str, artists: list[str]) -> Artwork:
    matches = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if (not album_name(title) or album_name(str(row.get("collectionName", ""))) != album_name(title)
                or not matches_credit(str(row.get("artistName", "")), artists)):
            continue
        link = album_url(row.get("collectionViewUrl"), "Apple Music")
        if link:
            matches.append(Artwork(artwork=cover_url(row.get("artworkUrl100"), "Apple Music"),
                                   listening_url=link, listening_service="Apple Music"))
    return unique_album(matches)


class ArtworkCache:
    def __init__(self, path):
        self.path = path
        self.key_locks = [threading.Lock() for _ in range(64)]
        self.streaming = StreamingDiscovery(path)
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS artwork (key TEXT PRIMARY KEY, expires REAL, payload TEXT)")

    def store(self, service, title, artists):
        try:
            if service == 'Apple Music':
                response = self.streaming.get('https://itunes.apple.com/search', params={
                    'term': f"{' '.join(artists)} {title}", 'entity': 'album', 'limit': 25,
                }, interval=3.1)
                result = match_artwork(response.json().get('results', []), title, artists)
            else:
                payload = self.streaming.get('https://api.deezer.com/search/album', params={
                    'q': f"{' '.join(artists)} {title}", 'limit': 25,
                }).json()
                if payload.get('error'):
                    return Artwork(), 'unavailable'
                result = match_deezer(payload.get('data', []), title, artists)
            return result, 'found' if result.listening_url else 'not_found'
        except FAILURES:
            return Artwork(), 'unavailable'

    def resolve(self, title, artists, aoty_url=None, year=None):
        key = json.dumps(["album-pages-v4", title, artists, aoty_url, year], ensure_ascii=False)
        # A cached album must not queue behind unrelated network searches.
        with sqlite3.connect(self.path) as conn:
            row = conn.execute("SELECT expires,payload FROM artwork WHERE key=?", (key,)).fetchone()
            if row and row[0] > time.time():
                return Artwork.model_validate_json(row[1])
        with self.key_locks[int(hashlib.sha256(key.encode()).hexdigest(), 16) % 64]:
            with sqlite3.connect(self.path) as conn:
                row = conn.execute("SELECT expires,payload FROM artwork WHERE key=?", (key,)).fetchone()
                if row and row[0] > time.time():
                    return Artwork.model_validate_json(row[1])
            aoty_status = None
            aoty_image = None
            if aoty_url:
                with sqlite3.connect(self.path) as conn:
                    blocked = conn.execute("SELECT payload FROM artwork WHERE key=? AND expires>?",
                                           ('aoty-access', time.time())).fetchone()
                if blocked:
                    aoty_status = json.loads(blocked[0])
                else:
                    try:
                        image = fetch_artwork(aoty_url, title, artists)
                        aoty_status = 'found' if image else 'not_found'
                        aoty_image = image
                    except (FetchBlocked, httpx.HTTPError, ValueError, OSError) as exc:
                        aoty_status = getattr(exc, 'code', 'unavailable')
                        # Host-wide cooldown prevents repeated blocked requests for each card.
                        if aoty_status not in {'INVALID_URL'}:
                            with sqlite3.connect(self.path) as conn:
                                conn.execute("INSERT OR REPLACE INTO artwork VALUES (?,?,?)",
                                             ('aoty-access', time.time() + 300, json.dumps(aoty_status)))
            # Independent providers: an Apple hit never suppresses Deezer or other services.
            with ThreadPoolExecutor(max_workers=3) as pool:
                apple_future = pool.submit(self.store, 'Apple Music', title, artists)
                deezer_future = pool.submit(self.store, 'Deezer', title, artists)
                mb_future = pool.submit(self.streaming.musicbrainz, title, artists, year)
                apple, apple_status = apple_future.result()
                deezer, deezer_status = deezer_future.result()
                mb_links, mb_status = mb_future.result()
            links = [direct_link(r.listening_url, r.listening_url) for r in (apple, deezer) if r.listening_url]
            links = merge_links(links, mb_links)
            expanded, expanded_status = self.streaming.album_link(links, title, artists)
            links = merge_links(links, expanded)
            # Keep the legacy single-link fields for older clients and cover attribution.
            result = apple if apple.listening_url else deezer
            result.listening_links = links
            result.link_status = {'apple': apple_status, 'deezer': deezer_status,
                                  'musicbrainz': mb_status, 'album_link': expanded_status}
            if not result.listening_url and links:
                result.listening_url, result.listening_service = links[0].url, links[0].service
            ttl = 7 * 86400 if links else 86400
            if any(status in {'unavailable', 'partial', 'blocked'} for status in result.link_status.values()):
                ttl = min(ttl, 300)  # A partial outage must not hide a platform for a week.
            if aoty_image:
                result.artwork = aoty_image
                result.source_url = aoty_url
            result.aoty_status = aoty_status
            if not result.artwork and aoty_status and aoty_status not in {'found', 'not_found'}:
                ttl = min(ttl, 300)
            with sqlite3.connect(self.path) as conn:
                conn.execute("INSERT OR REPLACE INTO artwork VALUES (?,?,?)",
                             (key, time.time() + ttl, result.model_dump_json()))
            return result


@router.get("/releases/{release_id}/artwork", response_model=Artwork, dependencies=[Depends(get_user)])
async def artwork(release_id: str, request: Request):
    catalog = request.app.state.catalog
    release = next((r for r in catalog.releases if r.id == release_id), None)
    if release is None:
        raise HTTPException(404, "Релиз не найден.")
    names = {a.id: a.name for a in catalog.artists}
    return await anyio.to_thread.run_sync(partial(
        request.app.state.artwork_cache.resolve, release.title,
        [names[a] for a in release.artist_ids if a in names], release.links.get("aoty"), release.year),
        limiter=request.app.state.artwork_limiter)
