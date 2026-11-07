import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from discovery.web.artwork import ArtworkCache
from discovery.web.streaming import (
    StreamingDiscovery,
    direct_link,
    merge_links,
    parse_album_link,
)

GID = 'ae04379e-7502-4a9e-959c-45a8cffbed29'
RID = '6798e6ed-ef9f-42df-bdee-96a774c50516'
SPOTIFY = 'https://open.spotify.com/album/6A2IQLnp76gVz8OJLWWVxF'
APPLE = 'https://music.apple.com/us/album/zig/1817607896'
DEEZER = 'https://www.deezer.com/album/763788411'
GROUP = {'id': GID, 'title': 'Zig', 'artist-credit': [{'name': 'Poppy'}], 'first-release-date': '2023-10-27'}


def relation(url, **kwargs):
    return {'type': 'streaming', 'url': {'resource': url}, **kwargs}


def html_page(**changes):
    entity = {'provider': 'deezer', 'type': 'album', 'id': '763788411', 'title': 'Zig', 'artistName': 'Poppy'}
    data = {'entityUniqueId': 'deezer|album|763788411', 'entityData': entity, 'sections': [
        {'sectionId': 'section|auto|links|listen', 'show': True, 'links': [
            {'linkId': 'link|auto|spotify', 'uniqueId': 'spotify|album|abc', 'show': True, 'url': SPOTIFY},
            {'linkId': 'link|auto|apple', 'uniqueId': 'itunes|album|123', 'show': True},
            {'linkId': 'link|auto|deezer', 'uniqueId': 'deezer|song|123', 'show': True, 'url': DEEZER},
            {'linkId': 'custom', 'uniqueId': 'itunes|album|123', 'show': True, 'url': APPLE},
        ]}]}
    data.update(changes)
    return '<script id="__NEXT_DATA__" type="application/json">' + json.dumps({'props': {'pageProps': {'pageData': data}}}) + '</script>'


@pytest.fixture
def discovery(tmp_path, monkeypatch):
    monkeypatch.setattr(StreamingDiscovery, 'wait', lambda *args: None)
    return StreamingDiscovery(tmp_path / 'media.sqlite3')


def transport(monkeypatch, *, overrides=None):
    calls = []
    overrides = overrides or {}

    def get(url, **kwargs):
        calls.append((url, kwargs.get('params')))
        if url in overrides:
            value = overrides[url]
            if isinstance(value, Exception):
                raise value
            return httpx.Response(200, json=value, request=httpx.Request('GET', url))
        if url.endswith('/search'):
            payload = {'results': [{'collectionName': 'Zig', 'artistName': 'Poppy', 'collectionViewUrl': APPLE}]}
        elif 'api.deezer.com' in url:
            payload = {'data': [{'title': 'Zig', 'artist': {'name': 'Poppy'}, 'link': DEEZER}]}
        elif url.endswith('/release-group/'):
            payload = {'release-groups': [GROUP]}
        elif url.endswith(GID):
            payload = {**GROUP, 'relations': [relation('https://label.bandcamp.com/album/zig')]}
        elif url.endswith('/release/'):
            payload = {'release-count': 1, 'releases': [{**GROUP, 'id': RID, 'status': 'Official', 'relations': [
                relation(SPOTIFY), relation('https://music.youtube.com/playlist?list=OLAK5uy_example'),
                relation('https://open.spotify.com/track/no'), relation('https://evil.test/album/x'),
                relation('https://play.anghami.com/album/123', ended=True),
            ]}]}
        elif url.endswith('/robots.txt'):
            return httpx.Response(200, text='User-agent: *\nDisallow:\n', request=httpx.Request('GET', url))
        elif 'album.link/' in url:
            return httpx.Response(200, text=html_page(), request=httpx.Request('GET', url))
        else:
            raise AssertionError(url)
        return httpx.Response(200, json=payload, request=httpx.Request('GET', url))

    monkeypatch.setattr(httpx, 'get', get)
    return calls


