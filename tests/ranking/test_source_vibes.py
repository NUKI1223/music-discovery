"""Source descriptions work across genres without inventing acoustic/vocal evidence."""

import json
from types import SimpleNamespace

import httpx
import pytest

from discovery.contracts import (
    Artist,
    Catalog,
    Genre,
    Release,
    RequestIntent,
    SourceMetadata,
    UserProfile,
)
from discovery.llm.budget import BudgetLedger
from discovery.llm.client import OpenRouterClient, OpenRouterSettings
from discovery.ranking import score_candidates
from discovery.ranking.source_descriptors import source_descriptors
from discovery.service import recommend
from discovery.storage import Store
from discovery.web.cards import decision_cards
from discovery.web.chat import (
    claim_message,
    create_chat,
    get_chat,
    handle_message,
    model_messages,
    update_filters,
)
from discovery.web.schemas import ChatCreate, Filters, MessageRequest


@pytest.fixture
def catalog():
    artists = []
    releases = []
    for scope in ("jazz", "electronic"):
        for index, vibes in enumerate((["Energetic", "warm"], ["mellow", "Noisy"], [])):
            aid, rid = f"{scope}-artist-{index}", f"{scope}-release-{index}"
            artists.append(Artist(id=aid, name=f"Fixture {scope} artist {index}", genre_ids=[scope]))
            releases.append(Release(id=rid, title=f"Fixture {scope} release {index}",
                artist_ids=[aid], genre_ids=[scope], links={"AOTY": f"https://www.albumoftheyear.org/album/{100 + index if scope == 'jazz' else 200 + index}-{scope}.php"},
                source_metadata=[SourceMetadata(source="aoty", source_row_id=f"row-{rid}",
                    title=f"Fixture {scope} release {index}", artist_credit=f"Fixture {scope} artist {index}",
                    vibes=vibes)] if vibes else []))
    return Catalog(snapshot_id="source-vibes-fixture", is_fixture=True,
                   genres=[Genre(id="jazz", name="Jazz"), Genre(id="electronic", name="Electronic")],
                   artists=artists, releases=releases)


@pytest.mark.parametrize("scope", ["jazz", "electronic"])
def test_vibes_support_positive_and_negative_preferences_in_each_genre(catalog, scope):
    request = RequestIntent(genre_scope_id=scope, preferred_descriptors=["energetic"], avoided_descriptors=["noisy"])
    scored = score_candidates(catalog, request, UserProfile())
    assert [item.release_id for item in scored.items] == [f"{scope}-release-0", f"{scope}-release-2", f"{scope}-release-1"]
    assert scored.items[0].components["matched_preferred_descriptors"] == "energetic"
    assert scored.items[-1].components["matched_avoided_descriptors"] == "noisy"
    assert scored.items[1].components["sound_adjustment"] == 0
    assert "sound_descriptors_unavailable" in scored.items[1].uncertainty_flags
    assert all("source_descriptors_are_subjective" in item.uncertainty_flags for item in scored.items)
    assert all(item.components["rarity"] is None for item in scored.items)


@pytest.mark.parametrize("scope", ["jazz", "electronic"])
def test_request_controls_polarity_no_global_good_bad_axis(catalog, scope):
    # A different listener/request can want noisy sound and avoid energetic sound.
    request = RequestIntent(genre_scope_id=scope, preferred_descriptors=["noisy"], avoided_descriptors=["energetic"])
    scored = score_candidates(catalog, request, UserProfile())
    assert scored.items[0].release_id == f"{scope}-release-1"
    assert scored.items[-1].release_id == f"{scope}-release-0"
    assert scored.items[0].components["sound_adjustment"] > 0
    assert scored.items[-1].components["sound_adjustment"] < 0


def test_union_deduplicates_across_fields_and_source_rows(catalog):
    release = catalog.releases[0]
    release.source_metadata[0].descriptors = ["energetic", " Warm "]
    release.source_metadata.append(SourceMetadata(source="rym", source_row_id="other",
        title=release.title, artist_credit="Fixture jazz artist 0", descriptors=["Energetic"]))
    request = RequestIntent(genre_scope_id="jazz", preferred_descriptors=["energetic"])
    first = score_candidates(catalog, request, UserProfile()).items[0]
    release.source_metadata *= 2
    second = score_candidates(catalog, request, UserProfile()).items[0]
    assert first.score == second.score and first.components["sound_adjustment"] == .25
    assert source_descriptors(release) == {"energetic", "warm"}


def test_vibe_words_are_not_genres_audio_or_vocal_predictions(catalog):
    assert catalog.releases[0].genre_ids == ["jazz"]
    for unsupported in ("hard", "aggressive", "male vocals", "distorted vocals"):
        with pytest.raises(ValueError, match="Unknown source descriptor"):
            RequestIntent(genre_scope_id="jazz", preferred_descriptors=[unsupported]).validate_catalog(catalog)
    # No cross-genre vocabulary leakage.
    catalog.releases[-1].source_metadata = [SourceMetadata(source="aoty", source_row_id="electronic-only",
        title="Fixture", artist_credit="Fixture", vibes=["futuristic"])]
    with pytest.raises(ValueError, match="Unknown source descriptor"):
        RequestIntent(genre_scope_id="jazz", preferred_descriptors=["futuristic"]).validate_catalog(catalog)
    RequestIntent(genre_scope_id="electronic", preferred_descriptors=["futuristic"]).validate_catalog(catalog)


