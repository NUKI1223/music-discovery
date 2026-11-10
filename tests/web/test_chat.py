"""Dialogue evals: grounding, pinned scope, bounded history/cost, durable deduplication.

Deterministic pass criteria: no invented cards, no silent strict-filter changes,
no duplicate calls after retries/restart, no text-generated taste labels, no
cross-account access, and honest uncertainty when popularity is not measured.
"""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from discovery.contracts import (
    Artist,
    Catalog,
    Feedback,
    Genre,
    HistoricalObservation,
    Release,
    RequestIntent,
    UserProfile,
)
from discovery.llm.budget import BudgetLedger
from discovery.llm.client import OpenRouterClient, OpenRouterSettings
from discovery.storage import Store
from discovery.web.cards import collection_cards, safe_link
from discovery.web.chat import (
    claim_message,
    create_chat,
    get_chat,
    handle_message,
    model_messages,
    recover_pending,
    save_feedback,
    shortcut_action,
    update_filters,
)
from discovery.web.schemas import ChatCreate, FeedbackRequest, Filters, MessageRequest


@pytest.fixture
def catalog():
    return Catalog(snapshot_id="web-test", genres=[Genre(id="hip-hop", name="Hip-hop"),
                   Genre(id="jazz", name="Jazz")],
                   artists=[Artist(id=f"a{i}", name=f"Artist {i}", genre_ids=["hip-hop"])
                            for i in range(15)],
                   releases=[Release(id=f"r{i}", title=f"Album {i}", artist_ids=[f"a{i}"],
                                     genre_ids=["hip-hop"], year=2020,
                                     links={"unsafe": "javascript:alert(1)", "source": "https://example.org/album"},
                                     historical_observations=[HistoricalObservation(
                                         source="rym", source_row_id=str(i), provenance_id="test",
                                         rating=3.5, rating_scale=5, rating_count=25)])
                             for i in range(15)])


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "user.sqlite3")


def request(catalog):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(catalog=catalog)))


def chat(store, catalog, filters=None):
    return create_chat(ChatCreate(filters=filters or Filters()), request(catalog), store)


def action(**changes):
    return {"action": "recommend", "requested_artist_ids": [], "reference_artist_ids": [], "excluded_artist_ids": [],
            "preferred_descriptors": [], "avoided_descriptors": [], "genre_scope_id": "hip-hop", "mode": "explore",
            "obscurity": "prefer", "locked_subgenre_ids": [], "exclude_known_artists": False, "count": 5,
            "release_id": None, "unsupported_preferences": [], "clarification": "none", **changes}


class Provider:
    def __init__(self, tmp_path, responses):
        self.calls = []
        self.responses = iter(responses)
        self.budgets = []
        self.ledger = BudgetLedger(tmp_path / "ledger.sqlite3")
        self.settings = OpenRouterSettings(api_key="TEST_ONLY", model="test", input_usd_per_million=.1,
                                           output_usd_per_million=.3, request_cost_limit=.01,
                                           daily_cost_limit=.10)

    def factory(self):
        def respond(req):
            self.calls.append(json.loads(req.content))
            value = next(self.responses)
            if isinstance(value, Exception):
                raise value
            if isinstance(value, httpx.Response):
                return value
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(value)}}],
                                            "usage": {"cost": .0001}})
        client = OpenRouterClient(self.settings, self.ledger,
                                  httpx.Client(transport=httpx.MockTransport(respond)), sleep=lambda _: None)
        original = client.complete

        def complete(*args, **kwargs):
            self.budgets.append(kwargs["budget"])
            return original(*args, **kwargs)
        client.complete = complete
        return client


def test_free_selection_persists_excludes_shown_and_never_calls_provider(store, catalog):
    ident = chat(store, catalog).id
    def forbidden():
        raise AssertionError("free recommendation must not call provider")
    first = handle_message(store, ident, MessageRequest(message_id="message-1", apply_filters=True),
                           catalog, forbidden)
    assert first.status == "completed" and len(first.cards) == 5
    second = handle_message(store, ident, MessageRequest(message_id="message-2", apply_filters=True), catalog)
    assert not {c.release_id for c in first.cards} & {c.release_id for c in second.cards}
    assert len(get_chat(Store(store.path), ident).messages) == 4
    assert not store.feedback()
    assert "неизвестна" in first.cards[0].warnings[0]
    assert all(link.url.startswith("https:") for link in first.cards[0].links)
    assert first.cards[0].historical_ratings[0].scale == 5