@pytest.mark.parametrize('url,service', [
    (SPOTIFY, 'Spotify'), (APPLE, 'Apple Music'), (DEEZER, 'Deezer'),
    ('https://music.youtube.com/playlist?list=OLAK5uy_123', 'YouTube Music'),
    ('https://label.bandcamp.com/album/zig', 'Bandcamp'),
    ('https://music.yandex.kz/album/123', 'Яндекс Музыка'),
    ('https://listen.tidal.com/album/123', 'TIDAL'),
    ('https://music.amazon.com/albums/ABC123', 'Amazon Music'),
    ('https://www.qobuz.com/gb-en/album/zig-poppy/abc123', 'Qobuz'),
    ('https://www.pandora.com/AL:123', 'Pandora'),
    ('https://play.napster.com/album/alb.123', 'Napster'),
    ('https://www.boomplay.com/albums/123', 'Boomplay'),
    ('https://play.anghami.com/album/123', 'Anghami'),
    ('https://www.beatport.com/release/zig/123', 'Beatport'),
])
def test_platform_album_paths(url, service):
    assert direct_link(url, 'source').service == service


@pytest.mark.parametrize('url', [
    SPOTIFY.replace('https:', 'http:'), SPOTIFY.replace('spotify.com', 'spotify.com.evil.test'),
    SPOTIFY.replace('open.spotify.com', 'user:password@open.spotify.com'),
    SPOTIFY.replace('open.spotify.com', 'open.spotify.com:443'),
    'javascript:alert(1)', 'https://open.spotify.com/search/zig', 'https://open.spotify.com/artist/123',
    APPLE + '?i=12', 'https://music.youtube.com/watch?v=123',
    'https://music.youtube.com/playlist?list=PL_user_playlist',
    'https://music.youtube.com/playlist?list=OLAK5uy_album&v=123',
    'https://artist.bandcamp.com/track/zig', 'https://tidal.com/track/123',
    'https://music.amazon.com/albums/123?trackAsin=456', 'https://www.qobuz.com/gb-en/artist/poppy/123',
    'https://play.anghami.com/song/123', SPOTIFY + '\n', SPOTIFY + '#anything',
])
def test_non_album_or_unsafe_url_rejected(url):
    assert direct_link(url, 'source') is None


def test_all_sources_merged_once_cache_survives_restart(discovery, monkeypatch):
    calls = transport(monkeypatch)
    cache = ArtworkCache(discovery.path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(cache.resolve, 'Zig', ['Poppy'], None, 2023) for _ in range(2)]
        results = [f.result() for f in futures]
    assert results[0] == results[1]
    assert {link.service for link in results[0].listening_links} == {'Spotify', 'Apple Music', 'Deezer', 'Bandcamp', 'YouTube Music'}
    assert len(calls) == 7
    assert ArtworkCache(discovery.path).resolve('Zig', ['Poppy'], None, 2023) == results[0]
    assert len(calls) == 7
    assert all(link.source_url.startswith('https:') for link in results[0].listening_links)


def test_store_outage_preserves_other_platforms_and_short_retry(discovery, monkeypatch):
    transport(monkeypatch, overrides={'https://itunes.apple.com/search': httpx.ConnectError('offline')})
    result = ArtworkCache(discovery.path).resolve('Zig', ['Poppy'], None, 2023)
    assert result.link_status['apple'] == 'unavailable'
    assert {'Spotify', 'Deezer', 'Bandcamp'} <= {link.service for link in result.listening_links}
    with sqlite3.connect(discovery.path) as conn:
        expiry = conn.execute('SELECT expires FROM artwork').fetchone()[0]
    assert 0 < expiry - time.time() <= 300


@pytest.mark.parametrize('groups', [
    [{**GROUP, 'artist-credit': [{'name': 'Other'}]}],
    [{**GROUP, 'title': 'Zig (Deluxe)'}], [{**GROUP, 'first-release-date': '2025-01-01'}],
    [GROUP, {**GROUP, 'id': RID}], [{**GROUP, 'artist-credit': [{'name': 'Poppy'}, {'name': 'Other'}]}],
])
def test_ambiguous_or_mismatched_group_not_used(discovery, monkeypatch, groups):
    calls = transport(monkeypatch, overrides={'https://musicbrainz.org/ws/2/release-group/': {'release-groups': groups}})
    links, status = discovery.musicbrainz('Zig', ['Poppy'], 2023)
    assert not links
    assert status in {'ambiguous', 'not_found'}
    assert len(calls) == 1


