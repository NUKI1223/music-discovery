"""Library deletion, feedback revocation, provenance and account isolation."""
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from discovery.catalog.demo import make_demo_catalog
from discovery.contracts import UserProfile
from discovery.ranking import rank
from discovery.storage import Store
from discovery.web.app import create_app
from discovery.web.chat import claim_message
from discovery.web.schemas import MessageRequest


@pytest.fixture
def library(tmp_path):
    catalog = make_demo_catalog()
    def no_llm():
        raise AssertionError('Library management must not call the provider')
    app = create_app(tmp_path, catalog, client_factory=no_llm, allowed_origins=['http://testserver'])
    with TestClient(app, headers={'Origin': 'http://testserver'}) as client:
        response = client.post('/api/auth/register', json={'username': 'library', 'password': 'library-password-123',
                                                        'password_confirmation': 'library-password-123'})
        session = response.json()
        client.headers['X-CSRF-Token'] = session['csrf_token']
        store = Store(tmp_path / 'users' / session['user']['id'] / 'discovery.sqlite3')
        yield client, store, catalog, app


def selection(client, title='Первый чат', message_id=None):
    chat = client.post('/api/chats', json={'title': title, 'filters': {'genre_scope_id': 'hip-hop'}}).json()
    response = client.post(f"/api/chats/{chat['id']}/messages", json={
        'message_id': message_id or uuid4().hex, 'apply_filters': True})
    assert response.status_code == 200
    assert response.json()['cards']
    return chat, response.json()['cards']


def mark(client, card, kind='listened_like'):
    payload = {'feedback_id': uuid4().hex, 'decision_id': card['decision_id'],
               'release_id': card['release_id'], 'kind': kind}
    assert client.post('/api/feedback', json=payload).status_code == 200
    return payload


def test_chat_delete_preserves_collection_provenance_and_consumed_keys(library):
    client, store, _, _ = library
    first, cards = selection(client, message_id='permanent-request-key')
    second, second_cards = selection(client, title='Другой чат')
    assert cards[0]['release_id'] == second_cards[0]['release_id']
    mark(client, cards[0])
    entry = client.get('/api/collection').json()[0]
    assert {c['id'] for c in entry['chats']} == {first['id'], second['id']}
    assert entry['saved_at']
    assert client.delete(f"/api/chats/{first['id']}").status_code == 200
    assert client.get(f"/api/chats/{first['id']}").status_code == 404
    assert [c['id'] for c in client.get('/api/chats').json()] == [second['id']]
    entry = client.get('/api/collection').json()[0]
    assert entry['feedback'] == 'listened_like'
    deleted = next(c for c in entry['chats'] if c['id'] == first['id'])
    assert deleted == {'id': first['id'], 'title': 'Удалённый чат', 'deleted': True}
    assert store.feedback() and store.profile().known_release_ids == [cards[0]['release_id']]
    with store.connection() as conn:
        for table in ('web_messages', 'web_chat_cards', 'web_chat_memory'):
            assert conn.execute(f'SELECT count(*) FROM {table} WHERE chat_id=?', (first['id'],)).fetchone()[0] == 0
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    assert client.post(f"/api/chats/{second['id']}/messages", json={
        'message_id': 'permanent-request-key', 'apply_filters': True}).status_code == 409


def test_pending_chat_cannot_be_deleted(library):
    client, store, _, _ = library
    chat = client.post('/api/chats', json={'filters': {}}).json()
    claim_message(store, chat['id'], MessageRequest(message_id='pending-message'))
    assert client.delete(f"/api/chats/{chat['id']}").status_code == 409
    assert client.get(f"/api/chats/{chat['id']}").status_code == 200


@pytest.mark.parametrize('kind', ['known', 'save_for_later', 'listened_like', 'listened_dislike'])
def test_hide_preserves_feedback_but_excludes_even_unheard_albums(library, kind):
    client, store, catalog, _ = library
    _, cards = selection(client)
    payload = mark(client, cards[0], kind)
    before = store.feedback()
    assert client.delete(f"/api/collection/{cards[0]['release_id']}").status_code == 200
    assert client.get('/api/collection').json() == []
    reopened = Store(store.path)
    assert reopened.feedback() == before
    assert cards[0]['release_id'] in reopened.profile().known_release_ids
    request = reopened.decisions()[0].request
    assert cards[0]['release_id'] not in {item.release_id for item in rank(catalog, request, reopened.profile()).items}
    # Replaying the old mark cannot restore a hidden item; a NEW explicit mark can.
    assert client.post('/api/feedback', json=payload).status_code == 200
    assert client.get('/api/collection').json() == []
    mark(client, cards[0], 'save_for_later')
    assert len(client.get('/api/collection').json()) == 1


