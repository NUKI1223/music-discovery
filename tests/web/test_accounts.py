from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from discovery.catalog.demo import make_demo_catalog
from discovery.contracts import Feedback, RequestIntent, UserProfile
from discovery.service import recommend
from discovery.storage import Store
from discovery.web.app import create_app
from discovery.web.cli import import_legacy

PASSWORD = "correct-horse-123"


def login_client(app, username):
    client = TestClient(app, headers={"Origin": "http://testserver"})
    result = client.post("/api/auth/register", json={"username": username, "password": PASSWORD,
                         "password_confirmation": PASSWORD})
    assert result.status_code == 200
    client.headers["X-CSRF-Token"] = result.json()["csrf_token"]
    return client, result.json()["user"]["id"]


def test_profile_isolated_persistent_and_per_genre(tmp_path):
    catalog = make_demo_catalog()
    app = create_app(tmp_path, catalog, allowed_origins=["http://testserver"])
    alice, alice_id = login_client(app, "alice")
    bob, _ = login_client(app, "bobby")
    profile = alice.get("/api/profile").json()
    assert profile["seed_artist_ids"] == [] and profile["onboarded"] is False
    profile.update(onboarded=True, seed_artist_ids=[catalog.artists[0].id], avoid_tag_ids=["rnb"])
    assert alice.put("/api/profile", json=profile).status_code == 200
    assert bob.get("/api/profile").json()["seed_artist_ids"] == []
    saved = alice.get("/api/profile?genre_scope_id=hip-hop").json()
    assert saved["avoid_tag_ids"] == ["rnb"]
    untouched = alice.get("/api/profile?genre_scope_id=jazz").json()
    assert untouched["seed_artist_ids"] == []
    assert untouched["avoid_tag_ids"] == []
    profile.update(seed_artist_ids=[catalog.artists[1].id], avoid_tag_ids=[])
    profile["filters"]["genre_scope_id"] = "jazz"
    assert alice.put("/api/profile", json=profile).status_code == 200
    core = Store(tmp_path / "users" / alice_id / "discovery.sqlite3").profile()
    assert core.avoid_tags_by_genre["hip-hop"] == ["rnb"]
    assert core.seed_artist_ids_by_genre["hip-hop"] == [catalog.artists[0].id]
    restarted = create_app(tmp_path, catalog, allowed_origins=["http://testserver"])
    with TestClient(restarted, cookies=alice.cookies) as fresh:
        assert fresh.get("/api/profile").json() == profile
    profile["seed_artist_ids"] = ["invented"]
    assert alice.put("/api/profile", json=profile).status_code == 422
    assert bob.get("/api/catalog/artists?q=demo").status_code == 200


def test_legacy_never_automatic_import_backup_and_replay(tmp_path):
    legacy = Store(tmp_path / "old.sqlite3")
    legacy.set_base_profile(UserProfile(seed_artist_ids_by_genre={"hip-hop": ["old-artist"]}))
    data = tmp_path / "web"
    app = create_app(data, make_demo_catalog(), allowed_origins=["http://testserver"])
    alice, alice_id = login_client(app, "alice")
    bob, _ = login_client(app, "bobby")
    assert alice.get("/api/profile").json()["seed_artist_ids"] == []
    backups = import_legacy(data, "alice", legacy.path)
    assert (backups / "legacy.sqlite3").exists()
    assert (backups / "account-before.sqlite3").exists()
    assert alice.get("/api/profile").json()["seed_artist_ids"] == ["old-artist"]
    assert bob.get("/api/profile").json()["seed_artist_ids"] == []
    with pytest.raises(ValueError, match="уже импортировал"):
        import_legacy(data, "ALICE", legacy.path)
    assert Store(data / "users" / alice_id / "discovery.sqlite3").profile().seed_artist_ids_by_genre == {"hip-hop": ["old-artist"]}
    with pytest.raises(ValueError, match="не найдена"):
        import_legacy(data, "alice", Path("/missing.sqlite3"))


def test_cli_password_reset_revokes_cookie(tmp_path):
    app = create_app(tmp_path, make_demo_catalog(), allowed_origins=["http://testserver"])
    alice, user_id = login_client(app, "alice")
    app.state.accounts.reset_password(user_id, "new-local-password")
    assert alice.get("/api/auth/session").status_code == 401
    assert alice.post("/api/auth/login", json={"username": "alice", "password": "new-local-password"}).status_code == 200


