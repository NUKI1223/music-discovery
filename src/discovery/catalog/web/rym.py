"""Offline RYM release parsing; live DOM compatibility has not been verified.

Selector provenance and authored-fixture limitations are recorded in
tests/catalog/web/fixtures/rym/README.md. No network access happens here.
"""

import json
import math
import re
from datetime import datetime
from urllib.parse import urlsplit

from pydantic import ValidationError

from .common import ParsedRelease, ParseError, document, exact_count, html_digest, validate_url

PARSER_VERSION = "rym-offline-v1"


def parse_rym(html: str, *, url: str, observed_at: datetime) -> ParsedRelease:
    """Parse one saved release page, preserving unknown/rounded counts as null."""
    soup = document(html, url=url, source="rym")
    canonical = validate_url(url, "rym")
    page_link = soup.select_one('link[rel="canonical"]')
    if page_link is not None and page_link.get("href"):
        if validate_url(page_link["href"], "rym").rstrip("/") != canonical.rstrip("/"):
            raise ParseError("IDENTITY_MISMATCH", "Page canonical URL differs from supplied release")
    warnings = ["Live RYM DOM compatibility has not been verified."]
    evidence = {}
    albums = _album_nodes(soup)
    if len(albums) > 1:
        raise ParseError("AMBIGUOUS_RELEASE", "Multiple MusicAlbum entities; cannot choose a release")
    album = albums[0] if albums else {}
    if album.get("url"):
        if not isinstance(album["url"], str) or (
            validate_url(album["url"], "rym").rstrip("/") != canonical.rstrip("/")
        ):
            raise ParseError("IDENTITY_MISMATCH", "MusicAlbum URL differs from the supplied release")

    tables = soup.select("table.album_info_outer")
    if len(tables) > 1:
        raise ParseError("AMBIGUOUS_RELEASE", "Multiple release information tables")
    rows = _rows(tables[0] if tables else None)
    title_nodes = soup.select('h1.album_title, .album_title[itemprop="name"], '
                             'h1[itemprop="name"]')
    if len(title_nodes) > 1:
        raise ParseError("AMBIGUOUS_RELEASE", "Multiple release title elements")
    title_node = title_nodes[0] if title_nodes else None
    title = _text(title_node)
    structured_title = album.get("name")
    if title and isinstance(structured_title, str) and title != " ".join(structured_title.split()):
        raise ParseError("IDENTITY_MISMATCH", "DOM and MusicAlbum titles disagree")
    if title:
        evidence["title"] = "release title (.album_title / itemprop=name)"
    elif isinstance(structured_title, str):
        title = structured_title.strip()
        evidence["title"] = "MusicAlbum.name"

    artist_row = rows.get("artist")
    artists = []
    if artist_row is not None:
        artists = list(dict.fromkeys(_text(n) for n in artist_row.select('a.artist, a[href^="/artist/"]')
                                     if _text(n)))
        if artists:
            evidence["artists"] = "album_info_outer Artist row artist links"
    structured_artists = _names(album.get("byArtist"))
    if artists and structured_artists and set(artists) != set(structured_artists):
        raise ParseError("IDENTITY_MISMATCH", "DOM and MusicAlbum artists disagree")
    if not artists and structured_artists:
        artists = structured_artists
        evidence["artists"] = "MusicAlbum.byArtist"
    if not title or not artists or not (tables or album):
        raise ParseError("UNSUPPORTED_LAYOUT", "Cannot identify a release title and artist credits")

    released = rows.get("released")
    year = _year(_text(released)) if released else _year(album.get("datePublished"))
    if year is not None:
        evidence["year"] = "Released row" if released else "MusicAlbum.datePublished"
    genres = _genre_links(rows.get("genres"), "release_pri_genres")
    secondary = _genre_links(rows.get("genres"), "release_sec_genres")
    if genres:
        evidence["genres"] = "Genres row .release_pri_genres a"
    elif album.get("genre"):
        genres = _names(album["genre"])
        evidence["genres"] = "MusicAlbum.genre (primary/secondary distinction unavailable)"
        warnings.append("JSON-LD genres do not specify primary versus secondary status.")
    if secondary:
        evidence["secondary_genres"] = "Genres row .release_sec_genres a"
    descriptor_node = rows.get("descriptors")
    descriptors = []
    if descriptor_node is not None:
        node = descriptor_node.select_one(".release_pri_descriptors")
        descriptors = [x.strip() for x in _text(node).split(",") if x.strip()]
        if descriptors:
            evidence["descriptors"] = "Descriptors row .release_pri_descriptors"

    rating, count_text = None, None
    rating_row = rows.get("rym rating")
    if rating_row is not None:
        node = rating_row.select_one(".avg_rating")
        rating = _rating(_text(node)) if node is not None else None
        if rating is not None:
            evidence["rating"] = "RYM Rating row .avg_rating"
        scale = rating_row.select_one(".max_rating")
        if scale is not None and _text(scale).strip("/ ") not in ("5", "5.0", "5.00"):
            raise ParseError("INVALID_RATING", "RYM rating scale must be five")
        count_node = rating_row.select_one('[itemprop="ratingCount"], .num_ratings b')
        if count_node is not None:
            count_text = _text(count_node)
            evidence["rating_count"] = "RYM Rating row ratingCount / .num_ratings b"
    elif isinstance(album.get("aggregateRating"), dict):
        aggregate = album["aggregateRating"]
        if aggregate.get("@type", "AggregateRating") != "AggregateRating":
            raise ParseError("INVALID_RATING", "Album aggregate is not an AggregateRating")
        if str(aggregate.get("bestRating", 5)) not in ("5", "5.0", "5.00"):
            raise ParseError("INVALID_RATING", "RYM aggregate rating scale must be five")
        rating = _rating(aggregate.get("ratingValue"))
        if rating is not None:
            evidence["rating"] = "MusicAlbum.aggregateRating.ratingValue"
        if "ratingCount" in aggregate:
            value = aggregate["ratingCount"]
            count_text = str(value) if value is not None else None
            evidence["rating_count"] = "MusicAlbum.aggregateRating.ratingCount"
        elif "reviewCount" in aggregate:
            warnings.append("reviewCount is not ratingCount; the rating count remains unknown.")
    count = exact_count(count_text)
    if count_text is not None and count is None:
        warnings.append("Rating count is abbreviated or not an exact integer; kept as unknown.")
    if count == 0 and rating is not None:
        raise ParseError("INVALID_RATING", "A mean rating with zero ratings is inconsistent")
    try:
        return ParsedRelease(
            source="rym", source_id=urlsplit(canonical).path.rstrip("/"), url=canonical,
            title=title, artists=artists, year=year, genres=genres, secondary_genres=secondary,
            descriptors=descriptors, rating=rating, rating_scale=5, rating_count=count,
            rating_count_text=count_text, observed_at=observed_at, html_sha256=html_digest(html),
            parser_version=PARSER_VERSION, field_evidence=evidence, warnings=warnings,
        )
    except ValidationError as exc:
        raise ParseError("INVALID_FIELDS", "Parsed release contains invalid field values") from exc


