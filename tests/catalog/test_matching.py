"""Synthetic adversarial identity cases; real holdout pairs live in eval fixtures."""

from copy import deepcopy

import pytest

from discovery.catalog.matching import classify_pair, match_releases
from discovery.contracts import Artist, Catalog, Release


def catalog(*releases):
    return Catalog(snapshot_id="matching-test", artists=[Artist(id="a", name="Artist A"),
        Artist(id="b", name="Artist B"), Artist(id="ye", name="Ye")], releases=list(releases))


def release(id="mb-one", **kwargs):
    return Release(**{"id": id, "title": "Test Album", "artist_ids": ["a"], "year": 2000, **kwargs})


def row(id="row-1", source="rym", **kwargs):
    return {"row_id": id, "source": source, "title": "Test Album", "artist_credit": "Artist A",
            "year": 2000, "is_fixture": False, "source_release_id": None, "source_url": None, **kwargs}


def pair(r=None, album=None, **kwargs):
    c = catalog()
    return classify_pair(r or row(), album or release(), {a.id: a for a in c.artists}, **kwargs)


def test_pair_exact_rule_preserves_casefold_space_normalization_only():
    result = pair(row(title=" TEST  ALBUM ", artist_credit="artist a"))
    assert result["decision"] == "match"
    assert result["rule"] == "exact_credit_title_known_year_v1"
    assert result["evidence"]["independently_verified"] is False


@pytest.mark.parametrize("change", [{"artist_credit": "Artist B"}, {"title": "Test-Album"},
                                    {"title": "Test Album (Deluxe Edition)"}])
def test_title_only_or_changed_edition_never_matches(change):
    assert pair(row(**change))["decision"] == "reject"


@pytest.mark.parametrize("change", [{"year": None, "chart_year": 2000}, {"year": 2001},
                                    {"release_type": "ep"}])
def test_unknown_year_and_conflicting_year_type_abstain(change):
    assert pair(row(**change))["decision"] == "abstain"


def test_unknown_candidate_year_does_not_use_source_year_as_proof():
    assert pair(album=release(year=None))["decision"] == "abstain"


def test_collaboration_requires_entire_ordered_credit():
    album = release(artist_ids=["a", "b"])
    assert pair(row(artist_credit="Artist A"), album)["decision"] == "reject"
    assert pair(row(artist_credit="Artist A / Artist B"), album)["decision"] == "match"
    assert pair(row(artist_credit="Artist B / Artist A"), album)["decision"] == "reject"


def test_alias_is_explicit_id_bound_and_not_a_group_alias():
    album = release(artist_ids=["ye"])
    r = row(artist_credit="Kanye West")
    assert pair(r, album)["decision"] == "reject"
    assert pair(r, album, artist_aliases={"Kanye West": "ye"})["decision"] == "match"
    assert pair(r, release(artist_ids=["ye", "b"]),
                artist_aliases={"Kanye West": "ye"})["decision"] == "reject"


def test_exact_external_id_can_accept_unknown_year_and_slug_variation():
    album = release(links={"aoty": "https://www.albumoftheyear.org/album/123-old-slug.php"})
    r = row(source="aoty", year=None, source_release_id="123",
            source_url="https://albumoftheyear.org/album/123-new-slug.php")
    assert pair(r, album)["rule"] == "exact_external_identity_v1"
    assert pair(r, album)["decision"] == "match"


def test_conflicting_external_id_is_not_overridden_by_exact_names():
    album = release(links={"aoty": "https://www.albumoftheyear.org/album/123-test.php"})
    assert pair(row(source="aoty", source_release_id="124"), album)["decision"] == "reject"


def test_external_identity_metadata_conflict_requires_review():
    album = release(links={"aoty": "https://www.albumoftheyear.org/album/123-test.php"})
    assert pair(row(source="aoty", source_release_id="123", year=2001), album)["decision"] == "abstain"


def test_exact_unique_mb_match_preserves_id_and_does_not_mutate_inputs():
    c = catalog(release())
    rows = [row()]
    previous = deepcopy(rows)
    result = match_releases(c, rows)
    assert result["links"][0]["release_id"] == "mb-one"
    assert result["links"][0]["status"] == "accepted"
    assert result["entities"] == []
    assert rows == previous


def test_multiple_mb_entities_same_identity_remain_pending():
    result = match_releases(catalog(release(), release("mb-two")), [row()])
    link = result["links"][0]
    assert link["status"] == "pending"
    assert link["candidate_ids"] == ["mb-one", "mb-two"]
    assert link["release_id"].startswith("community:")


