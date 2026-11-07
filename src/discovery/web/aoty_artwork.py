"""Artwork from the exact catalogue AOTY page, without guessed CDN paths."""

import re
from tempfile import TemporaryDirectory
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from discovery.catalog.web.aoty import _album
from discovery.catalog.web.common import ParseError
from discovery.catalog.web.fetch import FetchBlocked, fetch_html, validate_url


def parse_artwork(html: str, url: str, title: str, artists: list[str]) -> str | None:
    from .streaming import normalized

    validate_url(url)
    soup = BeautifulSoup(html, "html.parser")
    canonical = soup.select_one('link[rel="canonical"]')
    if canonical and urljoin(url, str(canonical.get('href', ''))) != url:
        raise ValueError("AOTY page identity mismatch")
    candidates = []
    try:
        album = _album(soup, url, [])
    except ParseError:
        album = None
    if album:
        if normalized(str(album.get('name', ''))) != normalized(title):
            raise ValueError("AOTY album title mismatch")
        image = album.get('image')
        for value in image if isinstance(image, list) else [image]:
            if isinstance(value, dict):
                value = value.get('contentUrl') or value.get('url')
            candidates.append(value)
    else:
        # Never accept a recommendation tile or generic site social image.
        heading = soup.select_one('meta[property="og:title"]')
        text = normalized(str(heading.get('content', ''))) if heading else ''
        if not canonical or not normalized(title) or normalized(title) not in text or not any(
                normalized(artist) and normalized(artist) in text for artist in artists):
            raise ValueError("AOTY page identity unavailable")
    for node in soup.select('meta[property="og:image"]'):
        candidates.append(node.get('content'))
    for node in soup.select('.albumCover img'):
        candidates.append(node.get('src'))
    for value in candidates:
        if not isinstance(value, str):
            continue
        image = urljoin(url, value)
        try:
            parts = urlsplit(image)
            if (parts.scheme == 'https' and not parts.username and not parts.password
                    and parts.port is None
                    and (parts.hostname == 'albumoftheyear.org'
                         or (parts.hostname or '').endswith('.albumoftheyear.org'))
                    and re.match(r'^/(?:\d+x/)?album/', parts.path)):
                return image
        except ValueError:
            continue
    return None


def fetch_artwork(url: str, title: str, artists: list[str]) -> str | None:
    if urlsplit(url).hostname != 'www.albumoftheyear.org':
        raise FetchBlocked('INVALID_URL', 'Expected AOTY album page')
    with TemporaryDirectory(prefix='discovery-aoty-') as directory:
        capture = fetch_html(url, directory)
        return parse_artwork(capture.html_path.read_text(), url, title, artists)