def test_llm_more_and_surprise_context_is_grounded(store, catalog, tmp_path):
    ident = chat(store, catalog).id
    provider = Provider(tmp_path, [action(), action(mode="surprise")])
    first = handle_message(store, ident, MessageRequest(message_id="message-1", text="Подбери хип-хоп"),
                           catalog, provider.factory)
    second = handle_message(store, ident, MessageRequest(message_id="message-2", text="Ещё, более неожиданное"),
                            catalog, provider.factory)
    assert first.status == second.status == "completed"
    assert not {c.release_id for c in first.cards} & {c.release_id for c in second.cards}
    assert first.cards[0].release_id in provider.calls[1]["messages"][0]["content"]
    assert store.decisions()[-1].request.mode == "surprise"
    assert store.decisions()[-1].request.genre_scope_id == "hip-hop"
    assert not store.feedback()


def test_repeat_payload_and_account_wide_key_conflict(store, catalog, tmp_path):
    ident = chat(store, catalog).id
    provider = Provider(tmp_path, [action()])
    body = MessageRequest(message_id="message-1", text="Ещё")
    result = handle_message(store, ident, body, catalog, provider.factory)
    assert handle_message(store, ident, body, catalog, provider.factory) == result
    assert len(provider.calls) == 1 and len(get_chat(store, ident).messages) == 2
    with pytest.raises(HTTPException) as error:
        handle_message(store, ident, body.model_copy(update={"text": "Изменено"}), catalog, provider.factory)
    assert error.value.status_code == 409
    with pytest.raises(HTTPException):
        handle_message(store, chat(store, catalog).id, body, catalog, provider.factory)


@pytest.mark.parametrize("role", ["user", "assistant"])
def test_visible_message_id_collision_returns_conflict_before_provider(store, catalog, role):
    ident = chat(store, catalog).id
    handle_message(store, ident, MessageRequest(message_id="message-1", apply_filters=True), catalog)
    history = get_chat(store, ident)
    occupied = next(message.id for message in history.messages if message.role == role)
    body = MessageRequest(message_id=occupied, text="Другой запрос")
    with pytest.raises(HTTPException) as error:
        handle_message(store, ident, body, catalog, lambda: pytest.fail("paid call on ID conflict"))
    assert error.value.status_code == 409
    assert get_chat(store, ident) == history


def test_concurrent_duplicate_claim_only_calls_once(store, catalog, tmp_path):
    ident = chat(store, catalog).id
    entered, release = threading.Event(), threading.Event()
    provider = Provider(tmp_path, [action()])
    def factory():
        entered.set()
        assert release.wait(5)
        return provider.factory()
    body = MessageRequest(message_id="message-1", text="Ещё")
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(handle_message, store, ident, body, catalog, factory)
        assert entered.wait(5)
        result = handle_message(store, ident, body, catalog, factory)
        assert result.status == "pending"
        with pytest.raises(HTTPException):
            handle_message(store, ident, body.model_copy(update={"message_id": "message-2"}), catalog, factory)
        with pytest.raises(HTTPException):
            update_filters(ident, Filters(mode="close"), request(catalog), store)
        release.set()
        assert future.result().status == "completed"
    assert len(provider.calls) == 1


def test_pending_restart_never_retries(store, catalog):
    ident = chat(store, catalog).id
    body = MessageRequest(message_id="message-1", text="Ещё")
    claim_message(store, ident, body)
    recover_pending(Store(store.path))
    message = handle_message(store, ident, body, catalog, lambda: pytest.fail("retry after restart"))
    assert message.status == "interrupted" and message.error_code == "INTERRUPTED"
    assert len(get_chat(store, ident).messages) == 2


@pytest.mark.parametrize("changes", [{"genre_scope_id": "jazz"}, {"obscurity": "require"},
                                    {"exclude_known_artists": True}])
def test_strict_filter_change_is_proposal_not_mutation(store, catalog, tmp_path, changes):
    ident = chat(store, catalog).id
    provider = Provider(tmp_path, [action(**changes)])
    result = handle_message(store, ident, MessageRequest(message_id="message-1", text="Измени фильтры"),
                            catalog, provider.factory)
    assert result.proposed_filters and not result.cards
    assert get_chat(store, ident).filters == Filters()
    assert not store.decisions()
    update_filters(ident, result.proposed_filters, request(catalog), store)
    assert get_chat(store, ident).filters == result.proposed_filters


def test_explanation_uses_prior_card_catalog_facts(store, catalog, tmp_path):
    ident = chat(store, catalog).id
    first = handle_message(store, ident, MessageRequest(message_id="message-1", apply_filters=True), catalog)
    selected = first.cards[1]
    provider = Provider(tmp_path, [action(action="explain", release_id=selected.release_id)])
    result = handle_message(store, ident, MessageRequest(message_id="message-2", text="Почему второй?"),
                            catalog, provider.factory)
    assert result.cards == [selected]
    assert selected.title in result.text and "неизвестна" in result.text
    assert len(store.decisions()) == 1


