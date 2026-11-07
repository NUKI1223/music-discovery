"""Unmatched source facts; they are never silently promoted to canonical releases."""

import hashlib
import json
import re
from datetime import datetime
from typing import Literal, Self
from urllib.parse import urlsplit, urlunsplit

from bs4 import BeautifulSoup
from pydantic import Field, model_serializer, model_validator

from discovery.contracts import Contract


class ParseError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def validate_url(url: str, source: str) -> str:
    parts = urlsplit(url)
    hosts = {"rym": {"rateyourmusic.com", "www.rateyourmusic.com"},
             "aoty": {"albumoftheyear.org", "www.albumoftheyear.org"}}
    if (source not in hosts or parts.scheme != "https" or parts.hostname not in hosts[source]
        or parts.username or parts.password or parts.port not in (None, 443)):
        raise ParseError("INVALID_URL", "Expected an HTTPS release URL on the selected source")
    pattern = (r"/release/(?:album|ep|mixtape)/[^/]+/[^/]+/?" if source == "rym"
               else r"/album/\d+[^/]*\.php")
    if not re.fullmatch(pattern, parts.path) or parts.query:
        raise ParseError("INVALID_URL", "Expected a release detail page, not a chart or user page")
    hostname = "rateyourmusic.com" if source == "rym" else "www.albumoftheyear.org"
    return urlunsplit(("https", hostname, parts.path, "", ""))


def html_digest(html: str) -> str:
    return hashlib.sha256(html.encode("utf-8")).hexdigest()


def document(html: str, *, url: str, source: str) -> BeautifulSoup:
    validate_url(url, source)
    if not html.strip() or len(html.encode("utf-8")) > 5_000_000:
        raise ParseError("INVALID_HTML", "HTML is empty or exceeds the 5 MB limit")
    soup = BeautifulSoup(html, "html.parser")
    if page_is_blocked(soup, source):
        raise ParseError("BLOCKED_PAGE", "Input is a challenge, login, or access-denied page")
    return soup


def has_release_markers(soup: BeautifulSoup, source: str) -> bool:
    """Structural hints for page classification, not a successful identity parse."""
    return bool(_release_titles(soup, source))


def _release_titles(soup: BeautifulSoup, source: str) -> list[str]:
    titles = []
    if source == "rym" and soup.select_one("h1.album_title") and soup.select_one("table.album_info_outer"):
        titles.extend(node.get_text(" ", strip=True) for node in soup.select("h1.album_title"))
    if source == "aoty" and soup.select_one(".albumTitle"):
        titles.extend(node.get_text(" ", strip=True) for node in soup.select(".albumTitle"))

    def album(value, depth=0):
        if depth > 8:
            return False
        if isinstance(value, list):
            return any(album(item, depth + 1) for item in value)
        if isinstance(value, dict):
            kinds = value.get("@type", [])
            kinds = kinds if isinstance(kinds, list) else [kinds]
            if any(str(kind).rsplit("/", 1)[-1] == "MusicAlbum" for kind in kinds):
                if isinstance(value.get("name"), str) and value.get("byArtist"):
                    titles.append(value["name"])
                return False
            return any(album(value.get(key), depth + 1) for key in ("@graph", "mainEntity"))
        return False

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            album(json.loads(script.string or script.get_text()))
        except (ValueError, TypeError, RecursionError):
            continue
    return [" ".join(title.casefold().split()) for title in titles if title.strip()]


def page_is_blocked(soup: BeautifulSoup, source: str) -> bool:
    """Do not confuse an album named 'Access Denied' with a challenge page.

    Explicit challenge elements and bare status titles remain blocking even if
    other release markup is present. Ambiguous branded titles require absence
    of release structure before classification as a block.
    """
    if soup.select_one('#challenge-form, #captcha-form, [id^="cf-chl-"], '
                       'script[src*="/cdn-cgi/challenge-platform/"], '
                       'iframe[src*="challenges.cloudflare.com"]'):
        return True
    blocked = ("just a moment", "access denied", "attention required", "verify you are human",
               "security check", "rate limit", "captcha", "sign in", "log in", "login",
               "403 forbidden", "429 too many requests", "404 not found", "service unavailable")
    title = " ".join(soup.title.get_text(" ", strip=True).casefold().split()) if soup.title else ""
    if title.strip(" .!?:…") in blocked:
        return True
    release_titles = _release_titles(soup, source)
    title_markers = [marker for marker in blocked if marker in title]
    if title_markers:
        # The suspicious words must belong to the actual release name, not a
        # login banner surrounding an unrelated album left in the document.
        return not any(name in title and any(marker in name for marker in title_markers)
                       for name in release_titles)
    if not release_titles:
        headings = " ".join(node.get_text(" ", strip=True).casefold() for node in soup.select("h1"))
        return any(marker in headings for marker in blocked)
    return False


def exact_count(text: str | None) -> int | None:
    """Accept complete integers; abbreviated counts such as 25.3K stay unknown."""
    if text is None:
        return None
    text = text.strip()
    if not re.fullmatch(r"(?:\d+|\d{1,3}(?:[, \u00a0\u202f]\d{3})+)", text):
        return None
    return int(re.sub(r"[, \u00a0\u202f]", "", text))


class ParsedRelease(Contract):
    source: Literal["rym", "aoty"]
    source_id: str = Field(min_length=1)
    url: str
    title: str = Field(min_length=1)
    artists: list[str] = Field(min_length=1)
    year: int | None = Field(default=None, ge=1800, le=2200)
    genres: list[str] = Field(default_factory=list)
    secondary_genres: list[str] = Field(default_factory=list)
    descriptors: list[str] = Field(default_factory=list)
    vibes: list[str] = Field(default_factory=list)
    rating: float | None = None
    rating_scale: float
    rating_count: int | None = Field(default=None, ge=0)
    rating_count_text: str | None = None
    critic_rating: float | None = None
    critic_rating_scale: float | None = Field(default=None, gt=0)
    critic_rating_count: int | None = Field(default=None, ge=0)
    observed_at: datetime
    html_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    parser_version: str = Field(min_length=1)
    field_evidence: dict[str, str] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def serialize_compatible(self, handler):
        payload = handler(self)
        if not payload.get("vibes"):
            payload.pop("vibes", None)
        return payload

    @model_validator(mode="after")
    def valid_source_facts(self) -> Self:
        validate_url(self.url, self.source)
        if not self.title.strip() or any(not artist.strip() for artist in self.artists):
            raise ValueError("Title and artists cannot be blank")
        if self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must include a timezone")
        expected_scale = 5 if self.source == "rym" else 100
        if self.rating_scale != expected_scale:
            raise ValueError("Incorrect source rating scale")
        if self.rating is not None and not 0 <= self.rating <= self.rating_scale:
            raise ValueError("Rating is outside the original source scale")
        if self.critic_rating is not None and (
            self.critic_rating_scale is None or not 0 <= self.critic_rating <= self.critic_rating_scale
        ):
            raise ValueError("Critic rating requires its own valid scale")
        return self