def test_cards_attribute_vibe_and_reuse_existing_safe_source_url(catalog, tmp_path):
    request = RequestIntent(genre_scope_id="jazz", preferred_descriptors=["energetic"], avoided_descriptors=["noisy"])
    decision = recommend(Store(tmp_path / "fixture.sqlite3"), catalog, request)
    cards = decision_cards(catalog, decision)
    positive = next(card for card in cards if card.release_id == "jazz-release-0")
    negative = next(card for card in cards if card.release_id == "jazz-release-1")
    assert any("AOTY Vibe" in text and "energetic" in text for text in positive.explanation)
    assert any("AOTY Vibe" in text and "noisy" in text for text in negative.warnings)
    assert any("не являются измерением аудио" in text for text in positive.warnings)
    assert any(link.label == "AOTY Vibe" and link.url == catalog.releases[0].links["AOTY"] for link in positive.links)
    assert positive.genres == ["Jazz"]
    assert not any("aggressive" in text or "male vocals" in text for text in positive.explanation)
    # Missing or malformed URLs do not create a guessed source link.
    catalog.releases[0].links = {"broken": "http://[broken", "AOTY": "https://example.com/album/123-false.php"}
    cards = decision_cards(catalog, decision)
    positive = next(card for card in cards if card.release_id == "jazz-release-0")
    assert not any(link.label == "AOTY Vibe" for link in positive.links)


@pytest.mark.parametrize("scope", ["jazz", "electronic"])
def test_model_context_exposes_union_with_source_uncertainty_and_no_fixed_axis(catalog, tmp_path, scope):
    store = Store(tmp_path / "fixture.sqlite3")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(catalog=catalog)))
    created = create_chat(ChatCreate(filters=Filters(genre_scope_id=scope)), request, store)
    body = MessageRequest(message_id="vibes-context", text="Больше энергии, без шумного звучания")
    claim_message(store, created.id, body)
    settings = SimpleNamespace(max_input_tokens=16000, max_input_bytes=64000, max_output_tokens=768, model="mock",
                               input_usd_per_million=.1, output_usd_per_million=.3)
    messages = model_messages(get_chat(store, created.id), body, catalog, SimpleNamespace(settings=settings))
    text = messages[0]["content"]
    context = json.loads('{"filters":' + text.split('{"filters":', 1)[1])
    assert set(context["source_descriptors"]) == {"energetic", "warm", "noisy", "mellow"}
    assert "energetic does not imply hard/aggressive" in text
    assert "noisy does not describe vocal delivery" in text
    assert "not genres or measured audio" in text
    assert not store.feedback()


@pytest.mark.parametrize("scope", ["jazz", "electronic"])
def test_mock_dialogue_preserves_request_polarity_and_requires_confirmation(catalog, tmp_path, scope):
    store = Store(tmp_path / "dialogue.sqlite3")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(catalog=catalog)))
    created = create_chat(ChatCreate(filters=Filters(genre_scope_id=scope)), request, store)
    profile = store.profile()
    proposed = Filters(genre_scope_id=scope, preferred_descriptors=["noisy"], avoided_descriptors=["energetic"])
    action = {"action": "recommend", **proposed.model_dump(), "count": 3, "release_id": None,
              "unsupported_preferences": [], "clarification": "none"}
    calls = []

    def respond(http_request):
        calls.append(json.loads(http_request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(action)}}],
                                        "usage": {"cost": 0}})

    settings = OpenRouterSettings(api_key="mock-only", model="mock", input_usd_per_million=.1,
                                  output_usd_per_million=.3, request_cost_limit=.01, daily_cost_limit=.1)
    ledger = BudgetLedger(tmp_path / "mock-ledger.sqlite3")
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        def factory():
            return OpenRouterClient(settings, ledger, http, sleep=lambda _: None)
        proposal = handle_message(store, created.id,
            MessageRequest(message_id="vibe-proposal", text="Хочу шумное звучание, но без энергичного"),
            catalog, factory)
    assert proposal.proposed_filters == proposed and not proposal.cards
    assert get_chat(store, created.id).filters.preferred_descriptors == []
    update_filters(created.id, proposal.proposed_filters, request, store)
    selection = handle_message(store, created.id,
        MessageRequest(message_id="confirmed-vibes", apply_filters=True), catalog)
    assert selection.cards[0].release_id == f"{scope}-release-1"
    assert all(card.release_id.startswith(scope) for card in selection.cards)
    assert store.decisions()[-1].request.preferred_descriptors == ["noisy"]
    assert store.decisions()[-1].request.avoided_descriptors == ["energetic"]
    assert len(calls) == 1
    assert store.profile() == profile and not store.feedback()