def test_invented_release_rejected_and_repairs_share_budget(store, catalog, tmp_path):
    ident = chat(store, catalog).id
    provider = Provider(tmp_path, [action(action="explain", release_id="invented")] * 2)
    result = handle_message(store, ident, MessageRequest(message_id="message-1", text="Почему альбом?"),
                            catalog, provider.factory)
    assert result.status == "failed" and result.error_code == "INVALID_SCHEMA"
    assert len(provider.calls) == 2 and provider.budgets[0] is provider.budgets[1]
    assert provider.ledger.total() <= .01
    assert not result.cards and not store.decisions()


def test_unsupported_wishes_are_visible_and_do_not_change_taste(store, catalog, tmp_path):
    ident = chat(store, catalog).id
    provider = Provider(tmp_path, [action(unsupported_preferences=["только инструментальное"])])
    result = handle_message(store, ident, MessageRequest(message_id="message-1", text="Люблю инструментальное"),
                            catalog, provider.factory)
    assert "не поддерживаются" in result.text and "инструментальное" in result.text
    assert not result.cards and not store.feedback() and not store.decisions()


def test_sound_comparison_proposes_catalog_genres_then_recommends_after_confirmation(store, catalog, tmp_path):
    catalog.genres.append(Genre(id="industrial-hip-hop", name="Industrial hip-hop", parent_ids=["hip-hop"]))
    catalog.artists[0].name = "Death Grips"
    catalog.artists[0].genre_ids = ["industrial-hip-hop", "jazz"]
    for release in catalog.releases[:6]:
        release.genre_ids = ["industrial-hip-hop"]
    ident = chat(store, catalog, Filters(exclude_known_artists=True)).id
    provider = Provider(tmp_path, [action(locked_subgenre_ids=["industrial-hip-hop"], exclude_known_artists=True)])
    body = MessageRequest(message_id="sound-wish", text="Нужно больше жесткости в звучании, как например у Death Grips")
    result = handle_message(store, ident, body, catalog, provider.factory)
    assert result.status == "completed" and result.proposed_filters
    assert "Industrial hip-hop" in result.text and "не гарантируют" in result.text
    assert result.proposed_filters.exclude_known_artists
    assert not get_chat(store, ident).filters.locked_subgenre_ids
    assert not result.cards and not store.decisions() and not store.feedback()
    context = json.loads('{"filters":' + provider.calls[0]["messages"][0]["content"].split('{"filters":', 1)[1])
    assert context["mentioned_artists"] == [{"id": "a0", "name": "Death Grips", "genre_ids": ["industrial-hip-hop"]}]
    assert handle_message(store, ident, body, catalog, provider.factory) == result
    assert len(provider.calls) == 1
    update_filters(ident, result.proposed_filters, request(catalog), store)
    selection = handle_message(store, ident, MessageRequest(message_id="confirmed-sound", apply_filters=True), catalog)
    assert len(selection.cards) == 5
    assert {card.release_id for card in selection.cards} <= {release.id for release in catalog.releases[:6]}
    assert not store.feedback()


def test_unmapped_sound_wish_asks_for_reference_without_changing_filters(store, catalog, tmp_path):
    ident = chat(store, catalog).id
    provider = Provider(tmp_path, [action(action="clarify", clarification="ask_preferences")])
    result = handle_message(store, ident, MessageRequest(message_id="unclear-sound", text="Хочу что-то совсем иное по ощущению"),
                            catalog, provider.factory)
    assert "исполнителя для ориентира" in result.text
    assert not result.cards and not result.proposed_filters
    assert get_chat(store, ident).filters == Filters() and not store.feedback()


def test_daily_budget_and_network_failure_are_persisted_no_automatic_replay(store, catalog, tmp_path):
    ident = chat(store, catalog).id
    provider = Provider(tmp_path, [httpx.ConnectError("secret-provider-details")] * 2)
    body = MessageRequest(message_id="message-1", text="Ещё")
    result = handle_message(store, ident, body, catalog, provider.factory)
    assert result.status == "failed" and len(provider.calls) == 2
    assert "secret" not in result.text
    assert handle_message(store, ident, body, catalog, provider.factory) == result
    provider.ledger.reserve(.09, .10)
    provider.ledger.reserve(.007, .10)
    result = handle_message(store, ident, body.model_copy(update={"message_id": "message-2"}),
                            catalog, provider.factory)
    assert result.error_code == "DAILY_BUDGET"
    assert len(provider.calls) == 2
    assert handle_message(store, ident, MessageRequest(message_id="message-3", apply_filters=True), catalog).cards


