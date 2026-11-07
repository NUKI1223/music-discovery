import hashlib
import json
from datetime import date

import pytest

from discovery.catalog.community import normalize_row
from discovery.catalog.unified import combine, load_snapshot
from discovery.contracts import (
    Artist,
    Catalog,
    Genre,
    Observation,
    Release,
    RequestIntent,
    UserProfile,
)
from discovery.ranking import rank
from discovery.storage import catalog_digest


def base_catalog():
    return Catalog(snapshot_id="test-base", taxonomy_version="test-base-v1",
                   genres=[Genre(id="hip-hop", name="Hip-hop"), Genre(id="rock", name="Rock")],
                   artists=[Artist(id="mb-artist", name="Test Artist")], releases=[Release(
                       id="mb-release", title="Same Album", artist_ids=["mb-artist"], year=2000,
                       genre_ids=["hip-hop"], observations=[Observation(source="musicbrainz",
                           observed_at="2026-10-28T00:00:00Z", rating=3, rating_scale=5,
                           rating_count=100, access_basis="authored test")])])


def source_row(source, title="Same Album", year=2000, **changes):
    if source == "rym":
        raw = {"release_name": title, "artist_name": "Test Artist", "release_date": str(year),
               "primary_genres": "Experimental Hip Hop", "secondary_genres": "Art Rock",
               "avg_rating": 4, "rating_count": "1", **changes}
    else:
        raw = {"album_name": title, "artist_name": "Test Artist", "release_date": str(year),
               "genres": "['Experimental Hip Hop']", "user_score_float": 95,
               "number_of_ratings": "2", "link_to_album": "/album/123-test.php", **changes}
    return normalize_row(source, raw, row_number=1, input_sha256="a" * 64)


def snapshots(*rows):
    return [([row], {"source": row["source"],
                     "acquisition_provenance": {"retrieved_at": "2026-10-28T13:00:00Z"}},
             row["source"] + "-manifest-sha") for row in rows]


def test_two_sources_enrich_same_release_with_separate_scales_and_original_observation():
    base = base_catalog()
    old_hash = catalog_digest(base)
    catalog, reports, _ = combine(base, snapshots(source_row("rym"), source_row("aoty")))
    assert catalog_digest(base) == old_hash
    release = next(r for r in catalog.releases if r.id == "mb-release")
    assert len(catalog.releases) == 1
    assert release.artist_ids == ["mb-artist"]
    assert release.observations == base.releases[0].observations
    scores = {o.source: (o.rating, o.rating_scale) for o in release.historical_observations}
    assert scores == {"rym": (4, 5), "aoty": (95, 100)}
    assert all(o.observed_at is None and not o.count_semantics_verified
               for o in release.historical_observations)
    assert reports["manifest"]["counts"]["included_source_rows"] == 2
    assert all(m.secondary_labels == ["Art Rock"] for m in release.source_metadata if m.source == "rym")
    rock_scope = catalog.descendant_ids("rock")
    assert not set(release.genre_ids) & rock_scope  # secondary stays secondary


def test_ambiguous_year_is_kept_pending_and_not_added_to_serving():
    catalog, reports, rows = combine(base_catalog(), snapshots(source_row("rym", year=2001)))
    assert len(rows) == 1
    assert reports["manifest"]["counts"]["pending_source_rows"] == 1
    assert len(catalog.releases) == 1
    assert not catalog.releases[0].historical_observations


def test_unknown_genre_survives_and_historical_counts_do_not_prove_rarity():
    row = source_row("rym", title="Different Album", primary_genres="Unmapped Test Genre")
    catalog, _, _ = combine(base_catalog(), snapshots(row))
    extra = next(r for r in catalog.releases if r.id != "mb-release")
    assert not extra.genre_ids
    assert extra.source_metadata[0].unmapped[0]["label"] == "Unmapped Test Genre"
    assert not extra.observations
    assert rank(catalog, RequestIntent(genre_scope_id="hip-hop", obscurity="require"),
                UserProfile(), date(2026, 10, 28)).items == []


def test_combine_is_deterministic_and_fixture_boundary_is_checked():
    source = snapshots(source_row("rym"), source_row("aoty"))
    first, _, _ = combine(base_catalog(), source)
    second, _, _ = combine(base_catalog(), list(reversed(source)))
    assert first == second
    base = base_catalog()
    base.is_fixture = True
    with pytest.raises(ValueError):
        combine(base, source)


def test_modified_snapshot_rejected(tmp_path):
    row = source_row("rym")
    content = json.dumps(row) + "\n"
    (tmp_path / "releases.jsonl").write_text(content)
    manifest = {"dataset_kind": "historical_community_metadata", "is_fixture": False,
                "personal_labels": 0, "source": "rym", "records_file": "releases.jsonl",
                "records_sha256": hashlib.sha256(content.encode()).hexdigest(),
                "accepted_rows": 1, "input_sha256": "a" * 64}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    assert len(load_snapshot(tmp_path)[0]) == 1
    (tmp_path / "releases.jsonl").write_text(content + "\n")
    with pytest.raises(ValueError, match="hash mismatch"):
        load_snapshot(tmp_path)


