import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

from discovery.catalog.web import fetch

RYM = "https://rateyourmusic.com/release/album/test-artist/test-album/"
AOTY = "https://www.albumoftheyear.org/album/123-test-artist-test-album.php"
ALLOW = b"User-agent: *\nDisallow:\n"
PAGE = (b'<html><head><title>Test album</title></head><body>'
        b'<h1 class="album_title">Album</h1><table class="album_info_outer"></table></body></html>')


@pytest.fixture(autouse=True)
def local_rate_clock(tmp_path, monkeypatch):
    clock = {"now": 1000.0}

    def sleep(seconds):
        clock["now"] += seconds

    monkeypatch.setattr(fetch, "_RATE_PATH", tmp_path / "rate.json")
    monkeypatch.setattr(fetch, "time", SimpleNamespace(
        time=lambda: clock["now"], monotonic=lambda: clock["now"], sleep=sleep,
    ))
    return clock


@pytest.mark.parametrize("url", [
    "http://rateyourmusic.com/release/album/a/b/", "https://localhost/album/a.php",
    "https://www.albumoftheyear.org.evil.test/album/1-a.php",
    "https://user:password@rateyourmusic.com/release/album/a/b/",
    "https://rateyourmusic.com:443/release/album/a/b/",
    "https://rateyourmusic.com/charts/", "https://rateyourmusic.com/release/album/a/../b/",
    "https://rateyourmusic.com/release/album/a/%2e%2e/",
    RYM + "?redirect=https://evil.test", RYM + "#fragment", RYM + "\n",
    "https://www.albumoftheyear.org/account/", "https://www.rateyourmusic.com/release/album/a/b/",
])
def test_rejects_unapproved_destination_before_any_http(url, tmp_path):
    calls = []
    with httpx.Client(transport=httpx.MockTransport(lambda request: calls.append(request))) as client:
        with pytest.raises(fetch.FetchBlocked) as error:
            fetch.fetch_html(url, tmp_path / "out", client=client)
    assert error.value.code == "INVALID_URL"
    assert not calls and not (tmp_path / "out").exists()


@pytest.mark.parametrize("url,html", [(RYM, PAGE), (AOTY, PAGE.replace(b"album_title", b"albumTitle"))])
def test_success_raw_bytes_and_safe_provenance_without_client_credentials(url, html, tmp_path, local_rate_clock):
    calls = []

    def transport(request):
        calls.append((request, local_rate_clock["now"]))
        assert request.headers["User-Agent"] == fetch.USER_AGENT
        assert "authorization" not in request.headers and "cookie" not in request.headers
        is_robots = request.url.path == "/robots.txt"
        return httpx.Response(200, content=ALLOW if is_robots else html,
                              headers={"content-type": "text/plain" if is_robots else "text/html",
                                       "set-cookie": "private_cookie=SECRET"})

    with httpx.Client(transport=httpx.MockTransport(transport), auth=("secret", "password"),
                      cookies={"secret": "value"}, headers={"Authorization": "Bearer secret"},
                      follow_redirects=True) as client:
        result = fetch.fetch_html(url, tmp_path / "out", client=client)
        assert not client.is_closed
    assert result.html_path.read_bytes() == html
    assert result.sha256 == hashlib.sha256(html).hexdigest()
    provenance = json.loads(result.provenance_path.read_text())
    assert provenance["url"] == url and provenance["robots_allowed"] is True
    assert provenance["sha256"] == result.sha256 and provenance["http_status"] == 200
    assert "SECRET" not in result.provenance_path.read_text()
    assert provenance["rights_status"] == "not_established_by_technical_access"
    assert len(calls) == 2 and calls[1][1] - calls[0][1] >= 3


@pytest.mark.parametrize("status,body,mime,code", [
    (200, b"User-agent: *\nDisallow: /\n", "text/plain", "ROBOTS_DENIED"),
    (200, b"server error", "text/plain", "ROBOTS_INVALID"),
    (200, b"<html>challenge</html>", "text/html", "CONTENT_TYPE"),
    (403, b"blocked", "text/plain", "ROBOTS_UNAVAILABLE"),
    (404, b"not found", "text/plain", "ROBOTS_UNAVAILABLE"),
    (429, b"limited", "text/plain", "ROBOTS_UNAVAILABLE"),
    (301, b"redirect", "text/plain", "REDIRECT_BLOCKED"),
])
def test_unavailable_or_disallowed_robots_never_requests_page(status, body, mime, code, tmp_path):
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(status, content=body, headers={"content-type": mime,
                                                             "location": "https://evil.test/"})

    with httpx.Client(transport=httpx.MockTransport(transport), follow_redirects=True) as client:
        with pytest.raises(fetch.FetchBlocked) as error:
            fetch.fetch_html(RYM, tmp_path / "out", client=client)
    assert error.value.code == code
    assert len(calls) == 1 and calls[0].url.path == "/robots.txt"
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("status,body,headers,code", [
    (403, PAGE, {}, "HTTP_BLOCKED"), (429, PAGE, {}, "HTTP_BLOCKED"),
    (500, PAGE, {}, "HTTP_BLOCKED"),
    (302, PAGE, {"location": AOTY}, "REDIRECT_BLOCKED"),
    (200, PAGE, {"cf-mitigated": "challenge"}, "CHALLENGE"),
    (200, PAGE.replace(b"Test album", b"Just a moment..."), {}, "CHALLENGE"),
    (200, PAGE.replace(b"Test album", b"  Just a moment...  "), {}, "CHALLENGE"),
    (200, PAGE.replace(b"</body>", b'<form id="challenge-form"></form></body>'), {}, "CHALLENGE"),
    (200, b"<html><title>Error</title><h1>Not found</h1></html>", {}, "NOT_RELEASE_PAGE"),
    (200, PAGE, {"content-type": "application/json"}, "CONTENT_TYPE"),
])
def test_page_errors_challenges_and_redirects_are_not_saved_or_retried(status, body, headers, code, tmp_path):
    calls = []

    def transport(request):
        calls.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, content=ALLOW, headers={"content-type": "text/plain"})
        return httpx.Response(status, content=body, headers={"content-type": "text/html", **headers})

    with httpx.Client(transport=httpx.MockTransport(transport), follow_redirects=True) as client:
        with pytest.raises(fetch.FetchBlocked) as error:
            fetch.fetch_html(RYM, tmp_path / "out", client=client)
    assert error.value.code == code and len(calls) == 2
    assert not (tmp_path / "out").exists()