def test_external_identity_beats_an_ambiguous_name_candidate():
    c = catalog(release(links={"aoty": "https://www.albumoftheyear.org/album/123-test.php"}),
                release("mb-two"))
    result = match_releases(c, [row(source="aoty", source_release_id="123")])
    assert result["links"][0]["release_id"] == "mb-one"
    assert result["links"][0]["rule"] == "exact_external_identity_v1"


def test_conflicting_external_identity_cannot_fall_back_to_other_tuple_candidate():
    c = catalog(release(year=1999, links={"aoty": "https://www.albumoftheyear.org/album/123-test.php"}),
                release("mb-two"))
    result = match_releases(c, [row(source="aoty", source_release_id="123")])
    assert result["links"][0]["status"] == "pending"
    assert result["links"][0]["rule"] == "external_identity_unresolved"


def test_unique_cross_source_tuple_joins_as_local_id_without_mbid():
    rows = [row(), row("row-2", source="aoty", source_release_id="123")]
    result = match_releases(catalog(), rows)
    assert len(result["entities"]) == 1
    assert result["entities"][0]["source_row_ids"] == ["row-1", "row-2"]
    assert all(x["status"] == "accepted" for x in result["links"])
    assert result["links"][0]["evidence"]["same_identity_source_rows"] == ["row-1", "row-2"]
    assert result["links"][0]["evidence"]["same_source_entity_counts"] == {"aoty": 1, "rym": 1}
    assert result == match_releases(catalog(), list(reversed(rows)))


def test_repeated_provider_id_can_group_consistent_identity_and_choose_known_year():
    rows = [row("row-1", source="aoty", source_release_id="123", year=None),
            row("row-2", source="aoty", source_release_id="123")]
    result = match_releases(catalog(), rows)
    assert len(result["entities"]) == 1
    assert result["entities"][0]["representative_row_id"] == "row-2"
    assert rows[0]["year"] is None


def test_same_provider_id_conflicting_years_never_merges():
    rows = [row("row-1", source="aoty", source_release_id="123", year=2001),
            row("row-2", source="aoty", source_release_id="123")]
    result = match_releases(catalog(), rows)
    assert len(result["entities"]) == 2
    assert all(x["status"] == "pending" for x in result["links"])


def test_same_source_distinct_ids_same_tuple_cannot_borrow_single_mb_identity():
    rows = [row("row-1", source="aoty", source_release_id="123"),
            row("row-2", source="aoty", source_release_id="124"), row("row-3")]
    result = match_releases(catalog(release()), rows)
    assert all(x["status"] == "pending" for x in result["links"])
    assert len(result["entities"]) == 3


@pytest.mark.parametrize("year", [None, 2001])
def test_unknown_or_conflicting_years_cannot_cross_source_join(year):
    result = match_releases(catalog(), [row(), row("row-2", source="aoty", year=year)])
    assert len(result["entities"]) == 2
    assert all(x["status"] == "pending" for x in result["links"])


def test_unmatched_release_retained_with_deterministic_internal_id():
    result = match_releases(catalog(), [row()])
    assert len(result["entities"]) == 1
    assert result["links"][0]["status"] == "unmatched"
    assert result["links"][0]["release_id"].startswith("community:")
    assert result == match_releases(catalog(), [row()])


def test_known_provider_identity_remains_stable_when_snapshot_row_id_changes():
    first = match_releases(catalog(), [row("snapshot1-row1", source="aoty", year=None,
                                          source_release_id="123")])
    second = match_releases(catalog(), [row("snapshot2-row17", source="aoty", year=None,
                                           source_release_id="123")])
    assert first["links"][0]["release_id"] == second["links"][0]["release_id"]


def test_local_known_year_identity_does_not_depend_on_source_snapshot_hash():
    first = match_releases(catalog(), [row("snapshot1-row1")])
    second = match_releases(catalog(), [row("snapshot2-row17")])
    assert first["links"][0]["release_id"] == second["links"][0]["release_id"]


@pytest.mark.parametrize("bad", [{"is_fixture": True}, {"source": "demo"},
                                {"year": "2000"}, {"artist_credit": ""}])
def test_invalid_or_fixture_rows_are_rejected(bad):
    with pytest.raises(ValueError):
        match_releases(catalog(), [row(**bad)])


def test_fixture_catalog_duplicate_row_ids_and_unknown_alias_fail():
    c = catalog()
    c.is_fixture = True
    with pytest.raises(ValueError):
        match_releases(c, [row()])
    with pytest.raises(ValueError):
        match_releases(catalog(), [row(), row()])
    with pytest.raises(ValueError):
        match_releases(catalog(), [row()], artist_aliases={"Kanye West": "not-in-catalog"})


def test_source_url_id_conflict_rejected_before_matching():
    with pytest.raises(ValueError):
        match_releases(catalog(), [row(source="aoty", source_release_id="123",
            source_url="https://www.albumoftheyear.org/album/124-other.php")])
