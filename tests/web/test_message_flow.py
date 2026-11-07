"""Message flow: no free-text LLM bypass, bounded pending state, per-account serialization."""

import json
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from discovery.contracts import Artist, Catalog, Genre, Release
from discovery.storage import Store
from discovery.web import chat as chat_module
from discovery.web.chat import (
    PENDING_TTL_SECONDS,
    claim_message,
    create_chat,
    delete_chat,
    get_chat,
    handle_message,
)
from discovery.web.schemas import ChatCreate, Filters, MessageRequest

ACTION = {"action": "recommend", "requested_artist_ids": [], "reference_artist_ids": [], "excluded_artist_ids": [],
          "preferred_descriptors": [], "avoided_descriptors": [], "genre_scope_id": "hip-hop", "mode": "explore",
          "obscurity": "prefer", "locked_subgenre_ids": [], "exclude_known_artists": False, "count": 5,
          "release_id": None, "unsupported_preferences": [], "clarification": "none"}


@pytest.fixture
def catalog():
    return Catalog(snapshot_id="flow-test", genres=[Genre(id="hip-hop", name="Hip-hop")],
                   artists=[Artist(id=f"a{i}", name=f"Artist {i}", genre_ids=["hip-hop"]) for i in range(8)],
                   releases=[Release(id=f"r{i}", title=f"Album {i}", artist_ids=[f"a{i}"],
                                     genre_ids=["hip-hop"], year=2020) for i in range(8)])


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "user.sqlite3")


def new_chat(store, catalog):
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(catalog=catalog)))
    return create_chat(ChatCreate(filters=Filters()), request, store).id


class FakeClient:
    def __init__(self):
        self.settings = SimpleNamespace(model="test", max_output_tokens=500, input_usd_per_million=.1,
                                        output_usd_per_million=.3, max_input_tokens=100000)

    def new_budget(self):
        return object()

    def complete(self, messages, budget=None, schema=None):
        return {"content": json.dumps(ACTION)}

    def close(self):
        pass


def test_plain_message_calls_factory_exactly_once(store, catalog, monkeypatch):
    ident = new_chat(store, catalog)
    calls = []

    def factory():
        calls.append(1)
        return FakeClient()
    monkeypatch.setattr(chat_module, "fits_input", lambda *a, **k: True)
    result = handle_message(store, ident, MessageRequest(message_id="plain-message-1", text="Подбери хип-хоп"),
                            catalog, factory)
    assert len(calls) == 1 and result.status == "completed"


def test_apply_filters_never_calls_provider_and_ignores_text(store, catalog):
    ident = new_chat(store, catalog)

    def forbidden():
        raise AssertionError("apply_filters must not call the provider")
    result = handle_message(store, ident, MessageRequest(message_id="apply-message-1", text="что угодно",
                                                         apply_filters=True), catalog, forbidden)
    assert result.status == "completed" and len(result.cards) == 5


def test_empty_plain_message_is_input_error_without_provider(store, catalog):
    ident = new_chat(store, catalog)
    result = handle_message(store, ident, MessageRequest(message_id="empty-message-1", text="  "), catalog,
                            lambda: pytest.fail("provider on empty text"))
    assert result.status == "failed" and result.error_code == "INPUT" and result.text == "Введите сообщение."
    assert "бесплатн" not in result.text.casefold()


def test_missing_provider_and_failures_do_not_offer_free_mode(store, catalog):
    ident = new_chat(store, catalog)
    missing = handle_message(store, ident, MessageRequest(message_id="no-provider-1", text="Подбери"), catalog)
    assert missing.error_code == "CONFIG" and "бесплатн" not in missing.text.casefold()

    def broken():
        raise RuntimeError("secret detail")
    failed = handle_message(store, ident, MessageRequest(message_id="broken-message-1", text="Подбери"),
                            catalog, broken)
    assert failed.error_code == "INTERNAL" and "бесплатн" not in failed.text.casefold()
    assert "secret" not in failed.text


def test_use_llm_in_request_body_is_rejected():
    with pytest.raises(ValueError):
        MessageRequest.model_validate({"message_id": "legacy-message-1", "text": "x", "use_llm": False})


def test_use_llm_rejected_with_422_over_http(tmp_path, catalog):
    from fastapi.testclient import TestClient

    from discovery.web.app import create_app

    app = create_app(tmp_path / "web", catalog, allowed_origins=["http://testserver"])
    with TestClient(app, headers={"Origin": "http://testserver"}) as client:
        response = client.post("/api/auth/register", json={"username": "flowuser", "password": "very-long-password",
                                                         "password_confirmation": "very-long-password"})
        client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        ident = client.post("/api/chats", json={"filters": Filters().model_dump()}).json()["id"]
        rejected = client.post(f"/api/chats/{ident}/messages", json={"message_id": "legacy-message-1", "use_llm": False})
        assert rejected.status_code == 422
    assert app.docs_url is None and app.redoc_url is None


def test_final_write_is_retried_once(store, catalog, monkeypatch):
    ident = new_chat(store, catalog)
    original, attempts = chat_module.refresh_memory, []

    def flaky(*args):
        attempts.append(1)
        if len(attempts) == 2:  # call 1 is the pre-context refresh; call 2 is the final write
            raise OSError("database is locked")
        return original(*args)
    monkeypatch.setattr(chat_module, "refresh_memory", flaky)
    result = handle_message(store, ident, MessageRequest(message_id="retry-write-1", apply_filters=True), catalog)
    assert result.status == "completed" and len(attempts) >= 2
    stored = get_chat(store, ident).messages[-1]
    assert stored.status == "completed" and stored.cards


