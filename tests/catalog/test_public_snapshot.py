"""Authored CSV fixtures, never downloaded music facts or personal taste labels."""

import csv
import hashlib
import io
import json

import pytest

from discovery.catalog.public_snapshot import convert_rows, convert_snapshot


def matteo(**changes):
    return {"artist": "Fixture Artist", "title": "Fixture Album", "rating": "91", "num_ratings": "42",
            "release_date": "May 12, 2001", "genres": "Jazz, Avant-Garde Jazz", "year": "2024",
            "link": "https://www.albumoftheyear.org/album/123-fixture.php", "rating_type": "Critics",
            "review_text": "MUST NOT BE IMPORTED", "cover_url": "https://example.com/cover.jpg", **changes}


def csv_bytes(rows, delimiter=","):
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=list(rows[0]), delimiter=delimiter)
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode()


def manifest(tmp_path, rows, delimiter=","):
    content = csv_bytes(rows, delimiter)
    raw = tmp_path / "synthetic.csv"
    raw.write_bytes(content)
    path = tmp_path / "acquisition.json"
    path.write_text(json.dumps({"source": "aoty", "is_fixture": False, "raw_file": raw.name,
                               "raw_sha256": hashlib.sha256(content).hexdigest(),
                               "source_url": "https://example.com/authored-test-fixture", "observed_at": None}))
    return path, raw


def test_critic_user_roles_are_not_interchanged_and_provenance_is_verifiable(tmp_path):
    path, raw = manifest(tmp_path, [matteo(), matteo(rating_type="Users", rating="72", num_ratings="1,234"),
                                   matteo(link="https://www.albumoftheyear.org/album/456-other.php", title="Other", release_date="")])
    report = convert_snapshot(path, "matteodelseppia", tmp_path / "snapshot")
    rows = [json.loads(line) for line in (tmp_path / "snapshot/releases.jsonl").read_text().splitlines()]
    assert len(rows) == 2
    assert rows[0]["rating"] == 72 and rows[0]["rating_count"] == 1234
    assert rows[1]["rating"] is None and rows[1]["rating_count"] is None
    assert rows[1]["year"] is None and rows[1]["chart_year"] is None
    assert all(row["observed_at"] is None and row["personal_label"] is None for row in rows)
    provenance = report["acquisition_provenance"]
    assert provenance["upstream_raw_sha256"] == hashlib.sha256(raw.read_bytes()).hexdigest()
    assert provenance["upstream_manifest_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert provenance["upstream_manifest"] == json.loads(path.read_text())
    converted = tmp_path / next(name for name in provenance["files"] if name.endswith(".jsonl"))
    assert provenance["files"][converted.name]["sha256"] == hashlib.sha256(converted.read_bytes()).hexdigest()
    assert "MUST NOT BE IMPORTED" not in converted.read_text() and "cover_url" not in converted.read_text()
    assert report["duplicate_source_ids"] == 0


@pytest.mark.parametrize("change, reason", [({"artist": "Other Artist"}, "CONFLICTING_ARTIST_OR_TITLE"),
    ({"title": "Other Title"}, "CONFLICTING_ARTIST_OR_TITLE"),
    ({"release_date": "June 2002"}, "CONFLICTING_RELEASE_YEARS")])
def test_duplicate_id_conflicts_quarantine_entire_group(change, reason):
    rows, quarantine, summary = convert_rows(csv_bytes([matteo(), matteo(**change)]), "matteodelseppia")
    assert not rows and quarantine == [{"source_release_id": "123", "input_rows": [1, 2], "reason": reason}]
    assert summary["quarantined_input_rows"] == 2


def test_conflicting_user_aggregates_keep_metadata_without_arbitrary_score():
    rows, quarantine, summary = convert_rows(csv_bytes([
        matteo(rating_type="Users", rating="50"), matteo(rating_type="Users", rating="90")]), "matteodelseppia")
    assert not quarantine and rows[0]["user_score"] is None and rows[0]["number_of_ratings"] is None
    assert summary["conflicting_user_aggregate_groups"] == 1


def test_yody_metadata_all_genres_unknown_date_and_no_prose(tmp_path):
    path, _ = manifest(tmp_path, [{"artis": "Fixture Jazz Ensemble", "album": "Fixture Sound", "tanggal_rilis": "",
        "genre": ";Spiritual Jazz;|Ambient|;Electronic;", "link_album": "album/789-fixture.php",
        "tracklist_album": "Secret prose", "thumbnail_album": "https://example.com/image"}], ";")
    report = convert_snapshot(path, "yodyamahesa", tmp_path / "snapshot")
    row = json.loads((tmp_path / "snapshot/releases.jsonl").read_text())
    assert row["genres"] == ["Spiritual Jazz", "Ambient", "Electronic"]
    assert row["source_release_id"] == "789" and row["year"] is None
    assert row["rating"] is None and row["rating_count"] is None
    assert report["with_ratings"] == 0
    assert "Secret prose" not in (tmp_path / "snapshot/releases.jsonl").read_text()


@pytest.mark.parametrize("url", ["https://example.com/album/123-fixture.php", "https://www.albumoftheyear.org/artist/123-x/",
                                  "http://[broken", "album/123-fixture.php", ""])
def test_matteo_requires_existing_valid_absolute_source_album_url(url):
    rows, quarantine, _ = convert_rows(csv_bytes([matteo(link=url)]), "matteodelseppia")
    assert not rows and len(quarantine) == 1


@pytest.mark.parametrize("change", ["changed_hash", "fixture", "source"])
def test_unverified_inputs_fail_before_any_derived_file(tmp_path, change):
    path, raw = manifest(tmp_path, [matteo()])
    data = json.loads(path.read_text())
    if change == "changed_hash":
        raw.write_bytes(raw.read_bytes() + b"\n")
    elif change == "fixture":
        data["is_fixture"] = True
    else:
        data["source"] = "rym"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        convert_snapshot(path, "matteodelseppia", tmp_path / "snapshot")
    assert set(item.name for item in tmp_path.iterdir()) == {path.name, raw.name}


def test_existing_snapshot_is_not_overwritten_and_derived_input_can_be_reused(tmp_path):
    path, _ = manifest(tmp_path, [matteo()])
    first = convert_snapshot(path, "matteodelseppia", tmp_path / "first")
    second = convert_snapshot(path, "matteodelseppia", tmp_path / "second")
    assert first["records_sha256"] == second["records_sha256"]
    with pytest.raises(FileExistsError):
        convert_snapshot(path, "matteodelseppia", tmp_path / "first")