def test_forget_resets_all_album_marks_and_base_profile_across_scopes(library):
    client, store, catalog, _ = library
    first, cards = selection(client)
    rid = cards[0]['release_id']
    mark(client, cards[0], 'known')
    old = mark(client, cards[0], 'listened_like')
    mark(client, cards[0], 'save_for_later')
    mark(client, cards[1], 'listened_dislike')
    store.set_base_profile(UserProfile(known_release_ids=[rid], liked_release_ids_by_genre={'hip-hop': [rid]},
        disliked_release_ids_by_genre={'all': [rid]}, seed_artist_ids_by_genre={'hip-hop': [catalog.artists[0].id]}))
    assert client.delete(f'/api/collection/{rid}?forget=true').status_code == 200
    assert [c['release_id'] for c in client.get('/api/collection').json()] == [cards[1]['release_id']]
    profile = Store(store.path).profile()
    assert rid not in profile.known_release_ids
    assert all(rid not in ids for ids in profile.liked_release_ids_by_genre.values())
    assert all(rid not in ids for ids in profile.disliked_release_ids_by_genre.values())
    assert profile.seed_artist_ids_by_genre['hip-hop'] == [catalog.artists[0].id]
    assert all(event.release_id != rid for event in store.feedback())
    assert all(event['release_id'] != rid for event in store.export()['feedback'])
    assert client.post('/api/feedback', json=old).status_code == 409
    current = client.get(f"/api/chats/{first['id']}").json()['messages'][-1]['cards'][0]
    assert current['feedback'] is None and current['listened'] is False
    # Old-chat no-repeat context is retained, but a fresh chat can recommend it again.
    _, new_cards = selection(client)
    assert rid in {card['release_id'] for card in new_cards}
    mark(client, cards[0], 'listened_like')
    assert len(client.get('/api/collection').json()) == 2


@pytest.mark.parametrize('forget', [False, True])
def test_clear_all_is_durable_and_idempotent(library, forget):
    client, store, catalog, app = library
    _, cards = selection(client)
    for card in cards:
        mark(client, card, 'save_for_later')
    url = '/api/collection?forget=' + str(forget).lower()
    assert client.delete(url).status_code == 200
    assert client.delete(url).status_code == 200
    assert len(store.feedback()) == (0 if forget else 5)
    assert set(store.profile().known_release_ids) == (set() if forget else {c['release_id'] for c in cards})
    restarted = create_app(app.state.data_dir, catalog, allowed_origins=['http://testserver'])
    with TestClient(restarted, cookies=client.cookies) as fresh:
        assert fresh.get('/api/collection').json() == []


def test_destructive_routes_require_csrf_origin_and_account_ownership(library):
    alice, store, _, app = library
    chat, cards = selection(alice)
    mark(alice, cards[0])
    urls = [f"/api/chats/{chat['id']}", f"/api/collection/{cards[0]['release_id']}?forget=true", '/api/collection?forget=true']
    for url in urls:
        assert alice.delete(url, headers={'X-CSRF-Token': ''}).status_code == 403
        assert alice.delete(url, headers={'Origin': 'https://evil.example'}).status_code == 403
    with TestClient(app, headers={'Origin': 'http://testserver'}) as bob:
        for url in urls:
            assert bob.delete(url).status_code == 401
        session = bob.post('/api/auth/register', json={'username': 'second', 'password': 'second-password-123',
                            'password_confirmation': 'second-password-123'}).json()
        bob.headers['X-CSRF-Token'] = session['csrf_token']
        assert bob.delete(urls[0]).status_code == 404
        assert bob.delete(urls[1]).status_code == 404
        assert bob.delete(urls[2]).status_code == 200
    assert len(alice.get('/api/collection').json()) == 1
    assert len(alice.get('/api/chats').json()) == 1
    assert len(store.feedback()) == 1