def test_persistent_final_write_failure_propagates(store, catalog, monkeypatch):
    ident = new_chat(store, catalog)

    def broken(*args):
        raise OSError("disk I/O error")
    monkeypatch.setattr(chat_module, "refresh_memory", broken)
    with pytest.raises(OSError):
        handle_message(store, ident, MessageRequest(message_id="lost-write-1", apply_filters=True), catalog)


def age_pending(store, seconds):
    old = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()
    with store.connection() as conn:
        conn.execute("UPDATE web_messages SET payload=json_set(payload,'$.created_at',?) "
                     "WHERE json_extract(payload,'$.status')='pending'", (old,))


def test_stale_pending_is_interrupted_and_does_not_block_new_message(store, catalog):
    ident = new_chat(store, catalog)
    claim_message(store, ident, MessageRequest(message_id="stuck-message-1", text="Ещё"))
    with pytest.raises(HTTPException) as fresh:
        claim_message(store, ident, MessageRequest(message_id="next-message-1", text="Ещё"))
    assert fresh.value.status_code == 409
    age_pending(store, PENDING_TTL_SECONDS + 5)
    result = handle_message(store, ident, MessageRequest(message_id="next-message-1", apply_filters=True), catalog)
    assert result.status == "completed"
    stuck = next(m for m in get_chat(store, ident).messages if m.id == "stuck-message-1")
    assert stuck.status == "interrupted" and stuck.error_code == "INTERRUPTED"


def test_stale_pending_does_not_block_chat_delete(store, catalog):
    ident = new_chat(store, catalog)
    claim_message(store, ident, MessageRequest(message_id="stuck-message-2", text="Ещё"))
    with pytest.raises(HTTPException) as fresh:
        delete_chat(ident, store)
    assert fresh.value.status_code == 409
    age_pending(store, PENDING_TTL_SECONDS + 5)
    assert delete_chat(ident, store).ok is True


def test_get_chat_does_not_write(store, catalog, monkeypatch):
    ident = new_chat(store, catalog)
    handle_message(store, ident, MessageRequest(message_id="read-only-1", apply_filters=True), catalog)
    monkeypatch.setattr(chat_module, "refresh_memory", lambda *a: pytest.fail("GET must not refresh memory"))
    assert len(get_chat(store, ident).messages) == 2


def test_second_parallel_message_of_account_gets_429_but_replay_is_idempotent(tmp_path, catalog, monkeypatch):
    from fastapi.testclient import TestClient

    from discovery.web.app import create_app

    entered, release = threading.Event(), threading.Event()
    original = chat_module.handle_message

    def slow(store, chat_id, body, *args, **kwargs):
        if body.message_id == "slow-message-1":
            entered.set()
            assert release.wait(5)
        return original(store, chat_id, body, *args, **kwargs)
    monkeypatch.setattr(chat_module, "handle_message", slow)
    app = create_app(tmp_path / "web", catalog, allowed_origins=["http://testserver"])
    with TestClient(app, headers={"Origin": "http://testserver"}) as client:
        response = client.post("/api/auth/register", json={"username": "flowuser2", "password": "very-long-password",
                                                         "password_confirmation": "very-long-password"})
        client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        first_chat = client.post("/api/chats", json={"filters": Filters().model_dump()}).json()["id"]
        second_chat = client.post("/api/chats", json={"filters": Filters().model_dump()}).json()["id"]
        done = client.post(f"/api/chats/{first_chat}/messages", json={"message_id": "done-message-1", "apply_filters": True})
        assert done.status_code == 200
        results = {}
        worker = threading.Thread(target=lambda: results.update(first=client.post(
            f"/api/chats/{first_chat}/messages", json={"message_id": "slow-message-1", "apply_filters": True})))
        worker.start()
        assert entered.wait(5)
        busy = client.post(f"/api/chats/{second_chat}/messages", json={"message_id": "other-message-1", "apply_filters": True})
        assert busy.status_code == 429 and busy.json()["detail"] == "Дождитесь ответа на предыдущее сообщение."
        replay = client.post(f"/api/chats/{first_chat}/messages", json={"message_id": "done-message-1", "apply_filters": True})
        assert replay.status_code == 200 and replay.json() == done.json()
        release.set()
        worker.join(10)
        assert results["first"].status_code == 200
        assert client.post(f"/api/chats/{second_chat}/messages", json={"message_id": "other-message-1", "apply_filters": True}).status_code == 200


def test_llm_client_is_bound_to_the_server_chosen_account(tmp_path, catalog, monkeypatch):
    store = Store(tmp_path / "users" / "acct-123" / "discovery.sqlite3")
    ident = new_chat(store, catalog)
    clients = []

    def factory():
        client = FakeClient()
        client.account_id = None
        clients.append(client)
        return client
    monkeypatch.setattr(chat_module, "fits_input", lambda *a, **k: True)
    result = handle_message(store, ident, MessageRequest(message_id="bound-account-1", text="Подбери хип-хоп"),
                            catalog, factory)
    assert result.status == "completed"
    assert [client.account_id for client in clients] == ["acct-123"]
