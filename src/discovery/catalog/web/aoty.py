"""Offline AOTY album HTML parser; no network access or personal labels.

DOM selector evidence: edideaur/AOTY-api, commit
f32a042f5c146f447fefaf98cd43f569a9cc97da, src/scrapers/album.ts.
Current live-page compatibility is unverified: the robots probe was blocked.
Authored test fixtures are explicitly synthetic, not source snapshots.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime
from html import unescape
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from .common import ParsedRelease, ParseError, document, exact_count, html_digest, validate_url

PARSER_VERSION = "aoty-offline-v3"
_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)\Z")


def _text(node: Tag | None) -> str:
    return " ".join(node.get_text(" ", strip=True).split()) if node else ""


def _names(value: object) -> list[str]:
    names = []
    for item in value if isinstance(value, list) else [value]:
        name = item.get("name") if isinstance(item, dict) else item
        if isinstance(name, str) and name.strip():
            names.append(" ".join(unescape(name).split()))
    return list(dict.fromkeys(names))


def _source_id(url: str) -> str:
    match = re.match(r"/album/(\d+)", urlsplit(validate_url(url, "aoty")).path)
    assert match is not None  # validate_url checked the release path.
    return match[1]


def _identity_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", unescape(value)).casefold().split())


def _check_dom_identity(soup: BeautifulSoup, title: str, artists: list[str],
                        evidence: dict[str, str]) -> None:
    """Cross-check available page headings, excluding album/review/track cards.

    These checks do not provide fallback identity when MusicAlbum is absent.
    In particular, albumTitle/artistTitle inside albumBlock are recommendations.
    """
    excluded = {"albumBlock", "albumReviewRow", "userReview", "trackListTable"}

    def headings(selector: str) -> list[Tag]:
        return [node for node in soup.select(selector)
                if not any(parent.name in {"aside", "nav"}
                           or excluded.intersection(parent.get("class", []))
                           for parent in node.parents if isinstance(parent, Tag))]

    titles = [_text(node) for node in headings(".albumTitle") if _text(node)]
    if any(_identity_text(value) != _identity_text(title) for value in titles):
        raise ParseError("IDENTITY_CONFLICT", "Album heading conflicts with MusicAlbum identity")
    if titles:
        evidence["title"] += "; checked page .albumTitle outside album/review cards"
    artist_nodes = headings(".artistTitle")
    names = []
    for node in artist_nodes:
        links = node.select('a[href*="/artist/"]')
        names.extend(_text(link) for link in links if _text(link))
        if not links and _text(node):
            names.append(_text(node))
    if names and {_identity_text(name) for name in names} != {_identity_text(name) for name in artists}:
        raise ParseError("IDENTITY_CONFLICT", "Artist heading conflicts with MusicAlbum identity")
    if names:
        evidence["artists"] += "; checked page .artistTitle outside album/review cards"


def _album(soup: BeautifulSoup, url: str, warnings: list[str]) -> dict:
    """Select MusicAlbum roots/graph/mainEntity, never tracks or recommendations."""
    candidates = []

    def visit(value: object) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            kinds = value.get("@type", [])
            kinds = kinds if isinstance(kinds, list) else [kinds]
            if any(str(kind).rsplit("/", 1)[-1] == "MusicAlbum" for kind in kinds):
                candidates.append(value)
            for key in ("@graph", "mainEntity"):
                if key in value:
                    visit(value[key])

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            visit(json.loads(script.string or script.get_text()))
        except (ValueError, TypeError, RecursionError):
            warnings.append("MALFORMED_JSON_LD_IGNORED")
    selected, unidentified = [], []
    for candidate in candidates:
        link = candidate.get("url")
        if isinstance(link, str):
            try:
                if _source_id(urljoin(url, link)) == _source_id(url):
                    selected.append(candidate)
            except ParseError:
                continue
        else:
            unidentified.append(candidate)
    if not selected and len(candidates) == 1 and len(unidentified) == 1:
        selected = unidentified
    unique = {json.dumps(item, sort_keys=True): item for item in selected}
    if len(unique) != 1:
        code = "AMBIGUOUS_RELEASE" if candidates else "MISSING_IDENTITY"
        raise ParseError(code, "Expected one identifiable MusicAlbum JSON-LD object")
    return next(iter(unique.values()))


def _score(soup: BeautifulSoup, audience: str, evidence: dict[str, str]) -> float | None:
    selector = f".album{audience}ScoreBox .album{audience}Score"
    scores = []
    for node in soup.select(selector):
        anchor = node.select_one("a")
        raw = str(anchor.get("title", "")).strip() if anchor else ""
        method = " a[title]"
        if not raw or not _NUMBER.fullmatch(raw):
            raw = _text(anchor or node)
            method = " text"
        if raw.upper() in {"", "NR", "N/A", "-", "—"}:
            continue
        if not _NUMBER.fullmatch(raw) or not 0 <= float(raw) <= 100:
            raise ParseError("INVALID_RATING", f"Invalid {audience.lower()} score on 0..100 scale")
        scores.append(float(raw))
        key = "rating" if audience == "User" else "critic_rating"
        evidence[key] = selector + method
    if len(set(scores)) > 1:
        raise ParseError("AMBIGUOUS_RATING", "Conflicting album aggregate score blocks")
    return scores[0] if scores else None


def _count(soup: BeautifulSoup, audience: str, warnings: list[str],
           evidence: dict[str, str]) -> tuple[int | None, str | None]:
    selector = f".album{audience}ScoreBox .numReviews"
    texts = list(dict.fromkeys(_text(node) for node in soup.select(selector) if _text(node)))
    if not texts:
        return None, None
    raw = " | ".join(texts)
    # AOTY's CSS calls both counters numReviews. The visible noun determines
    # whether this is an actual rating count; a user review count is not one.
    noun = r"ratings?" if audience == "User" else r"(?:ratings?|reviews?)"
    pattern = rf"(?<![\w.,/+−-])([+−-]?\d[\d,.\s\u00a0\u202f]*[KkMmBb]?\+?)\s+(?:user\s+)?{noun}\b"
    values = []
    for text in texts:
        matches = list(re.finditer(pattern, text, re.IGNORECASE))
        for match in matches:
            count = exact_count(match[1].strip())
            if re.search(r"\b(?:about|approx(?:imately)?|over|more than)\b|[~≈]", text, re.IGNORECASE):
                count = None
            values.append(count)
    field = "rating_count" if audience == "User" else "critic_rating_count"
    if not values:
        warnings.append(f"{audience.upper()}_COUNT_SEMANTICS_UNVERIFIED")
        return None, raw
    if None in values:
        warnings.append(f"{audience.upper()}_COUNT_NOT_EXACT")
        return None, raw
    if len(set(values)) != 1:
        warnings.append(f"{audience.upper()}_COUNT_CONFLICT")
        return None, raw
    evidence[field] = f"{selector} visible {'ratings' if audience == 'User' else 'reviews/ratings'} integer"
    return values[0], raw


def _genres(soup: BeautifulSoup, album: dict, evidence: dict[str, str]) -> tuple[list[str], list[str]]:
    primary = _names(album.get("genre"))
    secondary = []
    if primary:
        evidence["genres"] = "MusicAlbum.genre"
    selector = ".albumTopBox.info .detailRow a[href*='/genre/']"
    for node in soup.select(selector):
        name = _text(node)
        if not name:
            continue
        if "secondary" in node.get("class", []) or node.select_one(".secondary"):
            secondary.append(name)
        else:
            primary.append(name)
    secondary = list(dict.fromkeys(secondary))
    primary = list(dict.fromkeys(name for name in primary if name not in secondary))
    if secondary:
        evidence["secondary_genres"] = selector + " .secondary"
    if primary and "genres" not in evidence:
        evidence["genres"] = selector + " excluding .secondary"
    return primary, secondary


def _vibes(soup: BeautifulSoup, url: str, evidence: dict[str, str]) -> list[str]:
    """Only linked Vibe annotations in the main album details, never review prose.

    The reference scraper supplies .detailRow.vibes .vibe a. The link path is
    independently visible on AOTY's album page. Hidden labels already present
    in the HTML are retained; '+12' is a UI control, not an additional label.
    """
    selector = ".albumTopBox.info .detailRow.vibes .vibe a[href]"
    excluded = {"albumBlock", "albumReviewRow", "userReview", "trackListTable",
                "userReviewText", "reviewText", "recommendedAlbums", "recommendations"}
    values = {}
    for node in soup.select(selector):
        if any(parent.name in {"aside", "nav", "header", "footer"}
               or excluded.intersection(parent.get("class", []))
               for parent in node.parents if isinstance(parent, Tag)):
            continue
        try:
            link = urlsplit(urljoin(url, str(node["href"])))
        except ValueError:
            continue  # Malformed unrelated links do not invalidate album facts.
        if (link.scheme != "https" or link.netloc not in {"www.albumoftheyear.org", "albumoftheyear.org"}
                or not re.fullmatch(r"/all/releases/vibe/[^/]+/?", link.path)):
            continue
        name = _text(node)
        if not name or re.fullmatch(r"\+\s*\d+", name):
            continue
        values.setdefault(name.casefold(), name)
    if values:
        evidence["vibes"] = selector + " same-source /all/releases/vibe/ links; excludes review/card/navigation ancestors"
    return list(values.values())


def parse_aoty(html: str, *, url: str, observed_at: datetime) -> ParsedRelease:
    """Parse one saved album page. Generic JSON-LD aggregateRating is never a user score."""
    soup = document(html, url=url, source="aoty")
    url = validate_url(url, "aoty")
    source_id = _source_id(url)
    for canonical in soup.select('link[rel="canonical"][href]'):
        if _source_id(urljoin(url, str(canonical["href"]))) != source_id:
            raise ParseError("IDENTITY_CONFLICT", "Canonical album ID differs from the supplied URL")
    warnings = ["LIVE_DOM_COMPATIBILITY_UNVERIFIED"]
    album = _album(soup, url, warnings)
    title = album.get("name")
    artists = _names(album.get("byArtist"))
    if not isinstance(title, str) or not title.strip() or not artists:
        raise ParseError("MISSING_IDENTITY", "MusicAlbum must contain a title and artist names")
    evidence = {"title": "MusicAlbum.name", "artists": "MusicAlbum.byArtist", "source_id": "URL /album/<id>"}
    _check_dom_identity(soup, title, artists, evidence)
    year = None
    date = album.get("datePublished")
    match = re.fullmatch(r"(\d{4})(?:-\d{2}(?:-\d{2})?)?(?:T.*)?", str(date or ""))
    if match and 1800 <= int(match[1]) <= 2200:
        year = int(match[1])
        evidence["year"] = "MusicAlbum.datePublished"
    elif date is not None:
        warnings.append("RELEASE_YEAR_UNPARSED")
    genres, secondary = _genres(soup, album, evidence)
    vibes = _vibes(soup, url, evidence)
    rating = _score(soup, "User", evidence)
    critic_rating = _score(soup, "Critic", evidence)
    count, count_text = _count(soup, "User", warnings, evidence)
    critic_count, _ = _count(soup, "Critic", warnings, evidence)
    if count == 0 and rating is not None:
        rating = None
        evidence.pop("rating", None)
        warnings.append("USER_SCORE_WITH_ZERO_COUNT_IGNORED")
    if critic_count == 0 and critic_rating is not None:
        critic_rating = None
        evidence.pop("critic_rating", None)
        warnings.append("CRITIC_SCORE_WITH_ZERO_COUNT_IGNORED")
    if album.get("aggregateRating") is not None:
        warnings.append("UNSCOPED_JSON_LD_AGGREGATE_IGNORED")
    if rating is None:
        warnings.append("USER_RATING_MISSING")
    if count is None:
        warnings.append("EXACT_USER_RATING_COUNT_MISSING")
    return ParsedRelease(
        source="aoty", source_id=source_id, url=url, title=" ".join(unescape(title).split()),
        artists=artists, year=year, genres=genres, secondary_genres=secondary, vibes=vibes,
        rating=rating, rating_scale=100, rating_count=count, rating_count_text=count_text,
        critic_rating=critic_rating, critic_rating_scale=100, critic_rating_count=critic_count,
        observed_at=observed_at, html_sha256=html_digest(html), parser_version=PARSER_VERSION,
        field_evidence=evidence, warnings=list(dict.fromkeys(warnings)),
    )