def test_import_keeps_hidden_albums_and_revoked_marks(tmp_path):
    from discovery.web.cards import collection_cards
    from discovery.web.cli import import_legacy

    catalog = make_demo_catalog()
    source_app = create_app(tmp_path / 'source', catalog, allowed_origins=['http://testserver'])
    with TestClient(source_app, headers={'Origin': 'http://testserver'}) as client:
        session = client.post('/api/auth/register', json={'username': 'source', 'password': 'source-password-123',
            'password_confirmation': 'source-password-123'}).json()
        client.headers['X-CSRF-Token'] = session['csrf_token']
        _, cards = selection(client)
        mark(client, cards[0], 'listened_like')
        mark(client, cards[1], 'listened_dislike')
        assert client.delete(f"/api/collection/{cards[0]['release_id']}").status_code == 200
        assert client.delete(f"/api/collection/{cards[1]['release_id']}?forget=true").status_code == 200
        source = Store(tmp_path / 'source' / 'users' / session['user']['id'] / 'discovery.sqlite3')
    dest_app = create_app(tmp_path / 'dest', catalog, allowed_origins=['http://testserver'])
    with TestClient(dest_app, headers={'Origin': 'http://testserver'}) as client:
        session = client.post('/api/auth/register', json={'username': 'dest', 'password': 'dest-password-123',
            'password_confirmation': 'dest-password-123'}).json()
        import_legacy(tmp_path / 'dest', 'dest', source.path, catalog=catalog)
        dest = Store(tmp_path / 'dest' / 'users' / session['user']['id'] / 'discovery.sqlite3')
        assert dest.feedback() == source.feedback()
        assert dest.profile() == source.profile()
        assert collection_cards(dest) == []


def rate(client, card, kind, feedback_id=None, **extra):
    payload = {'feedback_id': feedback_id or uuid4().hex, 'decision_id': card['decision_id'],
               'release_id': card['release_id'], 'kind': kind, **extra}
    return client.post('/api/feedback', json=payload)


def test_mixed_rating_with_score_and_review_in_collection_and_chat(library):
    client, store, _, _ = library
    chat, cards = selection(client)
    assert rate(client, cards[0], 'listened_like').status_code == 200
    assert rate(client, cards[0], 'listened_mixed', score=60, review='  Местами сильно  ').status_code == 200
    entry = client.get('/api/collection').json()[0]
    assert (entry['feedback'], entry['score'], entry['review'], entry['listened']) == (
        'listened_mixed', 60, 'Местами сильно', True)
    profile = store.profile()
    assert cards[0]['release_id'] in profile.known_release_ids
    assert not profile.liked_release_ids_by_genre.get('hip-hop')
    assert not profile.disliked_release_ids_by_genre.get('hip-hop')
    shown = client.get(f"/api/chats/{chat['id']}").json()['messages'][-1]['cards'][0]
    assert (shown['feedback'], shown['score'], shown['review']) == ('listened_mixed', 60, 'Местами сильно')


def test_edit_replaces_score_and_review_and_event_without_score_clears_it(library):
    client, _, _, _ = library
    _, cards = selection(client)
    rate(client, cards[0], 'listened_mixed', score=40, review='первая')
    rate(client, cards[0], 'listened_mixed', score=70, review='вторая')
    entry = client.get('/api/collection').json()[0]
    assert (entry['score'], entry['review']) == (70, 'вторая')
    rate(client, cards[0], 'listened_mixed')
    entry = client.get('/api/collection').json()[0]
    assert (entry['feedback'], entry['score'], entry['review']) == ('listened_mixed', None, None)


def test_feedback_idempotency_includes_score_and_review(library):
    client, store, _, _ = library
    _, cards = selection(client)
    assert rate(client, cards[0], 'listened_like', 'same-key-1', score=80, review='ок').status_code == 200
    assert rate(client, cards[0], 'listened_like', 'same-key-1', score=80, review='ок').status_code == 200
    assert len(store.feedback()) == 1
    assert rate(client, cards[0], 'listened_like', 'same-key-1', score=81, review='ок').status_code == 409
    assert rate(client, cards[0], 'listened_like', 'same-key-1', score=80, review='иначе').status_code == 409
    assert rate(client, cards[0], 'listened_like', 'same-key-1', score=80).status_code == 409


@pytest.mark.parametrize('extra', [{'score': -1}, {'score': 101}, {'review': 'я' * 4001}])
def test_feedback_rejects_out_of_range_score_and_long_review(library, extra):
    client, store, _, _ = library
    _, cards = selection(client)
    assert rate(client, cards[0], 'listened_mixed', **extra).status_code == 422
    assert store.feedback() == []


@pytest.mark.parametrize('kind', ['known', 'save_for_later'])
@pytest.mark.parametrize('extra', [{'score': 50}, {'review': 'текст'}])
def test_score_and_review_rejected_for_non_listened_kinds(library, kind, extra):
    client, store, _, _ = library
    _, cards = selection(client)
    assert rate(client, cards[0], kind, **extra).status_code == 422
    assert store.feedback() == []


def test_blank_review_becomes_none(library):
    client, store, _, _ = library
    _, cards = selection(client)
    assert rate(client, cards[0], 'listened_mixed', score=10, review='   ').status_code == 200
    assert store.feedback()[0].review is None