def test_legacy_import_retains_decisions_feedback_and_rejects_mixed_fixture(tmp_path):
    catalog = make_demo_catalog()
    legacy = Store(tmp_path / "legacy.sqlite3")
    decision = recommend(legacy, catalog, RequestIntent(genre_scope_id="hip-hop"))
    legacy.mark_shown(decision)
    legacy.append_feedback(Feedback(id="legacy-feedback", decision_id=decision.id,
                                    release_id=decision.result.items[0].release_id,
                                    kind="listened_like"))
    data = tmp_path / "web"
    app = create_app(data, catalog, allowed_origins=["http://testserver"])
    _, user_id = login_client(app, "alice")
    with pytest.raises(ValueError, match="нельзя смешивать"):
        import_legacy(data, "alice", legacy.path,
                      catalog=catalog.model_copy(update={"is_fixture": False}))
    import_legacy(data, "alice", legacy.path, catalog=catalog)
    imported = Store(data / "users" / user_id / "discovery.sqlite3")
    assert imported.decisions() == legacy.decisions()
    assert imported.feedback() == legacy.feedback()
    assert imported.export()["impressions"] == legacy.export()["impressions"]


def test_default_client_uses_shared_ledger_and_caps(tmp_path, monkeypatch):
    settings = {"DISCOVERY_DB": str(tmp_path / "runtime/history.sqlite3"),
                "OPENROUTER_API_KEY": "private-test-key", "OPENROUTER_MODEL": "test-model",
                "OPENROUTER_INPUT_USD_PER_MILLION": "0.1",
                "OPENROUTER_OUTPUT_USD_PER_MILLION": "0.3",
                "MAX_COST_PER_REQUEST_USD": "3", "MAX_COST_PER_DAY_USD": "10"}
    for key, value in settings.items():
        monkeypatch.setenv(key, value)
    app = create_app(tmp_path / "web", make_demo_catalog())
    first, second = app.state.client_factory(), app.state.client_factory()
    try:
        assert first.settings.request_cost_limit == .01
        assert first.settings.daily_cost_limit == .10
        assert first.ledger.path == second.ledger.path == str(tmp_path / "runtime/llm-budget.sqlite3")
        first.ledger.reserve(.02, .10)
        assert second.ledger.total() == .02
    finally:
        first.close()
        second.close()
    monkeypatch.setenv("MAX_COST_PER_REQUEST_USD", ".001")
    monkeypatch.setenv("MAX_COST_PER_DAY_USD", ".02")
    lower = app.state.client_factory()
    try:
        assert lower.settings.request_cost_limit == .001
        assert lower.settings.daily_cost_limit == .02
    finally:
        lower.close()


def test_http_foreign_chat_feedback_and_message_ids_are_isolated(tmp_path):
    app = create_app(tmp_path, make_demo_catalog(), allowed_origins=["http://testserver"])
    alice, _ = login_client(app, "alice")
    bob, _ = login_client(app, "bobby")
    created = alice.post("/api/chats", json={"filters": {"genre_scope_id": "hip-hop"}})
    assert created.status_code == 200
    chat_id = created.json()["id"]
    body = {"message_id": "account-message-1", "apply_filters": True}
    selection = alice.post(f"/api/chats/{chat_id}/messages", json=body)
    assert selection.status_code == 200
    card = selection.json()["cards"][0]
    assert bob.get(f"/api/chats/{chat_id}").status_code == 404
    assert bob.patch(f"/api/chats/{chat_id}/filters", json={"genre_scope_id": "jazz"}).status_code == 404
    assert bob.post(f"/api/chats/{chat_id}/messages", json=body).status_code == 404
    feedback = {"feedback_id": "account-feedback-1", "decision_id": card["decision_id"],
                "release_id": card["release_id"], "kind": "listened_like"}
    assert bob.post("/api/feedback", json=feedback).status_code == 404
    assert bob.get("/api/collection").json() == []
    assert bob.get("/api/chats").json() == []
    assert alice.get(f"/api/chats/{chat_id}").json()["filters"]["genre_scope_id"] == "hip-hop"
    # Identical client-generated idempotency keys belong to separate accounts.
    bob_chat = bob.post("/api/chats", json={"filters": {"genre_scope_id": "jazz"}}).json()["id"]
    own = bob.post(f"/api/chats/{bob_chat}/messages", json=body)
    assert own.status_code == 200
    own_card = own.json()["cards"][0]
    assert own_card["decision_id"] == card["decision_id"]
    assert own_card["release_id"] != card["release_id"]
    assert bob.post("/api/feedback", json=feedback).status_code == 404
    own_feedback = {**feedback, "release_id": own_card["release_id"]}
    assert bob.post("/api/feedback", json=own_feedback).status_code == 200
    assert alice.get("/api/collection").json() == []


