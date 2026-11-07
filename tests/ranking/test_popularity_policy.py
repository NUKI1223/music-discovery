"""Artist popularity quotas use explicit account evidence, not name or rating heuristics."""

import pytest

from discovery.contracts import (
    Artist,
    ArtistPopularityLabel,
    Catalog,
    Genre,
    RankingResult,
    RecommendationItem,
    Release,
    RequestIntent,
    UserProfile,
)
from discovery.ranking import rank, score_candidates
from discovery.ranking.baseline import artist_popularity, counts_toward_popularity_limit


def fixture(wide=5, less=3, unknown=0):
    labels, artists, releases = {}, [], []
    for prefix, count, classification in (("a-wide", wide, "widely_known"),
                                           ("m-less", less, "less_known"),
                                           ("z-unknown", unknown, None)):
        for index in range(count):
            ident = f"{prefix}-{index}"
            artists.append(Artist(id=ident, name=f"Performer {ident}"))
            releases.append(Release(id=ident, title=f"Release {ident}", artist_ids=[ident], genre_ids=["jazz"]))
            if classification:
                labels[ident] = ArtistPopularityLabel(classification=classification,
                                                       evidence="Explicit listener statement in contract fixture")
    catalog = Catalog(snapshot_id="popularity-contract-fixture", genres=[Genre(id="jazz", name="Jazz")],
                      artists=artists, releases=releases)
    return catalog, UserProfile(limit_widely_known_artists=True, artist_popularity_labels=labels)


@pytest.mark.parametrize("wide,less,expected_length,expected_wide", [
    (5, 0, 0, 0), (5, 1, 1, 0), (5, 2, 3, 1), (5, 3, 5, 2),
    (1, 1, 1, 0), (1, 2, 3, 1), (2, 2, 3, 1),
])
def test_strict_minority_uses_actual_partial_slate(wide, less, expected_length, expected_wide):
    catalog, profile = fixture(wide=wide, less=less)
    request = RequestIntent(genre_scope_id="jazz")
    result = rank(catalog, request, profile)
    by_id = {release.id: release for release in catalog.releases}
    actual_wide = sum(counts_toward_popularity_limit(by_id[item.release_id], request, profile)
                      for item in result.items)
    assert len(result.items) == expected_length
    assert actual_wide == expected_wide
    assert actual_wide <= 2
    assert not result.items or actual_wide * 2 < len(result.items)