def test_wrong_edition_and_artist_relationships_not_used(discovery, monkeypatch):
    releases = [{**GROUP, 'id': RID, 'status': 'Official', 'relations': [relation(SPOTIFY)], **extra} for extra in [
        {'title': 'Zig Sessions'}, {'artist-credit': [{'name': 'Other'}]},
        {'disambiguation': 'deluxe edition'}, {'status': 'Bootleg'}]]
    transport(monkeypatch, overrides={'https://musicbrainz.org/ws/2/release/': {'releases': releases}})
    links, _ = discovery.musicbrainz('Zig', ['Poppy'], 2023)
    assert [link.service for link in links] == ['Bandcamp']


def test_partial_browse_failure_keeps_group_links(discovery, monkeypatch):
    transport(monkeypatch, overrides={'https://musicbrainz.org/ws/2/release/': httpx.ReadTimeout('timeout')})
    links, status = discovery.musicbrainz('Zig', ['Poppy'], 2023)
    assert status == 'unavailable'
    assert [link.service for link in links] == ['Bandcamp']


def test_pagination_is_bounded_and_reported(discovery, monkeypatch):
    calls = transport(monkeypatch, overrides={'https://musicbrainz.org/ws/2/release/': {'release-count': 900, 'releases': []}})
    _, status = discovery.musicbrainz('Zig', ['Poppy'], 2023)
    assert status == 'partial'
    assert [params['offset'] for url, params in calls if url.endswith('/release/')] == [0, 100, 200]


def test_album_link_identity_placeholders_and_custom_links():
    seed = ('deezer', '763788411', 'd/763788411')
    assert [link.url for link in parse_album_link(html_page(), seed, 'Zig', ['Poppy'])] == [SPOTIFY]
    with pytest.raises(ValueError):
        parse_album_link(html_page(), seed, 'Wrong album', ['Poppy'])
    with pytest.raises(ValueError):
        parse_album_link(html_page(), seed, 'Zig', ['Other'])
    with pytest.raises(ValueError):
        parse_album_link(html_page(entityUniqueId='deezer|song|763788411'), seed, 'Zig', ['Poppy'])
    hidden = [{'sectionId': 'section|auto|links|listen', 'show': False, 'links': [
        {'linkId': 'link|auto|spotify', 'uniqueId': 'spotify|album|abc', 'show': True, 'url': SPOTIFY}]}]
    assert not parse_album_link(html_page(sections=hidden), seed, 'Zig', ['Poppy'])


def test_robots_block_and_failure_prevent_page_fetch(discovery, monkeypatch):
    calls = []
    for status, content in [(200, 'User-agent: *\nDisallow: /'), (403, '')]:
        def get(url, **kwargs):
            calls.append(url)
            return httpx.Response(status, text=content, request=httpx.Request('GET', url))
        monkeypatch.setattr(httpx, 'get', get)
        with sqlite3.connect(discovery.path) as conn:
            conn.execute('DELETE FROM media_source_cache')
        links, state = discovery.album_link([direct_link(DEEZER, DEEZER)], 'Zig', ['Poppy'])
        assert not links and state in {'blocked', 'unavailable'}
    assert calls == ['https://album.link/robots.txt'] * 2


def test_dedup_service():
    first = direct_link(SPOTIFY, 'musicbrainz')
    assert merge_links([first], [direct_link(SPOTIFY + '?si=token', 'album.link')]) == [first]


def test_rate_limit_shared_between_instances(tmp_path, monkeypatch):
    pauses = []
    monkeypatch.setattr('discovery.web.streaming.time.sleep', pauses.append)
    monkeypatch.setattr('discovery.web.streaming.time.time', lambda: 1000)
    one = StreamingDiscovery(tmp_path / 'cache.sqlite3')
    two = StreamingDiscovery(one.path)
    one.wait('musicbrainz.org', 1.1)
    two.wait('musicbrainz.org', 1.1)
    two.wait('itunes.apple.com', 3.1)
    assert pauses == pytest.approx([0, 1.1, 0])
