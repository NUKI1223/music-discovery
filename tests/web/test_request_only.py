"""Explicit artist permissions are account preferences, never global popularity facts."""

import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from discovery.contracts import (
    Artist,
    ArtistPopularityLabel,
    Catalog,
    Genre,
    Release,
    RequestIntent,
    UserProfile,
)
from discovery.llm.budget import BudgetLedger
from discovery.llm.client import OpenRouterClient, OpenRouterSettings
from discovery.ranking.baseline import score_candidates
from discovery.storage import Store
from discovery.web import chat as chat_module
from discovery.web.app import create_app
from discovery.web.chat import (
    create_chat,
    direct_artist_ids,
    get_chat,
    handle_message,
    update_filters,
)
from discovery.web.schemas import ChatCreate, Filters, MessageRequest


@pytest.fixture
def catalog():
    return Catalog(snapshot_id="artist-preferences-test",
                   genres=[Genre(id="hip-hop", name="Hip-hop"), Genre(id="jazz", name="Jazz")],
                   artists=[Artist(id="carti-local-id", name="Playboi Carti"),
                            Artist(id="yeat-local-id", name="Yeat"),
                            Artist(id="other-id", name="Another Artist")],
                   releases=[Release(id=f"{artist}-{i}", title=f"Album {artist} {i}",
                                     artist_ids=[artist], genre_ids=["hip-hop"])
                             for artist in ("carti-local-id", "yeat-local-id", "other-id")
                             for i in range(3)])


def start_chat(store, catalog, filters=None):
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(catalog=catalog)))
    return create_chat(ChatCreate(filters=filters or Filters()), request, store)


def test_core_policy_applies_without_global_ban_and_reference_is_not_permission(catalog):
    profile = UserProfile(request_only_artist_ids=["carti-local-id", "yeat-local-id"])
    intent = RequestIntent(genre_scope_id="hip-hop", reference_artist_ids=["carti-local-id"])
    mine = score_candidates(catalog, intent, profile)
    assert set(mine.candidate_ids) == {"other-id-0", "other-id-1", "other-id-2"}
    assert len(score_candidates(catalog, intent, UserProfile()).candidate_ids) == 9
    requested = intent.model_copy(update={"requested_artist_ids": ["yeat-local-id"]})
    selected = score_candidates(catalog, requested, profile)
    assert selected.candidate_ids and all(rid.startswith("yeat-local-id-") for rid in selected.candidate_ids)
    assert profile.request_only_artist_ids == ["carti-local-id", "yeat-local-id"]
    with pytest.raises(ValueError, match="Unknown explicitly"):
        requested.model_copy(update={"requested_artist_ids": ["unknown"]}).validate_catalog(catalog)
    with pytest.raises(ValueError, match="explicitly excluded"):
        requested.model_copy(update={"excluded_artist_ids": ["yeat-local-id"]}).validate_catalog(catalog)
    # A direct artist request never silently bypasses required rarity evidence.
    assert score_candidates(catalog, requested.model_copy(update={"obscurity": "require"}), profile).items == []


def test_free_path_new_chat_and_explicit_confirmation_preserve_long_term_rule(tmp_path, catalog):
    store = Store(tmp_path / "mine.sqlite3")
    store.set_base_profile(UserProfile(request_only_artist_ids=["carti-local-id", "yeat-local-id"]))
    ident = start_chat(store, catalog).id
    def no_provider():
        pytest.fail("explicit supported request must not call provider")
    first = handle_message(store, ident, MessageRequest(message_id="free-message-1", apply_filters=True), catalog, no_provider)
    assert first.cards and all(card.artists == ["Another Artist"] for card in first.cards)
    fresh = start_chat(store, catalog).id
    proposal = handle_message(store, fresh, MessageRequest(message_id="artist-message-1", text="Покажи альбомы Yeat"), catalog, no_provider)
    assert proposal.proposed_filters.requested_artist_ids == ["yeat-local-id"]
    assert not proposal.cards and not get_chat(store, fresh).filters.requested_artist_ids
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(catalog=catalog)))
    update_filters(fresh, proposal.proposed_filters, request, store)
    picked = handle_message(store, fresh, MessageRequest(message_id="artist-message-2", apply_filters=True), catalog, no_provider)
    assert picked.cards and all(card.artists == ["Yeat"] for card in picked.cards)
    assert store.profile().request_only_artist_ids == ["carti-local-id", "yeat-local-id"]
    next_chat = start_chat(Store(store.path), catalog).id
    again = handle_message(store, next_chat, MessageRequest(message_id="free-message-2", apply_filters=True), catalog)
    assert again.cards and all(card.artists == ["Another Artist"] for card in again.cards)


