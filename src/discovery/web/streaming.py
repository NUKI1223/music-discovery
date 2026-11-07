"""Independent album link discovery. No keys, track/search URLs or inferred IDs.

MusicBrainz supplies explicit release relationships; public album.link pages expand
an identity-matched store album. The retired Songlink API is deliberately not used.
"""

import json
import re
import sqlite3
import time
import unicodedata
from urllib.parse import parse_qs, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from .schemas import ListeningLink

USER_AGENT = 'MusicDiscoveryLocal/0.1 (personal local album-link discovery)'
MB = 'https://musicbrainz.org/ws/2/'
MBID = r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}'
FAILURES = (httpx.HTTPError, ValueError, TypeError, AttributeError, KeyError)


def normalized(value: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFKC', value).casefold() if c.isalnum())


def album_name(value: str) -> str:
    return normalized(re.sub(r'\s+[-–—]\s+(EP|Single)$', '', value, flags=re.I))


def artist_name(value: str) -> str:
    name = normalized(value)
    return {'ye': 'kanyewest'}.get(name, name)


def matches_credit(value: str, artists: list[str]) -> bool:
    expected = artist_name(artists[0]) if len(artists) == 1 else artist_name(' & '.join(artists))
    return bool(expected) and artist_name(value) == expected


# Strict URL families shared by all discovery sources. Unknown domains are never fetched.
PLATFORMS = (
    ('Spotify', {'open.spotify.com'}, r'/(?:intl-[a-z]+/)?album/[A-Za-z0-9]+/?'),
    ('Apple Music', {'music.apple.com', 'itunes.apple.com'}, r'/[a-z]{2}/album/(?:[^/]+/)?(?:id)?\d+/?'),
    ('Deezer', {'www.deezer.com', 'deezer.com'}, r'/(?:[a-z]{2}/)?album/\d+/?'),
    ('YouTube Music', {'music.youtube.com', 'www.youtube.com', 'youtube.com'}, r'/playlist'),
    ('Яндекс Музыка', {'music.yandex.ru', 'music.yandex.kz', 'music.yandex.com'}, r'/album/\d+/?'),
    ('TIDAL', {'tidal.com', 'www.tidal.com', 'listen.tidal.com'}, r'/(?:browse/)?album/\d+/?'),
    ('Amazon Music', {'music.amazon.com', 'music.amazon.co.uk', 'music.amazon.de', 'music.amazon.co.jp'}, r'/albums/[A-Za-z0-9]+/?'),
    ('Qobuz', {'www.qobuz.com', 'qobuz.com'}, r'/[a-z]{2}-[a-z]{2}/album/[^/]+/[A-Za-z0-9]+/?'),
    ('Qobuz', {'play.qobuz.com', 'open.qobuz.com'}, r'/album/[A-Za-z0-9]+/?'),
    ('Pandora', {'www.pandora.com', 'pandora.com'}, r'/AL:\d+/?'),
    ('Napster', {'play.napster.com'}, r'/album/alb\.\d+/?'),
    ('Boomplay', {'www.boomplay.com', 'boomplay.com'}, r'/albums/\d+/?'),
    ('Anghami', {'play.anghami.com'}, r'/album/\d+/?'),
    ('Beatport', {'www.beatport.com', 'beatport.com'}, r'/release/[^/]+/\d+/?'),
)


def direct_link(value: object, source: str) -> ListeningLink | None:
    if not isinstance(value, str) or any(ord(c) < 33 for c in value):
        return None
    try:
        url = urlsplit(value)
        if url.scheme != 'https' or url.username or url.password or url.port is not None or url.fragment:
            return None
        service = None
        for label, hosts, pattern in PLATFORMS:
            if url.hostname in hosts and re.fullmatch(pattern, url.path):
                service = label
                break
        if (url.hostname or '').endswith('.bandcamp.com') and re.fullmatch(r'/album/[^/]+/?', url.path):
            service = 'Bandcamp'
        query = parse_qs(url.query, keep_blank_values=True)
        if service == 'Apple Music' and 'i' in query:
            return None
        if service == 'Amazon Music' and any(k in query for k in ('trackAsin', 'track')):
            return None
        if service == 'YouTube Music' and (not re.fullmatch(r'OLAK5uy_[\w-]+', query.get('list', [''])[0])
                                          or 'v' in query):
            return None
        return ListeningLink(service=service, url=value, source_url=source) if service else None
    except ValueError:
        return None


def album_url(value: object, service: str) -> str | None:
    link = direct_link(value, '')
    return link.url if link and link.service == service else None


def merge_links(*groups: list[ListeningLink]) -> list[ListeningLink]:
    by_service = {}
    for group in groups:
        for link in group:
            checked = direct_link(link.url, link.source_url)
            if checked:
                by_service.setdefault(checked.service, checked)
    order = ['Spotify', 'Apple Music', 'YouTube Music', 'Deezer', 'Bandcamp', 'Яндекс Музыка',
             'TIDAL', 'Amazon Music', 'Qobuz', 'Pandora', 'Napster', 'Boomplay', 'Anghami', 'Beatport']
    return sorted(by_service.values(), key=lambda link: order.index(link.service))


def credit_matches(row, title, artists):
    credits = row.get('artist-credit', [])
    names = [artist_name(c.get('name') or c.get('artist', {}).get('name', ''))
             for c in credits if isinstance(c, dict)]
    return (bool(album_name(title)) and album_name(row.get('title', '')) == album_name(title)
            and sorted(names) == sorted(artist_name(a) for a in artists) and bool(names))


def relationships(row, source):
    result = []
    for relation in row.get('relations', []):
        if relation.get('ended') or relation.get('type') not in {'streaming', 'free streaming', 'purchase for download'}:
            continue
        link = direct_link(relation.get('url', {}).get('resource'), source)
        if link:
            result.append(link)
    return result


def parse_album_link(html, seed, title, artists):
    script = BeautifulSoup(html, 'html.parser').find('script', id='__NEXT_DATA__')
    if script is None:
        raise ValueError('Missing page data')
    data = json.loads(script.get_text())['props']['pageProps']['pageData']
    entity = data['entityData']
    expected = f"{seed[0]}|album|{seed[1]}"
    if (data.get('entityUniqueId') != expected or entity.get('type') != 'album'
            or str(entity.get('id')) != seed[1] or entity.get('provider') != seed[0]
            or album_name(entity.get('title', '')) != album_name(title)
            or not matches_credit(entity.get('artistName', ''), artists)):
        raise ValueError('Album identity mismatch')
    result = []
    source = 'https://album.link/' + seed[2]
    for section in data.get('sections', []):
        if not section.get('show') or section.get('sectionId') not in {'section|auto|links|listen', 'section|auto|links|buy'}:
            continue
        for item in section.get('links', []):
            # Hidden placeholders, custom links and song mappings are not album evidence.
            if (not item.get('show') or not str(item.get('linkId', '')).startswith('link|auto|')
                    or '|album|' not in str(item.get('uniqueId', ''))):
                continue
            link = direct_link(item.get('url'), source)
            if link:
                result.append(link)
    return merge_links(result)


class StreamingDiscovery:
    def __init__(self, path):
        self.path = path
        with sqlite3.connect(path) as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS media_rate (host TEXT PRIMARY KEY, next REAL)')
            conn.execute('CREATE TABLE IF NOT EXISTS media_source_cache (key TEXT PRIMARY KEY, expires REAL, payload TEXT)')

    def wait(self, host, interval):
        # Shared between threads, cache instances and local processes.
        with sqlite3.connect(self.path, timeout=30) as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT next FROM media_rate WHERE host=?', (host,)).fetchone()
            slot = max(time.time(), row[0] if row else 0)
            conn.execute('INSERT OR REPLACE INTO media_rate VALUES (?,?)', (host, slot + interval))
        time.sleep(max(0, slot - time.time()))

    def get(self, url, *, params=None, interval=1.1):
        self.wait(urlsplit(url).hostname, interval)
        response = httpx.get(url, params=params, headers={'User-Agent': USER_AGENT},
                             timeout=8, follow_redirects=False)
        response.raise_for_status()
        if len(response.content) > 2_000_000:
            raise ValueError('Response too large')
        return response

    def musicbrainz(self, title, artists, year=None):
        found = []
        if not title or not artists:
            return [], 'not_found'
        try:
            def quote(s):
                return '"' + re.sub(r'([+\-!(){}\[\]^"~*?:\\/|&])', r'\\\1', s) + '"'
            query = 'releasegroup:' + quote(title) + ' AND ' + ' AND '.join('artist:' + quote(a) for a in artists)
            payload = self.get(MB + 'release-group/', params={'query': query, 'fmt': 'json', 'limit': 25}).json()
            groups = [g for g in payload.get('release-groups', []) if credit_matches(g, title, artists)
                      and (not year or str(g.get('first-release-date', '')).startswith(str(year)))
                      and re.fullmatch(MBID, g.get('id', ''))]
            if len(groups) != 1:
                return [], 'ambiguous' if groups else 'not_found'
            group = groups[0]
            gid = group['id']
            detail = self.get(MB + 'release-group/' + gid, params={'inc': 'artists+url-rels', 'fmt': 'json'}).json()
            if detail.get('id') != gid or not credit_matches(detail, title, artists):
                return [], 'ambiguous'
            found.extend(relationships(detail, 'https://musicbrainz.org/release-group/' + gid))
            # At most 300 editions, with an explicit partial result for very large groups.
            for offset in range(0, 300, 100):
                page = self.get(MB + 'release/', params={'release-group': gid, 'inc': 'url-rels+artist-credits',
                                                       'fmt': 'json', 'limit': 100, 'offset': offset}).json()
                for release in page.get('releases', []):
                    if (not credit_matches(release, title, artists) or release.get('status') != 'Official'
                            or not re.fullmatch(MBID, release.get('id', ''))
                            or re.search(r'deluxe|remaster|instrumental|karaoke|clean|bonus', release.get('disambiguation', ''), re.I)):
                        continue
                    found.extend(relationships(release, 'https://musicbrainz.org/release/' + release['id']))
                if offset + 100 >= page.get('release-count', 0):
                    return merge_links(found), 'found' if found else 'not_found'
            return merge_links(found), 'partial'
        except FAILURES:
            return merge_links(found), 'unavailable'

    def album_link(self, links, title, artists):
        seed = None
        for service, provider, short in [('Deezer', 'deezer', 'd'), ('Apple Music', 'itunes', 'i'), ('Spotify', 'spotify', 's')]:
            match = next((link for link in links if link.service == service), None)
            if match:
                ident = urlsplit(match.url).path.rstrip('/').split('/')[-1].removeprefix('id')
                seed = (provider, ident, short + '/' + ident)
                break
        if not seed:
            return [], 'not_found'
        try:
            robots_key = 'https://album.link/robots.txt'
            with sqlite3.connect(self.path) as conn:
                row = conn.execute('SELECT payload FROM media_source_cache WHERE key=? AND expires>?', (robots_key, time.time())).fetchone()
            if row:
                robots_text = row[0]
            else:
                response = self.get(robots_key, interval=3.1)
                robots_text = response.text
                if 'user-agent:' not in robots_text.lower():
                    raise ValueError('Invalid robots')
                with sqlite3.connect(self.path) as conn:
                    conn.execute('INSERT OR REPLACE INTO media_source_cache VALUES (?,?,?)', (robots_key, time.time()+86400, robots_text))
            url = 'https://album.link/' + seed[2]
            robots = RobotFileParser(robots_key)
            robots.parse(robots_text.splitlines())
            if not robots.can_fetch(USER_AGENT, url):
                return [], 'blocked'
            response = self.get(url, interval=max(3.1, robots.crawl_delay(USER_AGENT) or 0))
            found = parse_album_link(response.text, seed, title, artists)
            return found, 'found' if found else 'not_found'
        except FAILURES:
            return [], 'unavailable'