def test_context_at_most_six_exchanges_and_trims_old_messages(store, catalog, tmp_path):
    ident = chat(store, catalog).id
    for i in range(9):
        handle_message(store, ident, MessageRequest(message_id=f"message-{i}", apply_filters=True,
                                                  text=f"exchange-{i} " + "текст " * 200), catalog)
    body = MessageRequest(message_id="message-9", text="Ещё")
    claim_message(store, ident, body)
    detail = get_chat(store, ident)
    provider = Provider(tmp_path, [])
    client = provider.factory()
    messages = model_messages(detail, body, catalog, client)
    assert len(messages) <= 14
    assert "exchange-0" not in json.dumps(messages[1:])
    assert detail.memory.summarized_messages > 0
    assert len(json.dumps(messages, ensure_ascii=False).encode()) < client.settings.max_input_bytes
    assert len(get_chat(store, ident).messages) == 20


def test_feedback_idempotency_collection_and_explicit_listening_only(store, catalog):
    ident = chat(store, catalog).id
    card = handle_message(store, ident, MessageRequest(message_id="message-1", apply_filters=True), catalog).cards[0]
    body = FeedbackRequest(feedback_id="feedback-1", decision_id=card.decision_id,
                           release_id=card.release_id, kind="save_for_later")
    save_feedback(body, store)
    save_feedback(body, store)
    assert len(store.feedback()) == 1
    assert not store.profile().liked_release_ids_by_genre
    assert collection_cards(store)[0].feedback == "save_for_later"
    save_feedback(body.model_copy(update={"feedback_id": "feedback-2", "kind": "listened_like"}), store)
    assert card.release_id in store.profile().liked_release_ids_by_genre["hip-hop"]
    with pytest.raises(HTTPException):
        save_feedback(body.model_copy(update={"kind": "known"}), store)
    assert collection_cards(Store(store.path))[0].historical_ratings[0].scale == 5


def test_another_account_cannot_read_or_rate_guessed_ids(store, catalog, tmp_path):
    ident = chat(store, catalog).id
    card = handle_message(store, ident, MessageRequest(message_id="message-1", apply_filters=True), catalog).cards[0]
    stranger = Store(tmp_path / "stranger.sqlite3")
    with pytest.raises(HTTPException) as error:
        get_chat(stranger, ident)
    assert error.value.status_code == 404
    with pytest.raises(HTTPException):
        handle_message(stranger, ident, MessageRequest(message_id="message-2", apply_filters=True), catalog)
    with pytest.raises(HTTPException):
        save_feedback(FeedbackRequest(feedback_id="feedback-1", decision_id=card.decision_id,
                                      release_id=card.release_id, kind="listened_like"), stranger)
    assert collection_cards(stranger) == []


@pytest.mark.parametrize("url", ["javascript:alert(1)", "//example.com", "https://user:pass@example.com",
                                  "https://example.org/\nsecret", "http://[broken"])
def test_unsafe_links_omitted(url):
    assert not safe_link(url)


def test_surprise_mode_persists_and_explanation_does_not_replace_last_selection(store, catalog, tmp_path):
    ident = chat(store, catalog).id
    provider = Provider(tmp_path, [action(mode="surprise")])
    first = handle_message(store, ident, MessageRequest(message_id="message-1", text="Подбери более неожиданную музыку"),
                           catalog, provider.factory)
    assert get_chat(store, ident).filters.mode == "surprise"
    selected = first.cards[2]
    provider.responses = iter([action(action="explain", release_id=selected.release_id, mode="surprise")])
    handle_message(store, ident, MessageRequest(message_id="message-2", text="Почему третий?"),
                   catalog, provider.factory)
    provider.responses = iter([action(mode="surprise")])
    handle_message(store, ident, MessageRequest(message_id="message-3", text="Предложи ещё несколько вариантов"),
                   catalog, provider.factory)
    context = provider.calls[-1]["messages"][0]["content"]
    for card in first.cards:
        assert card.release_id in context
    assert store.decisions()[-1].request.mode == "surprise"


def test_small_request_budget_stops_before_network(store, catalog, tmp_path):
    from dataclasses import replace
    ident = chat(store, catalog).id
    provider = Provider(tmp_path, [])
    provider.settings = replace(provider.settings, request_cost_limit=.00001)
    result = handle_message(store, ident, MessageRequest(message_id="message-1", text="Подбери хип-хоп"),
                            catalog, provider.factory)
    assert result.error_code == "REQUEST_BUDGET"
    assert not provider.calls and provider.ledger.total() == 0


