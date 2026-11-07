import httpx
import pytest

from discovery.web.artwork import ArtworkCache, match_artwork


@pytest.fixture(autouse=True)
def independent_link_sources(monkeypatch):
    # These tests exercise store identity/cache; enrichment has its own transport tests.
    from discovery.web.streaming import StreamingDiscovery
    monkeypatch.setattr(StreamingDiscovery, 'musicbrainz', lambda *args: ([], 'not_found'))
    monkeypatch.setattr(StreamingDiscovery, 'album_link', lambda *args: ([], 'not_found'))


ROW = {"collectionName": "DAMN.", "artistName": "Kendrick Lamar",
       "artworkUrl100": "https://is1-ssl.mzstatic.com/image/thumb/Music/test/100x100bb.jpg",
       "collectionViewUrl": "https://music.apple.com/us/album/damn/123"}


def test_exact_identity_and_safe_urls():
    assert match_artwork([ROW], "DAMN.", ["Kendrick Lamar"]).artwork.endswith("600x600bb.jpg")
    assert match_artwork([ROW], "DAMN.", ["Someone else"]).artwork is None
    assert match_artwork([ROW], "Other title", ["Kendrick Lamar"]).artwork is None
    assert match_artwork([{**ROW, "artworkUrl100": "https://mzstatic.com.evil.test/a"}], "DAMN.", ["Kendrick Lamar"]).artwork is None
    assert match_artwork([{**ROW, "collectionViewUrl": "javascript:alert(1)"}], "DAMN.", ["Kendrick Lamar"]).artwork is None