def test_stream_size_limit_applies_without_content_length(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch, "MAX_HTML_BYTES", 100)

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"x" * 60
            yield b"y" * 60

    def transport(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, content=ALLOW, headers={"content-type": "text/plain"})
        return httpx.Response(200, stream=Stream(), headers={"content-type": "text/html"})

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(fetch.FetchBlocked) as error:
            fetch.fetch_html(RYM, tmp_path / "out", client=client)
    assert error.value.code == "SIZE_LIMIT" and not (tmp_path / "out").exists()


def test_network_timeout_is_not_retried(tmp_path):
    calls = []

    def transport(request):
        calls.append(request)
        assert all(value <= fetch.IO_TIMEOUT for value in request.extensions["timeout"].values())
        raise httpx.ReadTimeout("SECRET response details")

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(fetch.FetchBlocked) as error:
            fetch.fetch_html(RYM, tmp_path / "out", client=client)
    assert error.value.code == "NETWORK_ERROR" and "SECRET" not in str(error.value)
    assert len(calls) == 1


def test_crawl_delay_persists_across_invocations_and_no_extra_requests(tmp_path, local_rate_clock):
    times = []

    def transport(request):
        times.append(local_rate_clock["now"])
        if request.url.path == "/robots.txt":
            return httpx.Response(200, content=ALLOW + b"Crawl-delay: 7\n",
                                  headers={"content-type": "text/plain"})
        return httpx.Response(200, content=PAGE, headers={"content-type": "text/html"})

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        fetch.fetch_html(RYM, tmp_path / "out", client=client)
        fetch.fetch_html(RYM, tmp_path / "out", client=client)
    assert len(times) == 4 and all(b - a >= 7 for a, b in zip(times, times[1:]))


def test_slow_stream_expires_before_capture(tmp_path, local_rate_clock):
    class SlowStream(httpx.SyncByteStream):
        def __iter__(self):
            local_rate_clock["now"] += fetch.TOTAL_TIMEOUT + 1
            yield PAGE

    def transport(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, content=ALLOW, headers={"content-type": "text/plain"})
        return httpx.Response(200, stream=SlowStream(), headers={"content-type": "text/html"})

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(fetch.FetchBlocked) as error:
            fetch.fetch_html(RYM, tmp_path / "out", client=client)
    assert error.value.code == "DEADLINE" and not (tmp_path / "out").exists()


@pytest.mark.parametrize("robots,code", [
    (b"User-agent: *\nDisallow: /album/*.php\n", "ROBOTS_UNSUPPORTED"),
    (b"User-agent: *\nDisallow: /album/123-test-artist-test-album.php$\n", "ROBOTS_UNSUPPORTED"),
    (b"User-agent: MusicDiscovery\nAllow: /\n\nUser-agent: MusicDiscovery\nDisallow: /album/\n",
     "ROBOTS_UNSUPPORTED"),
    (b"User-agent: Music\nAllow: /\n\nUser-agent: MusicDiscovery\nDisallow: /album/\n",
     "ROBOTS_UNSUPPORTED"),
    (b"User-agent: *\nAllow: /\nDisallow: /album/\n", "ROBOTS_UNSUPPORTED"),
    (b"User-agent: *\nDisallow:\nDisallow: /album/\n", "ROBOTS_UNSUPPORTED"),
    (b"User-agent: *\nAllow: /\n\nUser-agent: *\nDisallow: /\n", "ROBOTS_UNSUPPORTED"),
    (ALLOW + b"Request-rate: 0/10\n", "ROBOTS_INVALID"),
    (ALLOW + b"Request-rate: 1/0\n", "ROBOTS_INVALID"),
    (ALLOW + b"Request-rate: malformed\n", "ROBOTS_INVALID"),
])
def test_unsupported_robots_rules_fail_closed_before_page(robots, code, tmp_path):
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(200, content=robots, headers={"content-type": "text/plain"})

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(fetch.FetchBlocked) as error:
            fetch.fetch_html(AOTY, tmp_path / "out", client=client)
    assert error.value.code == code
    assert len(calls) == 1 and calls[0].url.path == "/robots.txt"
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("album_title", [b"Access Denied", b"Captcha", b"Sign In"])
def test_real_album_names_are_not_confused_with_challenges(album_title):
    page = PAGE.replace(b"Test album", b"Artist - " + album_title + b" - Rate Your Music")
    page = page.replace(b">Album</h1>", b">" + album_title + b"</h1>")
    fetch._real_page(page, "rateyourmusic.com")
