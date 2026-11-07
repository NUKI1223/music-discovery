"""Golden mappings chosen manually before implementing the community mapper."""

import json
from copy import deepcopy

import pytest

from discovery.catalog.taxonomy import compile_taxonomy
from discovery.contracts import Catalog, Genre


def base_catalog():
    return Catalog(snapshot_id="taxonomy-golden", taxonomy_version="mb-tags-v2", genres=[
        Genre(id="hip-hop", name="Hip-hop"), Genre(id="jazz", name="Jazz"),
        Genre(id="rock", name="Rock"), Genre(id="electronic", name="Electronic"),
        Genre(id="rnb", name="R&B"),
        Genre(id="experimental", name="Experimental hip-hop", parent_ids=["hip-hop"]),
        Genre(id="abstract", name="Abstract hip-hop", parent_ids=["hip-hop"]),
        Genre(id="hardcore", name="Hardcore hip-hop", parent_ids=["hip-hop"]),
        Genre(id="trap", name="Trap", parent_ids=["hip-hop"]),
        Genre(id="jazz-rap", name="Jazz rap", parent_ids=["hip-hop", "jazz"]),
    ])


@pytest.fixture
def taxonomy():
    return compile_taxonomy(base_catalog(), [])


@pytest.mark.parametrize("label,expected", [
    ("Hip Hop", "hip-hop"), (" Experimental Hip-Hop ", "experimental"),
    ("Abstract Hip Hop", "abstract"), ("Hardcore Hip Hop", "hardcore"),
    ("Jazz Rap", "jazz-rap"), ("Conscious Hip Hop", "conscious-hip-hop"),
    ("Industrial Hip Hop", "industrial-hip-hop"), ("Pop Rap", "pop-rap"),
    ("Experimental", "experimental-music"), ("Hardcore Punk", "hardcore-punk"),
    ("Alternative Rock", "alternative-rock"), ("Art Pop", "art-pop"),
    ("Hard Bop", "hard-bop"), ("Neo-Soul", "neo-soul"),
    ("Contemporary R&B", "contemporary-rnb"), ("Synthpop", "synthpop"),
    ("Singer-Songwriter", "singer-songwriter"), ("MPB", "mpb"),
    ("Symphony", "symphony"), ("Technical Death Metal", "technical-death-metal"),
    ("Lo-Fi / Slacker Rock", "slacker-rock"), ("Trap [EDM]", "electronic-trap"),
])
def test_golden_exact_source_mappings(taxonomy, label, expected):
    result = taxonomy.map_labels([label], [])
    assert result.primary_ids == [expected]
    assert result.primary_labels == [label]
    assert result.unmapped == []
    assert result.evidence[0]["genre_id"] == expected


def test_primary_and_secondary_roles_are_preserved(taxonomy):
    result = taxonomy.map_labels(["Alternative Rock"], ["Art Pop", "Unknown secondary style"])
    assert result.primary_ids == ["alternative-rock"]
    assert result.secondary_ids == ["art-pop"]
    assert result.secondary_labels == ["Art Pop", "Unknown secondary style"]
    assert result.unmapped == [{"label": "Unknown secondary style", "role": "secondary", "reason": "unmapped_label"}]


@pytest.mark.parametrize("context", [[], ["Electronic"], ["Experimental"], ["Trap [EDM]"]])
def test_bare_trap_never_establishes_hiphop_alone(taxonomy, context):
    result = taxonomy.map_labels(["Trap", *context], [])
    assert "trap" not in result.primary_ids
    assert any(item["label"] == "Trap" and item["reason"] == "ambiguous_without_context" for item in result.unmapped)


def test_bare_trap_with_independent_hiphop_context(taxonomy):
    result = taxonomy.map_labels(["Trap", "Southern Hip Hop"], [])
    assert result.primary_ids == ["trap", "southern-hip-hop"]
    assert result.unmapped == []
    assert result.evidence[0]["rule"] == "contextual_alias"
    assert "southern-hip-hop" in result.evidence[0]["context"]


