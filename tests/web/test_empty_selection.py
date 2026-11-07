"""Empty slates explain observed inventory without weakening filters or calling a model."""

from types import SimpleNamespace

import pytest

from discovery.contracts import Artist, ArtistPopularityLabel, Catalog, Genre, Release, UserProfile
from discovery.storage import Store
from discovery.web.chat import create_chat, get_chat, handle_message
from discovery.web.schemas import ChatCreate, Filters, MessageRequest


@pytest.fixture
def catalog():
    return Catalog(snapshot_id="authored-exhaustion-fixture", is_fixture=True,
        genres=[Genre(id="hip-hop", name="Hip-hop"), Genre(id="rage", name="Rage", parent_ids=["hip-hop"]),
                Genre(id="jazz", name="Jazz")],
        artists=[Artist(id=f"a{i}", name=f"Fixture Artist {i}", genre_ids=["hip-hop"]) for i in range(4)],
        releases=[Release(id=f"r{i}", title=f"Fixture Album {i}", artist_ids=[f"a{i}"],
                          genre_ids=["rage" if i < 3 else "hip-hop"]) for i in range(4)])


def forbidden_provider():
    raise AssertionError("Inventory explanation must not call a paid provider")


def setup_chat(tmp_path, catalog, filters=None, profile=None):
    store = Store(tmp_path / "fixture.sqlite3")
    if profile is not None:
        store.set_base_profile(profile)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(catalog=catalog)))
    detail = create_chat(ChatCreate(filters=filters or Filters(locked_subgenre_ids=["rage"])), request, store)
    return store, detail


def free_message(store, detail, catalog):
    return handle_message(store, detail.id, MessageRequest(message_id="free-selection", apply_filters=True),
                          catalog, forbidden_provider)


def test_known_and_listened_inventory_is_exhausted_even_with_unrelated_fame_policy(tmp_path, catalog):
    profile = UserProfile(known_release_ids=["r0"], liked_release_ids_by_genre={"hip-hop": ["r1"]},
        disliked_release_ids_by_genre={"hip-hop": ["r2"]}, limit_widely_known_artists=True,
        max_widely_known_artists=1, request_only_artist_ids=["a0", "a1"])
    store, detail = setup_chat(tmp_path, catalog, profile=profile)
    before = store.profile()
    message = free_message(store, detail, catalog)
    assert message.status == "completed" and not message.cards
    assert "в текущем каталоге (3)" in message.text and "Повторы исключены" in message.text
    assert "знакомыми/прослушанными" in message.text
    assert "Личный лимит" not in message.text and "прямому запросу" not in message.text
    assert "Настройки подборки" in message.text and "подтвердите" in message.text
    assert get_chat(store, detail.id).filters == detail.filters
    assert store.profile() == before and not store.feedback()
    assert len(store.decisions()) == 1


def test_more_options_reports_shown_exhaustion_in_locked_scope_without_provider(tmp_path, catalog):
    store, detail = setup_chat(tmp_path, catalog)
    first = free_message(store, detail, catalog)
    assert {card.release_id for card in first.cards} == {"r0", "r1", "r2"}
    result = handle_message(store, detail.id,
        MessageRequest(message_id="more-options", text="Ещё варианты"), catalog, forbidden_provider)
    assert result.status == "completed" and not result.cards
    assert "в текущем каталоге (3)" in result.text and "уже показаны" in result.text
    assert get_chat(store, detail.id).filters.locked_subgenre_ids == ["rage"]
    assert len(store.decisions()) == 2  # No counterfactual decision or retry was persisted.


def test_empty_genre_reports_missing_catalog_data_not_exhaustion(tmp_path, catalog):
    store, detail = setup_chat(tmp_path, catalog, filters=Filters(genre_scope_id="jazz"),
                              profile=UserProfile(limit_widely_known_artists=True))
    result = free_message(store, detail, catalog)
    assert not result.cards and "пока нет релизов выбранного направления" in result.text
    assert "уже показаны" not in result.text and "Личный лимит" not in result.text
    assert get_chat(store, detail.id).filters.genre_scope_id == "jazz"


@pytest.mark.parametrize("kind", ["strict_obscurity", "artist_exclusions", "actual_fame_cap", "mixed_known_and_filters"])
def test_real_constraints_are_not_misreported_as_missing_data_or_full_exhaustion(tmp_path, catalog, kind):
    filters = Filters(locked_subgenre_ids=["rage"])
    profile = UserProfile()
    if kind == "strict_obscurity":
        filters.obscurity = "require"  # This fixture has no comparable popularity observations.
    elif kind == "artist_exclusions":
        filters.excluded_artist_ids = ["a0", "a1", "a2"]
    elif kind == "actual_fame_cap":
        profile.limit_widely_known_artists = True
        profile.max_widely_known_artists = 0
        profile.artist_popularity_labels = {f"a{i}": ArtistPopularityLabel(
            classification="widely_known", evidence="authored fixture label") for i in range(3)}
    else:
        profile.known_release_ids = ["r0"]
        filters.excluded_artist_ids = ["a1", "a2"]
    store, detail = setup_chat(tmp_path, catalog, filters, profile)
    result = free_message(store, detail, catalog)
    assert not result.cards and "действующими ограничениями" in result.text
    assert "Повторы исключены" not in result.text and "пока нет релизов" not in result.text
    assert "Личный лимит" not in result.text
    assert get_chat(store, detail.id).filters == filters