def test_cache_survives_restart_and_negative_results(tmp_path, monkeypatch):
    calls = []

    def get(url, **kwargs):
        calls.append(kwargs)
        return httpx.Response(200, json={"results": [ROW]}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", get)
    path = tmp_path / 'art.sqlite3'
    assert ArtworkCache(path).resolve("DAMN.", ["Kendrick Lamar"]).artwork
    assert ArtworkCache(path).resolve("DAMN.", ["Kendrick Lamar"]).artwork
    assert len(calls) == 2
    assert ArtworkCache(path).resolve("Unknown", ["Unknown"]).artwork is None
    assert ArtworkCache(path).resolve("Unknown", ["Unknown"]).artwork is None
    assert len(calls) == 4


def test_provider_outage_is_cached_briefly(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise httpx.ConnectError("offline")
    monkeypatch.setattr(httpx, 'get', fail)
    cache = ArtworkCache(tmp_path / 'art.sqlite3')
    assert cache.resolve("DAMN.", ["Kendrick Lamar"]).artwork is None


def test_aoty_parser_exact_page_and_cdn():
    import pytest

    from discovery.web.aoty_artwork import parse_artwork
    url = "https://www.albumoftheyear.org/album/123-test.php"
    html = '<link rel="canonical" href="' + url + '"><meta property="og:title" content="Artist - Album"><meta property="og:image" content="https://cdn2.albumoftheyear.org/750x/album/123-test.jpg">'
    assert parse_artwork(html, url, 'Album', ['Artist']).endswith('123-test.jpg')
    with pytest.raises(ValueError):
        parse_artwork(html, url, 'Other album', ['Artist'])
    with pytest.raises(ValueError):
        parse_artwork(html, url.replace('123-', '124-'), 'Album', ['Artist'])
    assert parse_artwork(html.replace('cdn2.albumoftheyear.org', 'evil.test'), url, 'Album', ['Artist']) is None


def test_aoty_priority_and_persistent_cache(tmp_path, monkeypatch):
    import discovery.web.artwork as module
    calls = []
    def fetch(url, title, artists):
        calls.append(url)
        return 'https://cdn2.albumoftheyear.org/750x/album/123-test.jpg'
    monkeypatch.setattr(module, 'fetch_artwork', fetch)
    store_calls = []
    def lookup(url, **kwargs):
        store_calls.append(url)
        return httpx.Response(200, json={"results": [{**ROW, "collectionName": "Album", "artistName": "Artist"}]}, request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx, 'get', lookup)
    path = tmp_path / 'art.sqlite3'
    url = 'https://www.albumoftheyear.org/album/123-test.php'
    for _ in range(2):
        result = ArtworkCache(path).resolve('Album', ['Artist'], url)
        assert result.source_url == url
        assert result.listening_url == ROW["collectionViewUrl"]
        assert result.artwork.endswith("123-test.jpg")
        assert result.aoty_status == 'found'
    assert calls == [url]
    assert set(store_calls) == {"https://itunes.apple.com/search", "https://api.deezer.com/search/album"}


def test_aoty_blocked_falls_back_and_cools_down(tmp_path, monkeypatch):
    import discovery.web.artwork as module
    from discovery.catalog.web.fetch import FetchBlocked
    calls = []
    def blocked(*args):
        calls.append(args)
        raise FetchBlocked('ROBOTS_UNAVAILABLE', 'HTTP 403', status_code=403)
    monkeypatch.setattr(module, 'fetch_artwork', blocked)
    monkeypatch.setattr(httpx, 'get', lambda url, **kwargs: httpx.Response(
        200, json={'results': [ROW]}, request=httpx.Request('GET', url)))
    path = tmp_path / 'art.sqlite3'
    result = ArtworkCache(path).resolve('DAMN.', ['Kendrick Lamar'], 'https://www.albumoftheyear.org/album/123-test.php')
    assert result.artwork and result.listening_url
    assert result.aoty_status == 'ROBOTS_UNAVAILABLE'
    result = ArtworkCache(path).resolve('Other', ['Artist'], 'https://www.albumoftheyear.org/album/124-test.php')
    assert result.artwork is None
    assert result.aoty_status == 'ROBOTS_UNAVAILABLE'
    assert len(calls) == 1


def test_store_suffix_alias_and_wrong_volume():
    from discovery.web.artwork import match_deezer
    assert match_artwork([{**ROW, 'collectionName': 'Hell Can Wait - EP', 'artistName': 'Vince Staples'}], 'Hell Can Wait', ['Vince Staples']).artwork
    assert match_artwork([{**ROW, 'collectionName': 'The College Dropout', 'artistName': 'Kanye West'}], 'The College Dropout', ['Ye']).artwork
    assert not match_artwork([{**ROW, 'collectionName': 'PeteStrumentals 2', 'artistName': 'Pete Rock'}], 'PeteStrumentals', ['Pete Rock']).artwork
    row = {'title': 'Imperial', 'artist': {'name': 'Denzel Curry'}, 'cover_big': 'https://cdn-images.dzcdn.net/images/cover/test/500.jpg', 'link': 'https://www.deezer.com/album/123'}
    assert match_deezer([row], 'Imperial', ['Denzel Curry']).listening_service == 'Deezer'
    assert not match_deezer([row], 'Imperial', ['Other']).artwork
    assert not match_deezer([{**row, 'cover_big': 'https://evil.test/cover.jpg'}], 'Imperial', ['Denzel Curry']).artwork


def test_deezer_used_after_empty_apple_search(tmp_path, monkeypatch):
    row = {'title': 'Imperial', 'artist': {'name': 'Denzel Curry'}, 'cover_big': 'https://cdn-images.dzcdn.net/images/cover/test/500.jpg', 'link': 'https://www.deezer.com/album/123'}
    def get(url, **kwargs):
        return httpx.Response(200, json={'data': [row]} if 'deezer.com' in url else {'results': []}, request=httpx.Request('GET', url))
    monkeypatch.setattr(httpx, 'get', get)
    result = ArtworkCache(tmp_path / 'art.sqlite3').resolve('Imperial', ['Denzel Curry'])
    assert result.artwork == row['cover_big']
    assert result.listening_service == 'Deezer'


def test_album_pages_only_and_no_partial_collaboration():
    for link in ['https://music.apple.com/us/artist/artist/123',
                 'https://music.apple.com/us/search?term=album',
                 'https://music.apple.com/us/album/damn/123?i=456',
                 'https://music.apple.com:8443/us/album/damn/123',
                 'https://user:secret@music.apple.com/us/album/damn/123']:
        assert match_artwork([{**ROW, 'collectionViewUrl': link}], 'DAMN.', ['Kendrick Lamar']).listening_url is None
    assert match_artwork([ROW], 'DAMN.', ['Kendrick Lamar', 'Other Artist']).listening_url is None


def test_ambiguous_store_result_not_chosen_and_cover_not_required():
    assert match_artwork([ROW, {**ROW, 'collectionViewUrl': 'https://music.apple.com/us/album/damn/456'}], 'DAMN.', ['Kendrick Lamar']).listening_url is None
    assert match_artwork([ROW, ROW], 'DAMN.', ['Kendrick Lamar']).listening_url == ROW['collectionViewUrl']
    result = match_artwork([{k: v for k, v in ROW.items() if k != 'artworkUrl100'}], 'DAMN.', ['Kendrick Lamar'])
    assert result.listening_url == ROW['collectionViewUrl']
    assert result.artwork is None


def test_missing_platform_pages_do_not_discard_aoty_cover(tmp_path, monkeypatch):
    import discovery.web.artwork as module
    monkeypatch.setattr(module, 'fetch_artwork', lambda *args: 'https://cdn2.albumoftheyear.org/album/123-test.jpg')
    monkeypatch.setattr(httpx, 'get', lambda url, **kwargs: httpx.Response(200, json={'results': [], 'data': []}, request=httpx.Request('GET', url)))
    result = ArtworkCache(tmp_path/'art.sqlite3').resolve('Album', ['Artist'], 'https://www.albumoftheyear.org/album/123-test.php')
    assert result.artwork and result.source_url and result.aoty_status == 'found'
    assert result.listening_url is None


def test_old_cache_cannot_restore_unvalidated_platform_link(tmp_path, monkeypatch):
    import json
    import sqlite3
    import time

    from discovery.web.schemas import Artwork
    cache = ArtworkCache(tmp_path/'art.sqlite3')
    with sqlite3.connect(cache.path) as c:
        c.execute('INSERT INTO artwork VALUES (?,?,?)', (
            json.dumps(['aoty-v2-deezer', 'DAMN.', ['Kendrick Lamar'], None]), time.time()+86400,
            Artwork(listening_url='https://music.apple.com/us/search?term=damn').model_dump_json()))
    monkeypatch.setattr(httpx, 'get', lambda url, **kwargs: httpx.Response(200, json={'results': [ROW]}, request=httpx.Request('GET', url)))
    assert cache.resolve('DAMN.', ['Kendrick Lamar']).listening_url == ROW['collectionViewUrl']


def test_cached_album_does_not_wait_behind_another_lookup(tmp_path):
    import hashlib
    import json
    import sqlite3
    import time
    from concurrent.futures import ThreadPoolExecutor

    from discovery.web.artwork import ArtworkCache
    from discovery.web.schemas import Artwork

    cache = ArtworkCache(tmp_path / 'parallel.sqlite3')
    key = json.dumps(['album-pages-v4', 'Cached album', ['Artist'], None, None], ensure_ascii=False)
    with sqlite3.connect(cache.path) as conn:
        conn.execute('INSERT INTO artwork VALUES (?,?,?)', (key, time.time() + 60,
            Artwork(listening_service='Apple Music', listening_url='https://music.apple.com/us/album/123').model_dump_json()))
    with ThreadPoolExecutor(max_workers=1) as pool:
        with cache.key_locks[int(hashlib.sha256(key.encode()).hexdigest(), 16) % 64]:
            future = pool.submit(cache.resolve, 'Cached album', ['Artist'])
            assert future.result(timeout=1).listening_url.endswith('/123')


def test_artwork_concurrency_does_not_consume_api_thread_pool(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from fastapi.testclient import TestClient

    from discovery.catalog.demo import make_demo_catalog
    from discovery.web.app import create_app
    from discovery.web.schemas import Artwork

    app = create_app(tmp_path, make_demo_catalog(), allowed_origins=['http://testserver'])
    release_id = app.state.catalog.releases[0].id
    entered, release, lock = threading.Event(), threading.Event(), threading.Lock()
    active = peak = 0

    def slow(*args):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active == 2:
                entered.set()
        release.wait(timeout=5)
        with lock:
            active -= 1
        return Artwork()

    app.state.artwork_cache.resolve = slow
    with TestClient(app, headers={'Origin': 'http://testserver'}) as client:
        assert client.post('/api/auth/register', json={'username': 'covers', 'password': 'covers-password-123',
            'password_confirmation': 'covers-password-123'}).status_code == 200
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(client.get, f'/api/releases/{release_id}/artwork') for _ in range(8)]
            try:
                assert entered.wait(timeout=2)
                assert client.get('/api/chats').status_code == 200
                assert peak == 2
            finally:
                release.set()
            assert all(future.result(timeout=5).status_code == 200 for future in futures)
        assert peak == 2