def test_relative_runtime_paths_are_independent_of_cwd(tmp_path, monkeypatch):
    from discovery.config import PROJECT_ROOT
    from discovery.web.app import default_data_dir

    monkeypatch.setenv("DISCOVERY_DB", "runtime/discovery.sqlite3")
    monkeypatch.chdir(tmp_path)
    app = create_app(tmp_path / "isolated-web", make_demo_catalog())
    assert default_data_dir() == PROJECT_ROOT / "runtime/web"
    assert app.state.ledger_path == PROJECT_ROOT / "runtime/llm-budget.sqlite3"


def test_known_is_listened_unrated_excluded_across_chats_and_restart(tmp_path):
    catalog = make_demo_catalog()
    app = create_app(tmp_path, catalog, allowed_origins=['http://testserver'])
    alice, alice_id = login_client(app, 'known_alice')
    bob, _ = login_client(app, 'known_bob')

    def selection(client, message_id):
        chat = client.post('/api/chats', json={'filters': {'genre_scope_id': 'hip-hop'}}).json()
        result = client.post(f"/api/chats/{chat['id']}/messages",
                            json={'message_id': message_id, 'apply_filters': True})
        assert result.status_code == 200
        return chat['id'], result.json()['cards']

    chat_id, cards = selection(alice, 'known-first')
    card = cards[0]
    body = dict(feedback_id='known-event', decision_id=card['decision_id'],
                release_id=card['release_id'], kind='known')
    for _ in range(2):
        assert alice.post('/api/feedback', json=body).status_code == 200
    account = Store(tmp_path / 'users' / alice_id / 'discovery.sqlite3')
    assert len(account.feedback()) == 1
    assert card['release_id'] in account.profile().known_release_ids
    assert not account.profile().liked_release_ids_by_genre
    assert not account.profile().disliked_release_ids_by_genre
    assert alice.get('/api/collection').json()[0]['listened'] is True
    assert bob.get('/api/collection').json() == []
    _, bob_cards = selection(bob, 'bob-first')
    assert card['release_id'] in {c['release_id'] for c in bob_cards}

    # A later save does not erase the factual listening history or exclusion.
    assert alice.post('/api/feedback', json={**body, 'feedback_id': 'saved-later', 'kind': 'save_for_later'}).status_code == 200
    restarted = create_app(tmp_path, catalog, allowed_origins=['http://testserver'])
    with TestClient(restarted, cookies=alice.cookies, headers=dict(alice.headers)) as fresh:
        saved = fresh.get('/api/collection').json()[0]
        assert saved['listened'] is True and saved['feedback'] == 'save_for_later'
        detail = fresh.get(f'/api/chats/{chat_id}').json()
        historical = next(c for m in detail['messages'] for c in m['cards'] if c['release_id'] == card['release_id'])
        assert historical['listened'] is True
        _, next_cards = selection(fresh, 'known-after-restart')
        assert next_cards and card['release_id'] not in {c['release_id'] for c in next_cards}


def test_free_scope_profile_and_lookup_do_not_rewrite_existing_taste(tmp_path):
    catalog = make_demo_catalog()
    app = create_app(tmp_path, catalog, allowed_origins=["http://testserver"])
    alice, aid = login_client(app, "free-user")
    original = alice.get("/api/profile").json()
    original.update(onboarded=True, seed_artist_ids=[catalog.artists[0].id])
    assert alice.put("/api/profile", json=original).status_code == 200
    free = alice.get("/api/profile?genre_scope_id=all")
    assert free.status_code == 200
    assert free.json()["filters"]["genre_scope_id"] == "all"
    assert alice.put("/api/profile", json=free.json()).status_code == 200
    assert alice.get("/api/profile?genre_scope_id=hip-hop").json()["seed_artist_ids"] == original["seed_artist_ids"]
    assert alice.get("/api/catalog/genres").json()[0] == {"id": "all", "name": "Все жанры"}
    assert {g["id"] for g in alice.get("/api/catalog/genres?genre_scope_id=all").json()} == {g.id for g in catalog.genres}
    assert alice.get("/api/profile?genre_scope_id=unknown").status_code == 422


def test_starter_preview_preserves_account_until_explicit_save(tmp_path):
    app = create_app(tmp_path, make_demo_catalog(), allowed_origins=['http://testserver'])
    client, _ = login_client(app, 'preview')
    before = client.get('/api/profile').json()
    preview = client.post('/api/profile/starter?preview=true')
    assert preview.status_code == 200 and preview.json()['onboarded']
    assert client.get('/api/profile').json() == before
    assert client.put('/api/profile', json=preview.json()).status_code == 200
    assert client.get('/api/profile').json() == preview.json()


def test_non_auth_validation_does_not_report_password_error(tmp_path):
    app = create_app(tmp_path, make_demo_catalog(), allowed_origins=['http://testserver'])
    client, _ = login_client(app, 'validation')
    response = client.post('/api/chats', json={})
    assert response.status_code == 422
    assert 'пароль' not in response.json()['detail']