def test_api_conversation_survives_app_restart_and_duplicate_delivery(tmp_path, catalog):
    from fastapi.testclient import TestClient

    from discovery.web.app import create_app
    from discovery.web.auth import COOKIE

    app = create_app(tmp_path / "web", catalog, allowed_origins=["http://testserver"])
    with TestClient(app, headers={"Origin": "http://testserver"}) as client:
        response = client.post("/api/auth/register", json={"username": "chatuser", "password": "very-long-password",
                                                         "password_confirmation": "very-long-password"})
        assert response.status_code == 200
        client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        cookie = client.cookies.get(COOKIE)
        created = client.post("/api/chats", json={"filters": Filters().model_dump()})
        assert created.status_code == 200 and created.json()["messages"] == []
        ident = created.json()["id"]
        body = {"message_id": "message-1", "apply_filters": True}
        message = client.post(f"/api/chats/{ident}/messages", json=body)
        assert message.status_code == 200 and len(message.json()["cards"]) == 5
        assert client.post(f"/api/chats/{ident}/messages", json=body).json() == message.json()
        card = message.json()["cards"][0]
        assert client.post("/api/feedback", json={"feedback_id": "feedback-1", "decision_id": card["decision_id"],
                                               "release_id": card["release_id"], "kind": "listened_like"}).status_code == 200
    restarted = create_app(tmp_path / "web", catalog, allowed_origins=["http://testserver"])
    with TestClient(restarted, headers={"Origin": "http://testserver"}) as client:
        client.cookies.set(COOKIE, cookie)
        history = client.get(f"/api/chats/{ident}").json()
        assert len(history["messages"]) == 2
        assert history["messages"][1]["cards"][0]["feedback"] == "listened_like"
        assert client.get("/api/collection").json()[0]["release_id"] == card["release_id"]


def test_personal_artist_exclusions_cover_other_albums_and_old_snapshots(store, catalog):
    from discovery.service import recommend

    store.set_base_profile(UserProfile(seed_artist_ids_by_genre={"hip-hop": ["a0"], "jazz": ["a1"]},
                                       known_release_ids=["r2"]))
    decision = recommend(store, catalog, RequestIntent(genre_scope_id="hip-hop", count=2,
        excluded_release_ids=[r.id for r in catalog.releases if r.id not in {"r3", "r4"}]))
    store.append_feedback(Feedback(id="heard", decision_id=decision.id, release_id="r3", kind="listened_like"))
    store.append_feedback(Feedback(id="saved", decision_id=decision.id, release_id="r4", kind="save_for_later"))
    # The heard album disappeared from the new snapshot; the artist has another album.
    current = catalog.model_copy(deep=True)
    current.releases = [r for r in current.releases if r.id != "r3"] + [
        catalog.releases[i].model_copy(update={"id": f"other-{i}"}) for i in range(5)]
    ident = chat(store, current, Filters(exclude_known_artists=True)).id
    result = handle_message(store, ident, MessageRequest(message_id="new-artists", apply_filters=True), current)
    assert result.status == "completed" and result.cards
    latest = store.decisions()[-1]
    assert set(latest.request.excluded_artist_ids) == {"a0", "a1", "a2", "a3"}
    artists = {r.id: r.artist_ids for r in current.releases}
    assert all(not set(artists[c.release_id]) & {"a0", "a1", "a2", "a3"} for c in result.cards)
    assert "other-4" in latest.result.candidate_ids  # Saving is not familiarity.
    assert len(store.feedback()) == 2
    assert get_chat(Store(store.path), ident).filters.exclude_known_artists


def test_known_artist_request_and_surprise_after_prior_refusal(store, catalog, tmp_path):
    store.set_base_profile(UserProfile(seed_artist_ids_by_genre={"hip-hop": ["a0"]}))
    ident = chat(store, catalog).id
    provider = Provider(tmp_path, [action(unsupported_preferences=["исключить известных исполнителей"])])
    handle_message(store, ident, MessageRequest(message_id="old-refusal", text="Только незнакомые мне артисты"),
                   catalog, provider.factory)
    provider.responses = iter([action(exclude_known_artists=True)])
    proposed = handle_message(store, ident, MessageRequest(message_id="request-known", text="Мне не нужны альбомы тех исполнителей, которых я знаю"),
                              catalog, provider.factory)
    assert proposed.proposed_filters.exclude_known_artists and not proposed.cards
    assert "Могу исключить" in proposed.text
    assert get_chat(store, ident).filters.exclude_known_artists is False
    update_filters(ident, proposed.proposed_filters, request(catalog), store)
    provider.responses = iter([action(mode="surprise", exclude_known_artists=True)])
    result = handle_message(store, ident, MessageRequest(message_id="new-surprise", text="Более неожиданное"),
                            catalog, provider.factory)
    assert result.status == "completed" and result.cards
    assert "a0" in store.decisions()[-1].request.excluded_artist_ids
    assert store.decisions()[-1].request.mode == "surprise"
    assert not store.feedback()
    # Removing the confirmed constraint also needs confirmation.
    provider.responses = iter([action(exclude_known_artists=False)])
    removal = handle_message(store, ident, MessageRequest(message_id="remove-known", text="Можно и знакомых"),
                             catalog, provider.factory)
    assert removal.proposed_filters.exclude_known_artists is False
    assert get_chat(store, ident).filters.exclude_known_artists is True