def test_one_matched_album_does_not_resolve_homonymous_artists():
    base = base_catalog()
    base.artists.append(Artist(id="other-mb-artist", name="Test Artist"))
    base.releases.append(Release(id="other-mb-album", title="Other MB Album", year=2001,
                                artist_ids=["other-mb-artist"], genre_ids=["hip-hop"]))
    known = source_row("rym")
    unknown = source_row("aoty", title="Unmatched Third Album")
    catalog, _, _ = combine(base, snapshots(known, unknown))
    third = next(r for r in catalog.releases if r.title == "Unmatched Third Album")
    assert third.artist_ids[0].startswith("credit:")
    assert third.artist_ids != ["mb-artist"]


def test_current_catalog_enrichment_preserves_ids_links_and_repeat_is_idempotent():
    base = base_catalog()
    base.releases[0].links = {"aoty": "https://www.albumoftheyear.org/album/123-old-slug.php",
                             "musicbrainz": "https://musicbrainz.org/release-group/existing"}
    before = base.model_dump(mode="json")
    row = source_row("aoty", vibes=["energetic", "dense"], source_evidence=[
        {"field": "vibes", "url": "https://www.albumoftheyear.org/album/123-test.php",
         "retrieved_at": "2026-11-02T00:00:00Z"}])
    row["rating"] = row["rating_count"] = row["rating_count_text"] = None
    enriched, _, _ = combine(base, snapshots(row))
    again, _, _ = combine(enriched, snapshots(row))
    assert base.model_dump(mode="json") == before
    assert catalog_digest(enriched) == catalog_digest(again)
    release = enriched.releases[0]
    assert release.id == "mb-release" and release.artist_ids == ["mb-artist"]
    assert release.links == base.releases[0].links
    assert release.observations == base.releases[0].observations
    assert len(release.source_metadata) == len(release.historical_observations) == 1
    assert release.source_metadata[0].vibes == ["energetic", "dense"]
    assert row["source_evidence"][0] in release.source_metadata[0].mapping_evidence
    assert release.historical_observations[0].rating is None


def test_independent_source_enrichments_of_same_external_album_retain_provenance():
    first = source_row("aoty")
    second = source_row("aoty", vibes=["noisy"])
    second["row_id"] = "aoty:independent-web:1"
    second["source_evidence"] = [{"field": "vibes", "retrieved_at": "2026-11-02T00:00:00Z"}]
    inputs = snapshots(first, second)
    inputs[1] = (inputs[1][0], inputs[1][1], "independent-web-manifest")
    enriched, reports, _ = combine(base_catalog(), inputs)
    release = enriched.releases[0]
    assert len(enriched.releases) == 1
    assert len(release.source_metadata) == len(release.historical_observations) == 2
    assert {o.provenance_id for o in release.historical_observations} == {
        "aoty-manifest-sha", "independent-web-manifest"}
    assert len(reports["manifest"]["source_manifest_entries"]) == 2
    initial, _, _ = combine(base_catalog(), snapshots(first))
    sequential, _, _ = combine(initial, [inputs[1]])
    assert sequential.releases == enriched.releases


def test_metadata_only_new_release_never_inherits_scores_or_implied_vibe_genres():
    row = source_row("aoty", title="New Jazz Album", genres=[], user_score_float=None,
                     number_of_ratings=None, vibes=["energetic"], release_date=None)
    expanded, _, _ = combine(base_catalog(), snapshots(row))
    release = next(r for r in expanded.releases if r.id != "mb-release")
    assert release.genre_ids == []
    assert release.year is None
    assert release.observations == []
    assert release.source_metadata[0].vibes == ["energetic"]
    assert all(o.rating is None and o.rating_count is None for o in release.historical_observations)
    repeat, _, _ = combine(expanded, snapshots(row))
    assert catalog_digest(repeat) == catalog_digest(expanded)


def test_generated_entity_collision_never_overwrites_base_id(monkeypatch):
    monkeypatch.setattr("discovery.catalog.matching._local_id", lambda _: "mb-release")
    base = base_catalog()
    before = base.model_dump(mode="json")
    with pytest.raises(ValueError, match="collides"):
        combine(base, snapshots(source_row("aoty", title="Unrelated Album")))
    assert base.model_dump(mode="json") == before


def test_aoty_secondary_genres_remain_evidence_without_expanding_eligibility():
    row = source_row("aoty", secondary_genres=["Art Rock", "Ambient"])
    catalog, _, _ = combine(base_catalog(), snapshots(row))
    release = catalog.releases[0]
    metadata = release.source_metadata[0]
    assert metadata.secondary_labels == ["Art Rock", "Ambient"]
    assert metadata.secondary_ids
    assert not set(metadata.secondary_ids) & set(release.genre_ids)
    assert not set(release.genre_ids) & catalog.descendant_ids("rock")
    assert rank(catalog, RequestIntent(genre_scope_id="rock"), UserProfile()).items == []