@pytest.mark.parametrize("text", ["Похожее на Playboi Carti", "Подбери похожее на Yeat",
    "Не предлагай Yeat", "Покажи альбомы не Yeat", "Покажи что-то как Playboi Carti",
    "Люблю Yeat", "Без Playboi Carti", "Show similar to Yeat", "Carti"])
def test_mention_comparison_or_negation_never_counts_as_direct_request(text, catalog):
    assert direct_artist_ids(text, catalog) == []


def test_catalog_names_and_reviewed_aliases_only(tmp_path, catalog, monkeypatch):
    assert direct_artist_ids("Хочу послушать Playboi Carti", catalog) == ["carti-local-id"]
    assert direct_artist_ids("Покажи альбомы YEAT, пожалуйста!", catalog) == ["yeat-local-id"]
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs/artist-aliases.json").write_text(json.dumps({"Carti": "carti-local-id", "Unknown": "not-in-catalog"}))
    monkeypatch.setattr(chat_module, "PROJECT_ROOT", tmp_path)
    assert direct_artist_ids("Покажи альбомы Carti", catalog) == ["carti-local-id"]
    assert direct_artist_ids("Похожее на Carti", catalog) == []
    assert direct_artist_ids("Покажи Unknown", catalog) == []


def test_model_cannot_turn_comparative_request_into_direct_permission(tmp_path, catalog):
    store = Store(tmp_path / "mine.sqlite3")
    store.set_base_profile(UserProfile(request_only_artist_ids=["carti-local-id"]))
    ident = start_chat(store, catalog).id
    action = {"action": "recommend", **Filters(requested_artist_ids=["carti-local-id"]).model_dump(),
              "count": 5, "release_id": None, "unsupported_preferences": [], "clarification": "none"}
    calls = []
    def factory():
        def respond(request):
            calls.append(request)
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(action)}}], "usage": {"cost": 0}})
        settings = OpenRouterSettings(api_key="test-only", model="mock", input_usd_per_million=.1,
                                      output_usd_per_million=.3, request_cost_limit=.01, daily_cost_limit=.1)
        return OpenRouterClient(settings, BudgetLedger(tmp_path / "mock-ledger.sqlite3"),
                                httpx.Client(transport=httpx.MockTransport(respond)))
    result = handle_message(store, ident, MessageRequest(message_id="comparative-message", text="Подбери похожее на Playboi Carti"), catalog, factory)
    assert result.error_code == "EXPLICIT_ARTIST_REQUIRED"
    assert not result.cards and not result.proposed_filters
    assert len(calls) == 1
    assert get_chat(store, ident).filters.requested_artist_ids == []
    assert store.profile().request_only_artist_ids == ["carti-local-id"]


def test_empty_new_core_fields_preserve_legacy_snapshot_serialization():
    assert "requested_artist_ids" not in RequestIntent(genre_scope_id="jazz").model_dump()
    assert "request_only_artist_ids" not in UserProfile().model_dump()
    before = UserProfile().model_dump_json()
    assert UserProfile.model_validate_json(before).model_dump_json() == before


def test_profile_core_update_visible_isolated_and_legacy_form_preserves(tmp_path, catalog):
    app = create_app(tmp_path / "web", catalog, allowed_origins=["http://testserver"])
    clients = []
    for name in ("alice", "bobby"):
        client = TestClient(app, headers={"Origin": "http://testserver"})
        session = client.post("/api/auth/register", json={"username": name, "password": "long-local-password",
                             "password_confirmation": "long-local-password"}).json()
        client.headers["X-CSRF-Token"] = session["csrf_token"]
        clients.append((client, session["user"]["id"]))
    alice, ident = clients[0]
    bob, _ = clients[1]
    store = Store(tmp_path / "web/users" / ident / "discovery.sqlite3")
    store.set_base_profile(UserProfile(request_only_artist_ids=["carti-local-id", "yeat-local-id"]))
    assert alice.get("/api/profile").json()["request_only_artist_ids"] == ["carti-local-id", "yeat-local-id"]
    assert bob.get("/api/profile").json()["request_only_artist_ids"] == []
    old_form = {"onboarded": True, "filters": {"genre_scope_id": "hip-hop"}, "seed_artist_ids": [], "avoid_tag_ids": []}
    assert alice.put("/api/profile", json=old_form).status_code == 200
    assert store.profile().request_only_artist_ids == ["carti-local-id", "yeat-local-id"]
    assert alice.post("/api/profile/starter").json()["request_only_artist_ids"] == ["carti-local-id", "yeat-local-id"]
    assert alice.get("/api/profile?genre_scope_id=jazz").json()["request_only_artist_ids"] == ["carti-local-id", "yeat-local-id"]
    assert alice.put("/api/profile", json={**old_form, "request_only_artist_ids": ["unknown"]}).status_code == 422
    assert alice.put("/api/profile", json={**old_form, "request_only_artist_ids": []}).status_code == 200
    assert store.profile().request_only_artist_ids == []