@pytest.mark.parametrize("text", ["Более неожиданное", "Ещё варианты",
    "Мне не нужны альбомы тех исполнителей, которых я знаю"])
def test_supported_shortcuts_do_not_need_provider_or_inherit_refusal(store, catalog, text):
    ident = chat(store, catalog).id
    def forbidden():
        pytest.fail("a supported shortcut must not call the provider")
    result = handle_message(store, ident, MessageRequest(message_id="shortcut-test", text=text),
                            catalog, forbidden)
    assert result.status == "completed"
    assert result.cards or result.proposed_filters


@pytest.mark.parametrize("text", ["Более неожиданное, но только 2025 года",
    "Ещё варианты, кроме Drake", "Не исключи знакомых исполнителей"])
def test_shortcuts_never_drop_additional_constraints(text):
    assert shortcut_action(Filters(), MessageRequest(message_id="qualifier-test", text=text)) is None


def test_prior_unsupported_exchange_not_sent_as_standing_preference(store, catalog, tmp_path):
    ident = chat(store, catalog).id
    provider = Provider(tmp_path, [action(unsupported_preferences=["исключить известных исполнителей"])])
    handle_message(store, ident, MessageRequest(message_id="old-refusal", text="Старое пожелание"),
                   catalog, provider.factory)
    body = MessageRequest(message_id="next-message", text="Найди другие интересные варианты")
    claim_message(store, ident, body)
    messages = model_messages(get_chat(store, ident), body, catalog, provider.factory())
    assert len(messages) == 2
    assert messages[-1]["content"] == body.text


def test_unknown_personal_familiarity_does_not_become_popularity_filter(store, catalog):
    ident = chat(store, catalog, Filters(exclude_known_artists=True)).id
    result = handle_message(store, ident, MessageRequest(message_id="empty-known", apply_filters=True), catalog)
    assert result.cards and "Пока нет отмеченных" in result.text
    assert store.decisions()[-1].request.obscurity == "prefer"
    assert not store.decisions()[-1].request.excluded_artist_ids


def test_all_known_artists_yields_honest_empty_selection(store, catalog):
    store.set_base_profile(UserProfile(seed_artist_ids_by_genre={"hip-hop": [a.id for a in catalog.artists]}))
    ident = chat(store, catalog, Filters(exclude_known_artists=True)).id
    result = handle_message(store, ident, MessageRequest(message_id="all-known", apply_filters=True), catalog)
    assert result.status == "completed" and not result.cards
    assert "Новых релизов под эти условия не осталось" in result.text


def test_request_context_requires_confirmation_survives_followup_without_learning(store, catalog, tmp_path):
    from discovery.contracts import SourceMetadata
    catalog.releases[1].source_metadata = [SourceMetadata(source="rym", source_row_id="sound-row",
        title="Album 1", artist_credit="Artist 1", descriptors=["aggressive", "melodic"])]
    profile = UserProfile(seed_artist_ids_by_genre={"hip-hop": ["a7"]})
    store.set_base_profile(profile)
    profile = store.profile()
    ident = chat(store, catalog).id
    provider = Provider(tmp_path, [action(reference_artist_ids=["a0"], excluded_artist_ids=["a0"],
                                         preferred_descriptors=["aggressive"], avoided_descriptors=["melodic"])])
    body = MessageRequest(message_id="request-context", text="Похожие на Artist 0, но поменьше мелодичности")
    proposal = handle_message(store, ident, body, catalog, provider.factory)
    assert proposal.proposed_filters and not proposal.cards
    assert not get_chat(store, ident).filters.reference_artist_ids
    update_filters(ident, proposal.proposed_filters, request(catalog), store)
    first = handle_message(store, ident, MessageRequest(message_id="confirmed-context", apply_filters=True), catalog)
    assert first.cards and all("Artist 0" not in c.artists for c in first.cards)
    persisted = Store(store.path)
    next_result = handle_message(persisted, ident, MessageRequest(message_id="context-followup", text="Ещё варианты"), catalog)
    assert not ({c.release_id for c in first.cards} & {c.release_id for c in next_result.cards})
    assert store.decisions()[-1].request.reference_artist_ids == ["a0"]
    assert store.decisions()[-1].request.avoided_descriptors == ["melodic"]
    assert store.profile() == profile
    assert not store.feedback()
    assert len(provider.calls) == 1
    other = chat(store, catalog).id
    assert not get_chat(store, other).filters.reference_artist_ids


