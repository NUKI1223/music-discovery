"""Shared page classification tests use authored markup only."""

import json

import pytest

from discovery.catalog.web.common import ParseError, document

URLS = {"rym": "https://rateyourmusic.com/release/album/fixture/title/",
        "aoty": "https://www.albumoftheyear.org/album/123-fixture.php"}
MARKERS = {
    "rym": '<h1 class="album_title">Release</h1><table class="album_info_outer"></table>',
    "aoty": '<h1 class="albumTitle">Release</h1>',
}


@pytest.mark.parametrize("source", ["rym", "aoty"])
@pytest.mark.parametrize("name", ["Access Denied", "Captcha", "Sign In", "Log In", "Security Check"])
def test_release_name_in_branded_title_is_not_a_challenge(source, name):
    marker = MARKERS[source].replace("Release", name)
    html = f"<html><title>Artist - {name} - Music site</title>{marker}</html>"
    assert document(html, url=URLS[source], source=source) is not None


@pytest.mark.parametrize("source", ["rym", "aoty"])
def test_schema_album_can_disambiguate_branded_title(source):
    album = {"@type": "MusicAlbum", "name": "Access Denied", "byArtist": {"name": "Artist"}}
    html = '<title>Artist - Access Denied - Music site</title><script type="application/ld+json">'
    html += json.dumps(album) + "</script>"
    assert document(html, url=URLS[source], source=source) is not None


@pytest.mark.parametrize("title", ["Just a moment...", "Access denied", "Captcha", "Sign In",
    "Log in", "403 Forbidden", "429 Too Many Requests"])
def test_bare_block_title_stops_even_with_release_markers(title):
    html = f"<title>{title}</title>{MARKERS['rym']}"
    with pytest.raises(ParseError) as exc:
        document(html, url=URLS["rym"], source="rym")
    assert exc.value.code == "BLOCKED_PAGE"


@pytest.mark.parametrize("marker", ['<form id="challenge-form"></form>',
    '<form id="captcha-form"></form>', '<div id="cf-chl-widget"></div>',
    '<script src="/cdn-cgi/challenge-platform/test.js"></script>'])
def test_structural_challenges_always_stop(marker):
    html = f"<title>Normal release</title>{MARKERS['rym']}{marker}"
    with pytest.raises(ParseError) as exc:
        document(html, url=URLS["rym"], source="rym")
    assert exc.value.code == "BLOCKED_PAGE"


@pytest.mark.parametrize("html", ["<title>Access denied - RYM</title>",
    "<title>Log in | Album of the Year</title>", "<h1>403 Forbidden</h1>"])
def test_branded_or_heading_only_block_without_release_structure_stops(html):
    with pytest.raises(ParseError) as exc:
        document(html, url=URLS["rym"], source="rym")
    assert exc.value.code == "BLOCKED_PAGE"
