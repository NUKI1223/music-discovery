"""Authored readable-text examples shaped like AOTY indexes, not downloaded facts."""

import hashlib
import json

import pytest

from discovery.catalog.community import normalize_row
from discovery.catalog.unified import combine, load_snapshot
from discovery.catalog.vibe_index import (
    build,
    enrich_rows,
    inspect_inputs,
    parse_index,
    serialize_parts,
)
from discovery.contracts import Artist, Catalog, Genre, Release, SourceMetadata
from discovery.storage import catalog_digest

URL = "https://www.albumoftheyear.org/all/releases/vibe/energetic/"
ALBUM_URL = "https://www.albumoftheyear.org/album/123-synthetic.php"


def page(cards=None, *, url=URL, vibe="energetic", active="energetic", page_number=None, tail=None):
    cards = cards if cards is not None else ["【77†Fixture Artist】【78†Fixture Album】", "2001 • LP",
                                            "91", "critic score", "(99)", "80", "user score", "(1.2K)"]
    lines = ["【0†Sign In】", "【1†Best Albums】", "Popularity", active, "", "+ Filter", "- Filter",
             "Type", "【2†LP】", "Genre", "【3†Jazz】", "【4†Rock】", "", *cards,
             *(tail if tail is not None else ["【181†NEXT »】", "Related", "ALBUMS"])]
    title_page = f"Page {page_number} - " if page_number else ""
    text = f"All Album Releases, Music Releases - {title_page}Album of The Year ({url})\n"
    text += "Readable fixture header\n" + "\n".join(f"L{index}: {line}" for index, line in enumerate(lines)) + "\n"
    receipt = {"url": url, "vibe": vibe, "retrieved_at": "2026-11-02T00:00:00Z",
               "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
               "is_fixture": False, "transport": "web_readable_text"}
    return text, receipt


def catalog():
    return Catalog(snapshot_id="authored-index-test", genres=[Genre(id="jazz", name="Jazz")],
        artists=[Artist(id="artist", name="Fixture Artist", genre_ids=["jazz"])],
        releases=[Release(id="release", title="Fixture Album", year=2001,
                          artist_ids=["artist"], genre_ids=["jazz"], links={"aoty": ALBUM_URL})])


def parsed_page(*args, **kwargs):
    text, receipt = page(*args, **kwargs)
    return parse_index(text, receipt), receipt, "synthetic.txt"


def test_main_cards_only_and_no_scores_genres_or_footer_are_extracted():
    result, _, _ = parsed_page(tail=["Related", "【9†Footer Artist】【10†Footer Album】", "2002 • LP"])
    assert result["issues"] == []
    assert result["cards"] == [{"artist_credit": "Fixture Artist", "title": "Fixture Album",
                                 "year": 2001, "vibe": "energetic", "line": 14}]


def test_collaborations_symbolic_names_and_pagination_preserve_full_credit():
    cards = ["First & Second", "", "【10†Collaborative Album】", "2001 • Mixtape",
             "【11†¥$】【12†Symbolic Album】", "2024 • LP",
             "【13†A】 & 【14†B】", "【15†Linked Collaboration】", "2002 • EP"]
    result, _, _ = parsed_page(cards, url=URL + "2/", page_number=2)
    assert [card["artist_credit"] for card in result["cards"]] == ["First & Second", "¥$", "A & B"]
    assert [card["title"] for card in result["cards"]] == ["Collaborative Album", "Symbolic Album", "Linked Collaboration"]


@pytest.mark.parametrize("changes", [
    {"url": "https://example.org/all/releases/vibe/energetic/"},
    {"url": "https://www.albumoftheyear.org/all/releases/vibe/not-energetic/"},
    {"url": URL + "?exclude=energetic"}, {"url": URL + "#negative"},
    {"url": "http://www.albumoftheyear.org/all/releases/vibe/energetic/"},
    {"vibe": "noisy"}, {"active": "not energetic"}, {"active": "noisy"},
    {"url": URL + "2/", "page_number": 3},
])
def test_receipt_positive_filter_host_and_header_must_agree(changes):
    text, receipt = page(**changes)
    with pytest.raises(ValueError):
        parse_index(text, receipt)


@pytest.mark.parametrize("change", ["hash", "fixture", "transport", "naive_time", "missing_filter", "missing_line", "missing_end"])
def test_unverified_or_incomplete_page_is_rejected(change):
    text, receipt = page()
    if change == "hash":
        receipt["text_sha256"] = "0" * 64
    elif change == "fixture":
        receipt["is_fixture"] = True
    elif change == "transport":
        receipt["transport"] = "unverified"
    elif change == "naive_time":
        receipt["retrieved_at"] = "2026-11-02T00:00:00"
    else:
        if change == "missing_filter":
            text = text.replace("+ Filter", "Unknown")
        elif change == "missing_line":
            text = text.replace("L2: Popularity\n", "")
        else:
            text = text.replace("NEXT »", "More").replace("Related", "Other").replace("ALBUMS", "Other")
        receipt["text_sha256"] = hashlib.sha256(text.encode()).hexdigest()
    with pytest.raises(ValueError):
        parse_index(text, receipt)