def test_invalid_explicit_artist_exclusion_rejected_by_filters(store, catalog):
    ident = chat(store, catalog).id
    with pytest.raises(HTTPException) as error:
        update_filters(ident, Filters(excluded_artist_ids=["unknown"]), request(catalog), store)
    assert error.value.status_code == 422


def test_misspelled_punctuation_in_artist_name_still_supplies_id(store, catalog, tmp_path):
    catalog.artists[0].name = "Tyler, the Creator"
    provider = Provider(tmp_path, [action(reference_artist_ids=["a0"])])
    handle_message(store, chat(store, catalog).id,
                   MessageRequest(message_id="punctuation-case", text="Похожее на Tyler The Creator"),
                   catalog, provider.factory)
    context = json.loads('{"filters":' + provider.calls[0]["messages"][0]["content"].split('{"filters":', 1)[1])
    assert context["mentioned_artists"][0]["id"] == "a0"


def test_free_scope_dialogue_is_grounded_idempotent_and_narrowing_needs_confirmation(store, catalog, tmp_path):
    ident = chat(store, catalog, Filters(genre_scope_id="all")).id
    provider = Provider(tmp_path, [action(genre_scope_id="all"), action(genre_scope_id="jazz")])
    first = handle_message(store, ident, MessageRequest(message_id="free-message-1", text="Посоветуй что-нибудь интересное"), catalog, provider.factory)
    assert first.status == "completed" and len(first.cards) == 5
    assert first.proposed_filters is None
    assert all("среди всех жанров" in c.explanation[0] for c in first.cards)
    system = provider.calls[0]["messages"][0]["content"]
    assert '"id": "all"' in system
    assert "without requiring a genre" in system
    same = handle_message(store, ident, MessageRequest(message_id="free-message-1", text="Посоветуй что-нибудь интересное"), catalog, provider.factory)
    assert same == first and len(provider.calls) == 1
    second = handle_message(store, ident, MessageRequest(message_id="free-message-2", text="А теперь джаз"), catalog, provider.factory)
    assert second.proposed_filters.genre_scope_id == "jazz"
    assert get_chat(store, ident).filters.genre_scope_id == "all"
    assert not second.cards
    assert not store.feedback()


def test_free_scope_command_proposes_broadening_and_preserves_other_constraints(store, catalog):
    ident = chat(store, catalog, Filters(genre_scope_id="hip-hop", obscurity="require", exclude_known_artists=True)).id
    message = handle_message(store, ident, MessageRequest(message_id="free-command", text="Все жанры"), catalog)
    assert message.status == "completed"
    assert message.proposed_filters.genre_scope_id == "all"
    assert message.proposed_filters.obscurity == "require"
    assert message.proposed_filters.exclude_known_artists
    assert message.proposed_filters.locked_subgenre_ids == []
    assert get_chat(store, ident).filters.genre_scope_id == "hip-hop"


def test_free_feedback_persists_and_is_excluded_in_another_scoped_chat(store, catalog):
    ident = chat(store, catalog, Filters(genre_scope_id="all")).id
    first = handle_message(store, ident, MessageRequest(message_id="free-first", apply_filters=True), catalog)
    card = first.cards[0]
    save_feedback(FeedbackRequest(feedback_id="free-known", decision_id=card.decision_id, release_id=card.release_id, kind="known"), store)
    reopened = Store(store.path)
    local = chat(reopened, catalog, Filters(genre_scope_id="hip-hop")).id
    other = handle_message(reopened, local, MessageRequest(message_id="local-after-free", apply_filters=True), catalog)
    assert card.release_id not in {c.release_id for c in other.cards}
    assert card.release_id in reopened.profile().known_release_ids