@pytest.mark.parametrize("count", [1, 2, 3, 4, 5])
def test_requested_small_slates_fill_with_nonpopular_candidates(count):
    catalog, profile = fixture(wide=5, less=5)
    result = rank(catalog, RequestIntent(genre_scope_id="jazz", count=count), profile)
    assert len(result.items) == count
    assert sum(item.components["artist_popularity_classification"] == "widely_known"
               for item in result.items) <= min(2, (count - 1) // 2)


def test_unknown_is_not_less_known_and_default_account_unaffected():
    catalog, profile = fixture(wide=5, less=0, unknown=3)
    request = RequestIntent(genre_scope_id="jazz")
    result = rank(catalog, request, profile)
    assert len(result.items) == 5
    assert sum(item.components["artist_popularity_classification"] == "widely_known"
               for item in result.items) == 2
    assert sum(item.components["artist_popularity_classification"] == "unknown"
               for item in result.items) == 3
    assert any("Неизвестные не объявляются малоизвестными" in warning for warning in result.warnings)
    other = rank(catalog, request, UserProfile())
    assert len(other.items) == 5 and all(item.release_id.startswith("a-wide") for item in other.items)
    assert "limit_widely_known_artists" not in UserProfile().model_dump()
    assert "artist_popularity_labels" not in UserProfile().model_dump()


def test_collaboration_any_widely_known_artist_counts_and_exception_is_specific():
    catalog, profile = fixture(wide=2, less=1, unknown=1)
    collaboration = Release(id="collab", title="Collaboration", artist_ids=["a-wide-0", "a-wide-1"], genre_ids=["jazz"])
    catalog.releases = [collaboration]
    assert artist_popularity(collaboration, profile) == "widely_known"
    request = RequestIntent(genre_scope_id="jazz", requested_artist_ids=["a-wide-0"], count=1)
    assert counts_toward_popularity_limit(collaboration, request, profile) is True
    assert rank(catalog, request, profile).items == []
    request.requested_artist_ids = ["a-wide-0", "a-wide-1"]
    assert counts_toward_popularity_limit(collaboration, request, profile) is False
    assert len(rank(catalog, request, profile).items) == 1
    collaboration.artist_ids = ["m-less-0", "z-unknown-0"]
    assert artist_popularity(collaboration, profile) == "unknown"
    collaboration.artist_ids = ["m-less-0"]
    assert artist_popularity(collaboration, profile) == "less_known"


def test_explicit_artist_exception_keeps_personal_rule_and_requires_evidence():
    catalog, profile = fixture(wide=5, less=0)
    request = RequestIntent(genre_scope_id="jazz", requested_artist_ids=["a-wide-0"], count=1)
    assert len(rank(catalog, request, profile).items) == 1
    assert profile.limit_widely_known_artists is True
    assert len(profile.artist_popularity_labels) == 5
    with pytest.raises(ValueError):
        ArtistPopularityLabel(classification="widely_known", source="llm_memory", evidence="guess")
    with pytest.raises(ValueError):
        ArtistPopularityLabel(classification="less_known", evidence="")
    with pytest.raises(ValueError):
        ArtistPopularityLabel(classification="less_known", evidence="said by user", labelled_at="2026-01-01")


@pytest.mark.parametrize("cap", [0, 1, 2])
def test_bandit_cannot_replace_fifth_slot_with_excess_popular_artist(monkeypatch, cap):
    from discovery.experiments.bandit import policy

    catalog, profile = fixture(wide=3, less=6)
    profile.max_widely_known_artists = cap
    request = RequestIntent(genre_scope_id="jazz")
    ids = [f"a-wide-{i}" for i in range(cap)] + [f"m-less-{i}" for i in range(5 - cap)]
    items = [RecommendationItem(release_id=ident, score=1, components={"novelty": .7}) for ident in ids]
    baseline = RankingResult(items=items, profile_basis="artist_prior", candidate_ids=ids)
    scored = score_candidates(catalog, request, profile)
    monkeypatch.setattr(policy, "rank", lambda *args, **kwargs: baseline)
    monkeypatch.setattr(policy, "score_candidates", lambda *args, **kwargs: scored)
    monkeypatch.setattr(policy, "exploration_candidates", lambda *args: scored.items)
    result, details = policy.build_slate(catalog, request, profile, seed=17,
                                        score_overrides={"a-wide-2": 100})
    assert details["enabled"] is True
    assert "a-wide-2" not in details["candidate_ids"]
    assert sum(item.release_id.startswith("a-wide") for item in result.items) == cap


def test_zero_cap_excludes_every_labelled_popular_artist_before_selection():
    catalog, profile = fixture(wide=5, less=3, unknown=2)
    profile.max_widely_known_artists = 0
    request = RequestIntent(genre_scope_id="jazz")
    candidates = score_candidates(catalog, request, profile)
    assert len(candidates.items) == 5
    assert not any(item.release_id.startswith("a-wide") for item in candidates.items)
    selected = rank(catalog, request, profile)
    assert len(selected.items) == 5
    assert any("только по прямому запросу" in warning for warning in selected.warnings)
    assert any("полную гарантию дать нельзя" in warning for warning in selected.warnings)
    assert sum(item.components["artist_popularity_classification"] == "unknown" for item in selected.items) == 2
    profile.max_widely_known_artists = 2
    assert sum(item.release_id.startswith("a-wide") for item in rank(catalog, request, profile).items) == 2
    profile.max_widely_known_artists = 0
    profile.limit_widely_known_artists = False
    assert all(item.release_id.startswith("a-wide") for item in rank(catalog, request, profile).items)
    assert "max_widely_known_artists" not in UserProfile().model_dump()


def test_zero_cap_explicit_artist_exception_cannot_authorize_popular_collaborator():
    catalog, profile = fixture(wide=2, less=1)
    profile.max_widely_known_artists = 0
    request = RequestIntent(genre_scope_id="jazz", requested_artist_ids=["a-wide-0"], count=1)
    selected = rank(catalog, request, profile)
    assert [item.release_id for item in selected.items] == ["a-wide-0"]
    collaboration = Release(id="collab", title="Collaboration", artist_ids=["a-wide-0", "a-wide-1"], genre_ids=["jazz"])
    catalog.releases = [collaboration]
    assert not score_candidates(catalog, request, profile).items
    request.requested_artist_ids = ["a-wide-0", "a-wide-1"]
    assert [item.release_id for item in rank(catalog, request, profile).items] == ["collab"]


@pytest.mark.parametrize("cap,expected", [(0, 0), (1, 1), (2, 2)])
def test_numeric_popularity_cap_limits_full_slates(cap, expected):
    catalog, profile = fixture(wide=5, less=5)
    profile.max_widely_known_artists = cap
    result = rank(catalog, RequestIntent(genre_scope_id="jazz"), profile)
    assert len(result.items) == 5
    assert sum(item.release_id.startswith("a-wide") for item in result.items) == expected
    payload = profile.model_dump()
    assert payload.get("max_widely_known_artists", 2) == cap