def test_review_section_and_repeated_cards_do_not_add_memberships():
    card = ["【77†Fixture Artist】【78†Fixture Album】", "2001 • LP"]
    result, _, _ = parsed_page(card + card + ["## Popular User Reviews", "【80†Review Artist】【81†Review Album】", "2003 • LP"])
    assert len(result["cards"]) == 1
    assert result["issues"][0]["reason"] == "DUPLICATE_CARD"


def test_unknown_year_and_previous_scores_are_reported_not_borrowed_as_identity():
    result, _, _ = parsed_page(["【77†Fixture Artist】【78†Fixture Album】", "TBA • LP",
                                "(1.2K)", "【80†Missing Artist】", "2001 • LP"])
    assert not result["cards"]
    assert [issue["reason"] for issue in result["issues"]] == ["MISSING_KNOWN_YEAR", "MISSING_FULL_ARTIST_CREDIT"]


def test_exact_match_only_preserves_catalog_and_existing_evidence():
    base = catalog()
    before = catalog_digest(base)
    rows, report = enrich_rows(base, [parsed_page(), parsed_page()])
    assert catalog_digest(base) == before
    assert report["matched_releases"] == 1
    assert report["matched_release_vibe_pairs"] == 1
    assert rows[0]["vibes"] == ["energetic"]
    assert len(rows[0]["source_evidence"]) == 1
    assert rows[0]["genres"] == []
    assert rows[0]["user_score"] is rows[0]["number_of_ratings"] is None
    evidence = rows[0]["source_evidence"][0]
    assert evidence["existing_catalog_album_url"] == ALBUM_URL
    assert evidence["external_album_id_read_from_index"] is False
    assert evidence["index_page_url"] == URL
    assert evidence["match_rule"] == "exact_full_credit_title_known_year"


@pytest.mark.parametrize("kind,reason", [("different_year", "NO_EXACT_CATALOG_MATCH"),
    ("unknown_year", "NO_EXACT_CATALOG_MATCH"), ("different_title", "NO_EXACT_CATALOG_MATCH"),
    ("partial_credit", "NO_EXACT_CATALOG_MATCH"), ("no_url", "NO_EXISTING_AOTY_ALBUM_URL"),
    ("fake_url", "NO_EXISTING_AOTY_ALBUM_URL"), ("ambiguous", "AMBIGUOUS_CATALOG_MATCH"),
    ("two_aoty_ids", "AMBIGUOUS_EXISTING_AOTY_IDENTITY")])
def test_ambiguous_or_incomplete_identity_never_guesses(kind, reason):
    base = catalog()
    release = base.releases[0]
    if kind == "different_year":
        release.year = 2002
    elif kind == "unknown_year":
        release.year = None
    elif kind == "different_title":
        release.title += " (Deluxe)"
    elif kind == "partial_credit":
        base.artists[0].name += " & Collaborator"
    elif kind == "no_url":
        release.links = {}
    elif kind == "fake_url":
        release.links = {"aoty": "https://example.org/album/123-synthetic.php"}
    elif kind == "ambiguous":
        base.releases.append(release.model_copy(update={"id": "duplicate"}, deep=True))
    else:
        release.links["second"] = "https://www.albumoftheyear.org/album/124-other.php"
    rows, report = enrich_rows(base, [parsed_page()])
    assert rows == []
    assert report["unmatched_reason_counts"] == {reason: 1}


def test_full_source_credit_can_match_verified_existing_multi_artist_metadata():
    base = catalog()
    base.artists[0].name = "First"
    base.artists.append(Artist(id="second", name="Second"))
    base.releases[0].artist_ids.append("second")
    base.releases[0].source_metadata.append(SourceMetadata(source="aoty", source_row_id="old",
        artist_credit="First & Second", title="Fixture Album", vibes=["warm"]))
    rows, _ = enrich_rows(base, [parsed_page(["First & Second", "【78†Fixture Album】", "2001 • LP"])])
    assert rows[0]["artist_name"] == "First / Second"
    normalized = normalize_row("aoty", rows[0], row_number=1, input_sha256="a" * 64)
    expanded, _, _ = combine(base, [([normalized], {"source": "aoty", "acquisition_provenance": {}}, "new-provenance")])
    assert len(expanded.releases) == 1
    assert expanded.releases[0].id == "release"
    assert expanded.releases[0].source_metadata[0].vibes == ["warm"]
    assert expanded.releases[0].source_metadata[1].vibes == ["energetic"]
    assert expanded.releases[0].artist_ids == ["artist", "second"]
    assert expanded.releases[0].genre_ids == ["jazz"]