def test_explicit_rap_trap_qualifier_is_not_ambiguous(taxonomy):
    assert taxonomy.map_labels(["Trap [Hip Hop]"], []).primary_ids == ["trap"]


def test_coarse_labels_do_not_imply_influenced_families(taxonomy):
    catalog = Catalog(snapshot_id="compiled", genres=taxonomy.genres)
    assert "singer-songwriter" not in catalog.descendant_ids("folk")
    assert "video-game-music" not in catalog.descendant_ids("electronic")
    assert "rap-metal" in catalog.descendant_ids("metal")
    assert taxonomy.map_labels(["Fusion"], []).primary_ids == []


def test_unknown_labels_do_not_silently_become_hiphop(taxonomy):
    labels = ["Post-Hip-Hop-Future-Noise", "New unheard genre", "hardcore"]
    result = taxonomy.map_labels(labels, [])
    assert result.primary_ids == []
    assert [entry["label"] for entry in result.unmapped] == labels


def test_multigenre_ancestry_and_existing_pilot_ids_are_preserved(taxonomy):
    base = base_catalog()
    updated = {g.id: g for g in taxonomy.genres}
    for original in base.genres:
        assert updated[original.id].name == original.name
        assert updated[original.id].parent_ids == original.parent_ids
    catalog = Catalog(snapshot_id="compiled", genres=taxonomy.genres)
    assert "jazz-rap" in catalog.descendant_ids("hip-hop")
    assert "jazz-rap" in catalog.descendant_ids("jazz")
    assert "electronic-trap" not in catalog.descendant_ids("hip-hop")
    assert "experimental-music" not in catalog.descendant_ids("hip-hop")
    assert "hardcore-punk" not in catalog.descendant_ids("hip-hop")


def test_report_keeps_unmapped_occurrences_and_source_coverage():
    rows = [{"source": "rym", "genres": ["Art Rock", "Unknown"], "secondary_genres": ["Unknown"]},
            {"source": "aoty", "genres": ["Trap"], "secondary_genres": []},
            {"source": "aoty", "genres": [], "secondary_genres": []}]
    untouched = deepcopy(rows)
    result = compile_taxonomy(base_catalog(), rows)
    assert rows == untouched
    assert result.mapping_report["rows"] == 3
    assert result.mapping_report["rows_with_primary_mapping"] == 1
    assert result.mapping_report["unmapped_label_counts"]["Unknown"] == 2
    assert result.mapping_report["by_source"]["aoty"]["rows"] == 2


def test_version_is_stable_across_rows_but_changes_with_mapping_config(tmp_path):
    first = compile_taxonomy(base_catalog(), [])
    second = compile_taxonomy(base_catalog(), [{"genres": ["New unknown"]}])
    assert first.version == second.version
    assert first.version.startswith("community-taxonomy-v2+")
    config = {"version": "community-taxonomy-v1", "genres": [
        {"id": "special", "name": "Special", "parent_ids": [], "aliases": []}],
        "contextual_aliases": []}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    third = compile_taxonomy(base_catalog(), [], config_path=path)
    assert third.version != first.version
    assert third.map_labels(["Trap"], []).primary_ids == []


def test_conflicting_aliases_fail_review_instead_of_choosing_first(tmp_path):
    config = {"version": "community-taxonomy-v1", "genres": [
        {"id": "first", "name": "First", "aliases": ["same"], "parent_ids": []},
        {"id": "second", "name": "Second", "aliases": ["SAME"], "parent_ids": []}],
        "contextual_aliases": []}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="alias"):
        compile_taxonomy(base_catalog(), [], config_path=path)


def test_label_list_required_instead_of_iterating_a_serialized_string(taxonomy):
    with pytest.raises(ValueError, match="list"):
        taxonomy.map_labels("Hip Hop", [])