def _rating(value) -> float | None:
    if value is None or str(value).strip().casefold() in ("", "-", "—", "n/a", "not rated"):
        return None
    if isinstance(value, bool):
        raise ParseError("INVALID_RATING", "Boolean is not a rating")
    try:
        number = float(value)
    except (ValueError, TypeError) as exc:
        raise ParseError("INVALID_RATING", "Invalid RYM rating") from exc
    if not math.isfinite(number) or not 0 <= number <= 5:
        raise ParseError("INVALID_RATING", "RYM rating must be within zero to five")
    return number


def _text(node) -> str:
    if node is None:
        return ""
    return " ".join(str(node.get("content") or node.get_text(" ", strip=True)).split())


def _names(value) -> list[str]:
    values = value if isinstance(value, list) else [value]
    result = []
    for item in values:
        name = item.get("name") if isinstance(item, dict) else item
        if isinstance(name, str) and name.strip():
            result.append(" ".join(name.split()))
    return list(dict.fromkeys(result))


def _album_nodes(soup) -> list[dict]:
    """Inspect only roots/@graph/mainEntity, never tracks or recommendations."""
    result = []

    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            kind = value.get("@type", [])
            kinds = kind if isinstance(kind, list) else [kind]
            if any(str(x).rsplit("/", 1)[-1] == "MusicAlbum" for x in kinds):
                result.append(value)
            for key in ("@graph", "mainEntity"):
                if key in value:
                    visit(value[key])

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            visit(json.loads(script.string or script.get_text()))
        except (ValueError, TypeError, RecursionError):
            continue
    return result


def _rows(table) -> dict:
    rows = {}
    if table is not None:
        for row in table.select("tr"):
            heading = row.select_one("th.info_hdr")
            if heading is not None:
                key = _text(heading).rstrip(":").casefold()
                if key in rows and key in {"artist", "released", "rym rating", "genres"}:
                    raise ParseError("AMBIGUOUS_RELEASE", "Duplicate release information rows")
                rows.setdefault(key, row)
    return rows


def _genre_links(row, css_class: str) -> list[str]:
    if row is None:
        return []
    return list(dict.fromkeys(_text(node) for node in row.select(f".{css_class} a")
                             if _text(node)))


def _year(value) -> int | None:
    match = re.search(r"(?<!\d)(1[0-9]{3}|20[0-9]{2})(?!\d)", str(value or ""))
    return int(match[1]) if match else None