def test_build_outputs_auditable_normalized_snapshot_without_touching_catalog(tmp_path):
    base = catalog()
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(base.model_dump_json())
    original = catalog_path.read_bytes()
    input_dir = tmp_path / "inputs"
    input_dir.mkdir()
    text, receipt = page()
    (input_dir / "page.txt").write_text(text)
    (input_dir / "page.json").write_text(json.dumps(receipt))
    output = tmp_path / "output"
    report = build(catalog_path, input_dir, output)
    assert report["matched_releases"] == 1 and report["accepted_pages"] == 1
    assert report["snapshot_paths"] == ["snapshot"]
    assert report["raw_part_paths"] == ["raw.jsonl"]
    rows, manifest, _ = load_snapshot(output / "snapshot")
    assert rows[0]["vibes"] == ["energetic"]
    assert rows[0]["genres"] == []
    assert rows[0]["rating"] is rows[0]["rating_count"] is None
    assert manifest["acquisition_provenance"]["inputs"][0]["receipt"] == receipt
    assert catalog_path.read_bytes() == original
    with pytest.raises(FileExistsError):
        build(catalog_path, input_dir, output)
    (input_dir / "page.txt").write_text(text + "tampered")
    audit_rows, audit, _ = inspect_inputs(catalog_path, input_dir, allow_incomplete=True)
    assert audit_rows == [] and audit["accepted_pages"] == 0
    assert audit["unverified_pages"][0]["file"] == "page.txt"
    with pytest.raises(ValueError, match="hash"):
        build(catalog_path, input_dir, tmp_path / "failed")
    assert not (tmp_path / "failed").exists()


def test_fixture_catalog_cannot_be_enriched():
    base = catalog()
    base.is_fixture = True
    with pytest.raises(ValueError, match="fixture"):
        enrich_rows(base, [parsed_page()])


def test_raw_parts_count_bytes_preserve_complete_records_and_fail_oversized_record():
    rows = [{"name": "Синтетический пример"}, {"name": "另一個例子"}, {"name": "last"}]
    encoded = [(json.dumps(row, ensure_ascii=False) + "\n").encode() for row in rows]
    limit = max(map(len, encoded))
    parts = serialize_parts(rows, limit)
    assert len(parts) > 1
    assert all(len(part) <= limit for part in parts)
    assert b"".join(parts) == b"".join(encoded)
    assert [json.loads(line) for part in parts for line in part.decode().splitlines()] == rows
    assert serialize_parts([], limit) == [b""]
    with pytest.raises(ValueError, match="One evidence record"):
        serialize_parts(rows, 5)


def test_build_shards_all_records_with_independent_verified_snapshot_paths(tmp_path, monkeypatch):
    base = catalog()
    cards = []
    for index in range(3):
        release = base.releases[0].model_copy(deep=True)
        release.id = f"release-{index}"
        release.title = f"Fixture Album {index}"
        release.links = {"aoty": f"https://www.albumoftheyear.org/album/{100+index}-synthetic.php"}
        base.releases.append(release)
        cards += [f"【77†Fixture Artist】【78†Fixture Album {index}】", "2001 • LP"]
    base.releases.pop(0)
    text, receipt = page(cards)
    parsed = parse_index(text, receipt)
    rows, _ = enrich_rows(base, [(parsed, receipt, "page.txt")])
    limit = max(len((json.dumps(row, ensure_ascii=False) + "\n").encode()) for row in rows)
    monkeypatch.setattr("discovery.catalog.vibe_index.MAX_PART_BYTES", limit)
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(base.model_dump_json())
    input_dir = tmp_path / "inputs"
    input_dir.mkdir()
    (input_dir / "page.txt").write_text(text)
    (input_dir / "page.json").write_text(json.dumps(receipt))
    output = tmp_path / "output"
    report = build(catalog_path, input_dir, output)
    assert len(report["snapshot_paths"]) == len(report["raw_part_paths"]) == 3
    parts = [(output / name).read_bytes() for name in report["raw_part_paths"]]
    assert b"".join(parts) == (output / "raw.jsonl").read_bytes()
    assert all(len(part) <= limit for part in parts)
    snapshots = [load_snapshot(output / path) for path in report["snapshot_paths"]]
    records = [record for records, _, _ in snapshots for record in records]
    assert len(records) == len({record["row_id"] for record in records}) == 3
    assert all(record["vibes"] == ["energetic"] and record["rating"] is None for record in records)
    expanded, _, _ = combine(base, snapshots)
    assert {release.id for release in expanded.releases} == {release.id for release in base.releases}
    assert all(release.source_metadata[0].vibes == ["energetic"] for release in expanded.releases)