def genre_catalog():
    """More than 30 genres: 4 top-level, the rest sub-genres, with some non-derivable names."""
    genres = [Genre(id="rock", name="Rock"), Genre(id="jazz", name="Jazz"),
              Genre(id="hardcore", name="Hardcore hip-hop", parent_ids=["rock"]),
              Genre(id="rnb", name="R&B", parent_ids=["rock"])]
    genres += [Genre(id=f"aaa-style-{i:02d}", name=f"Aaa Style {i:02d}", parent_ids=["rock"]) for i in range(35)]
    genres.append(Genre(id="gothic-rock", name="Gothic Rock", parent_ids=["rock"]))
    genres.append(Genre(id="zzz-metal", name="Zzz Metal", parent_ids=["rock"]))
    return Catalog(snapshot_id="genres-test", genres=genres,
                   artists=[Artist(id="a0", name="Artist 0", genre_ids=["rock"])],
                   releases=[Release(id="r0", title="Album 0", artist_ids=["a0"], genre_ids=["rock"], year=2020)])


def genre_context(store, tmp_path, text, limits=None, scope="all"):
    catalog = genre_catalog()
    ident = chat(store, catalog, Filters(genre_scope_id=scope)).id
    body = MessageRequest(message_id=f"genre-message-{ident}", text=text)
    claim_message(store, ident, body)
    client = Provider(tmp_path, []).factory()
    if limits:
        client.settings = client.settings.__class__(**{**client.settings.__dict__, **limits})
    messages = model_messages(get_chat(store, ident), body, catalog, client)
    system = messages[0]["content"]
    context = json.loads('{"filters":' + system.split('{"filters":', 1)[1])
    return catalog, client, messages, system, context


def test_russian_request_still_offers_late_subgenre_and_covers_catalog_once(store, tmp_path):
    catalog, _, _, _, context = genre_context(store, tmp_path, "Хочу какой нибудь готичный рок")
    assert "gothic-rock" in context["other_genre_ids"]
    listed = [g["id"] for g in context["genre_vocabulary"] if g["id"] != "all"] + context["other_genre_ids"]
    assert len(listed) == len(set(listed))
    assert set(listed) == {g.id for g in catalog.genres}


def test_non_derivable_genre_names_are_sent_with_names(store, tmp_path):
    _, _, _, _, context = genre_context(store, tmp_path, "Хочу что-нибудь")
    names = {g["id"]: g["name"] for g in context["genre_vocabulary"]}
    assert names["hardcore"] == "Hardcore hip-hop" and names["rnb"] == "R&B"
    assert "hardcore" not in context["other_genre_ids"] and "rnb" not in context["other_genre_ids"]
    assert "aaa-style-34" in context["other_genre_ids"]


def test_english_named_genre_stays_prioritized(store, tmp_path):
    _, _, _, _, context = genre_context(store, tmp_path, "something like zzz metal please")
    ids = [g["id"] for g in context["genre_vocabulary"]]
    assert "zzz-metal" in ids[:6]
    assert "zzz-metal" not in context["other_genre_ids"]


def test_instruction_mentions_other_genre_ids(store, tmp_path):
    _, _, _, system, _ = genre_context(store, tmp_path, "Привет")
    assert "other_genre_ids" in system.split('{"filters":', 1)[0]


def test_tight_limit_trims_other_genre_ids_before_vocabulary(store, tmp_path):
    from discovery.llm.context import fits_input
    _, _, _, _, full = genre_context(store, tmp_path, "Привет")
    # Tighten the token limit until the first trimming of the genre lists happens.
    for limit in range(8192, 1000, -25):
        _, client, messages, _, context = genre_context(store, tmp_path, "Привет", {"max_input_tokens": limit})
        assert fits_input({"messages": messages}, client.settings)
        if context["other_genre_ids"] != full["other_genre_ids"]:
            break
    assert full["other_genre_ids"] and len(context["other_genre_ids"]) < len(full["other_genre_ids"])
    assert context["genre_vocabulary"] == full["genre_vocabulary"]
    assert context["other_genre_ids"] == full["other_genre_ids"][:len(context["other_genre_ids"])]


def test_review_text_never_reaches_model_messages(store, catalog, tmp_path):
    ident = chat(store, catalog).id
    card = handle_message(store, ident, MessageRequest(message_id="message-1", apply_filters=True), catalog).cards[0]
    secret = "СЕКРЕТНАЯ-РЕЦЕНЗИЯ-12345"
    save_feedback(FeedbackRequest(feedback_id="feedback-1", decision_id=card.decision_id,
                                  release_id=card.release_id, kind="listened_mixed",
                                  score=33, review=secret), store)
    detail = get_chat(store, ident)
    assert detail.messages[-1].cards[0].review == secret
    body = MessageRequest(message_id="message-2", text="Ещё что-нибудь")
    claim_message(store, ident, body)
    provider = Provider(tmp_path, [])
    messages = model_messages(get_chat(store, ident), body, catalog, provider.factory(),
                              references=detail.messages[-1].cards, last_cards=detail.messages[-1].cards)
    assert secret not in json.dumps(messages, ensure_ascii=False)