def test_general_popularity_profile_preserved_by_old_forms_and_starter(tmp_path, catalog):
    app = create_app(tmp_path / "web", catalog, allowed_origins=["http://testserver"])
    with TestClient(app, headers={"Origin": "http://testserver"}) as client:
        session = client.post("/api/auth/register", json={"username": "listener", "password": "long-local-password",
                               "password_confirmation": "long-local-password"}).json()
        client.headers["X-CSRF-Token"] = session["csrf_token"]
        store = Store(tmp_path / "web/users" / session["user"]["id"] / "discovery.sqlite3")
        labels = {"carti-local-id": ArtistPopularityLabel(classification="widely_known", evidence="User explicitly said too popular"),
                  "yeat-local-id": ArtistPopularityLabel(classification="widely_known", evidence="User explicitly said too popular")}
        store.set_base_profile(UserProfile(limit_widely_known_artists=True, max_widely_known_artists=1,
                                          artist_popularity_labels=labels))
        original = client.get("/api/profile").json()
        assert original["limit_widely_known_artists"] is True
        assert original["max_widely_known_artists"] == 1
        assert original["artist_popularity_labels"]["carti-local-id"]["source"] == "explicit_user"
        old_form = {"onboarded": True, "filters": {"genre_scope_id": "hip-hop"}, "seed_artist_ids": [], "avoid_tag_ids": []}
        assert client.put("/api/profile", json=old_form).json()["artist_popularity_labels"] == original["artist_popularity_labels"]
        starter = client.post("/api/profile/starter").json()
        assert starter["limit_widely_known_artists"] is True
        assert starter["max_widely_known_artists"] == 1
        assert store.profile().max_widely_known_artists == 1
        assert client.get("/api/profile?genre_scope_id=jazz").json()["artist_popularity_labels"] == original["artist_popularity_labels"]
        chat_id = client.post("/api/chats", json={"filters": {"genre_scope_id": "hip-hop"}}).json()["id"]
        message = client.post(f"/api/chats/{chat_id}/messages", json={"message_id": "popularity-message", "apply_filters": True}).json()
        assert message["cards"] and all(card["artists"] == ["Another Artist"] for card in message["cards"])
        assert "полную гарантию дать нельзя" in message["text"]
        assert any("не считается малоизвестным автоматически" in warning for warning in message["cards"][0]["warnings"])
        assert client.put("/api/profile", json={**old_form, "limit_widely_known_artists": False}).status_code == 200
        assert store.profile().limit_widely_known_artists is False
        assert store.profile().artist_popularity_labels == labels
        assert store.profile().max_widely_known_artists == 1
        assert client.put("/api/profile", json={**old_form, "max_widely_known_artists": 0}).status_code == 200
        assert store.profile().max_widely_known_artists == 0
        assert client.put("/api/profile", json={**old_form, "max_widely_known_artists": 3}).status_code == 422
        invalid = {"invented": {"classification": "less_known", "source": "explicit_user", "evidence": "User statement"}}
        assert client.put("/api/profile", json={**old_form, "artist_popularity_labels": invalid}).status_code == 422


@pytest.mark.parametrize("text,expected", [
    ("Покажи альбомы плейбой карти", "credit:7a674a97a2044a237e3bc3b0"),
    ("Хочу послушать карти", "credit:7a674a97a2044a237e3bc3b0"),
    ("Дай альбомы йит", "credit:bcb92c2bd027b352fee0cca9"),
])
def test_reviewed_russian_aliases_resolve_only_to_existing_catalog_ids(text, expected):
    catalog = Catalog(snapshot_id="reviewed-catalog-identities", genres=[Genre(id="hip-hop", name="Hip-hop")],
                      artists=[Artist(id="credit:7a674a97a2044a237e3bc3b0", name="Playboi Carti"),
                               Artist(id="credit:bcb92c2bd027b352fee0cca9", name="Yeat")], releases=[])
    assert direct_artist_ids(text, catalog) == [expected]
    assert direct_artist_ids("Не " + text, catalog) == []
    assert direct_artist_ids("Похожее на " + text, catalog) == []
