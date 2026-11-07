from datetime import date

import pytest

from discovery.catalog.remap import remap_source_genres
from discovery.catalog.taxonomy import compile_taxonomy
from discovery.contracts import (
    Artist,
    Catalog,
    Genre,
    Release,
    RequestIntent,
    SourceMetadata,
    UserProfile,
)
from discovery.ranking import rank
from discovery.ranking.source_descriptors import ordered_vibe_positions
from discovery.storage import catalog_digest


@pytest.mark.parametrize("label,gid,root", [
    ("Electro House", "electro-house", "electronic"),
    ("Melodic Metalcore", "melodic-metalcore", "metal"),
    ("Folk Punk", "folk-punk", "folk"),
    ("Ska Punk", "ska-punk", "reggae"),
    ("Contemporary Country", "contemporary-country", "country"),
    ("Teen Pop", "teen-pop", "pop"),
    ("Acid Jazz", "acid-jazz", "jazz"),
    ("Deep Soul", "deep-soul", "rnb"),
    ("Digicore", "digicore", "hip-hop"),
    ("Trap Metal", "trap-metal", "hip-hop"),
    ("Reggaetón", "reggaeton", "latin"),
    ("Pop Reggae", "reggae-pop", "reggae"),
    ("Hymns", "hymns", "religious-music"),
    ("Neoclassical New Age", "neoclassical-new-age", "new-age"),
])
def test_new_source_labels_have_explicit_multigenre_scope(label, gid, root):
    taxonomy = compile_taxonomy(Catalog(snapshot_id="empty"), [])
    graph = Catalog(snapshot_id="graph", genres=taxonomy.genres)
    assert taxonomy.map_labels([label], []).primary_ids == [gid]
    assert gid in graph.descendant_ids(root)
    if gid == "trap-metal":
        assert gid not in graph.descendant_ids("metal")
    if gid == "digicore":
        assert gid not in graph.descendant_ids("electronic")


def example():
    evidence = {"kind": "aoty_album_readable_metadata",
                "url": "https://www.albumoftheyear.org/album/123-synthetic.php",
                "retrieved_at": "2026-11-03T00:00:00Z", "text_sha256": "a" * 64,
                "vibe_completeness": "visible_subset"}
    return Catalog(snapshot_id="original", taxonomy_version="old", is_fixture=True,
                   genres=[Genre(id="electronic", name="Electronic")],
                   artists=[Artist(id="artist", name="Synthetic artist")],
                   releases=[Release(id="album", title="Synthetic album", artist_ids=["artist"],
                       links={"aoty": evidence["url"]}, source_metadata=[SourceMetadata(
                           source="aoty", source_row_id="row", title="Synthetic album",
                           artist_credit="Synthetic artist", primary_labels=["Electro House"],
                           secondary_labels=["Folk Punk"], vibes=["energetic", "soft"],
                           mapping_evidence=[evidence])])])


def test_remap_is_additive_idempotent_and_preserves_source_facts_and_vibe_order():
    base = example()
    before = catalog_digest(base)
    catalog, report = remap_source_genres(base)
    assert catalog_digest(base) == before
    assert catalog.is_fixture
    old, new = base.releases[0], catalog.releases[0]
    assert new.genre_ids == ["electro-house"]
    assert new.artist_ids == old.artist_ids
    assert new.id == old.id
    assert new.links == old.links
    assert new.observations == old.observations
    assert new.historical_observations == old.historical_observations
    excluded = {"primary_ids", "secondary_ids", "unmapped", "mapping_evidence"}
    assert new.source_metadata[0].model_dump(exclude=excluded) == old.source_metadata[0].model_dump(exclude=excluded)
    assert old.source_metadata[0].mapping_evidence[0] in new.source_metadata[0].mapping_evidence
    assert ordered_vibe_positions(new) == ordered_vibe_positions(old) == {"energetic": 1, "soft": 2}
    assert report["newly_classified_releases"] == 1
    again, second_report = remap_source_genres(catalog)
    assert catalog_digest(again) == catalog_digest(catalog)
    assert second_report["releases_with_added_genres"] == 0


def test_secondary_genres_and_vibes_never_leak_into_eligibility():
    catalog, _ = remap_source_genres(example())
    metadata = catalog.releases[0].source_metadata[0]
    assert metadata.secondary_ids == ["folk-punk"]
    assert "folk-punk" not in catalog.releases[0].genre_ids
    assert catalog.artists[0].genre_ids == ["electro-house"]
    assert rank(catalog, RequestIntent(genre_scope_id="folk"), UserProfile(), date(2026, 11, 3)).items == []
    assert [x.release_id for x in rank(catalog, RequestIntent(genre_scope_id="electro-house"),
                                     UserProfile(), date(2026, 11, 3)).items] == ["album"]


def test_unknown_and_ambiguous_labels_stay_unmapped_and_legacy_assertions_survive():
    base = example()
    base.releases[0].genre_ids = ["electronic"]
    base.releases[0].source_metadata[0].primary_labels = ["Trap", "Unknown style"]
    catalog, _ = remap_source_genres(base)
    assert catalog.releases[0].genre_ids == ["electronic"]
    reasons = {item["reason"] for item in catalog.releases[0].source_metadata[0].unmapped}
    assert reasons == {"ambiguous_without_context", "unmapped_label"}
