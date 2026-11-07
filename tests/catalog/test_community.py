import hashlib
import json

import pytest

from discovery.catalog.community import build_snapshot, normalize_row


def aoty_row(**changes):
    return {"album_name": "Synthetic Album", "artist_name": "A / B",
            "genres": "['Experimental Hip Hop', 'Jazz Rap']", "user_score_float": 88.45,
            "number_of_ratings": "1,234", "link_to_album": "/album/123-example.php",
            "release_date": None, "list_year": 2024, **changes}


def normalize(source, row):
    return normalize_row(source, row, row_number=1, input_sha256="0" * 64)


def test_historical_dates_ids_and_credit_are_not_invented():
    row = normalize("aoty", aoty_row())
    assert row["year"] is None  # A chart year is not a release date.
    assert row["chart_year"] == 2024
    assert row["observed_at"] is None
    assert row["canonical_release_id"] is None
    assert row["source_release_id"] == "123"
    assert row["artist_credit"] == "A / B"
    assert row["personal_label"] is None
    assert row["rating"] == 88.45
    assert row["rating_count"] == 1234
    assert row["count_semantics_verified"] is False


def test_rym_preserves_genre_descriptor_spaces_and_partial_date():
    row = normalize("rym", {"release_name": "Test", "artist_name": "Artist",
                    "primary_genres": "\n Abstract Hip Hop,\n Jazz Rap ",
                    "descriptors": " male vocals, melancholic ", "release_date": "June 1997",
                    "avg_rating": "3.75", "rating_count": "25.3K"})
    assert row["genres"] == ["Abstract Hip Hop", "Jazz Rap"]
    assert row["descriptors"] == ["male vocals", "melancholic"]
    assert row["release_date_text"] == "June 1997"
    assert row["year"] == 1997
    assert row["source_url"] is None
    assert row["source_release_id"] is None
    assert row["rating_scale"] == 5
    assert row["rating_count"] is None
    assert row["rating_count_text"] == "25.3K"


@pytest.mark.parametrize("score", [101, -1, float("nan"), float("inf")])
def test_invalid_ratings_rejected(score):
    with pytest.raises(ValueError):
        normalize("aoty", aoty_row(user_score_float=score))


def test_zero_count_never_retains_mean():
    row = normalize("aoty", aoty_row(number_of_ratings=0))
    assert row["rating"] is None
    assert "SCORE_WITH_ZERO_COUNT_IGNORED" in row["warnings"]


def write_input(tmp_path):
    source = tmp_path / "raw.jsonl"
    source.write_text("\n".join(json.dumps(row) for row in [aoty_row(), aoty_row(),
                      aoty_row(user_score_float=101)]) + "\n")
    provenance = tmp_path / "provenance.json"
    payload = {"source": "aoty", "is_fixture": False,
               "files": {"raw.jsonl": {"sha256": hashlib.sha256(source.read_bytes()).hexdigest()}}}
    provenance.write_text(json.dumps(payload))
    return source, provenance


def test_export_quarantines_invalid_rows_preserves_duplicates_and_no_overwrite(tmp_path):
    source, provenance = write_input(tmp_path)
    output = tmp_path / "output"
    result = build_snapshot("aoty", source, provenance, output)
    assert result["input_rows"] == 3
    assert result["accepted_rows"] == 2
    assert result["quarantined_rows"] == 1
    assert result["unique_source_ids"] == 1
    rows = [json.loads(line) for line in (output / "releases.jsonl").read_text().splitlines()]
    assert rows[0]["row_id"] != rows[1]["row_id"]
    assert "DUPLICATE_SOURCE_ID_RETAINED_FOR_REVIEW" in rows[0]["warnings"]
    assert hashlib.sha256((output / "releases.jsonl").read_bytes()).hexdigest() == result["records_sha256"]
    with pytest.raises(FileExistsError):
        build_snapshot("aoty", source, provenance, output)


@pytest.mark.parametrize("change", ["fixture", "wrong_source", "changed_input"])
def test_provenance_mismatch_is_rejected_before_export(tmp_path, change):
    source, provenance = write_input(tmp_path)
    data = json.loads(provenance.read_text())
    if change == "fixture":
        data["is_fixture"] = True
    elif change == "wrong_source":
        data["source"] = "rym"
    else:
        source.write_text(source.read_text() + "\n")
    provenance.write_text(json.dumps(data))
    output = tmp_path / "output"
    with pytest.raises(ValueError):
        build_snapshot("aoty", source, provenance, output)
    assert not output.exists()


def test_genres_are_not_executable(tmp_path):
    marker = tmp_path / "executed"
    with pytest.raises((ValueError, SyntaxError)):
        normalize("aoty", aoty_row(genres=f"__import__('pathlib').Path({str(marker)!r}).touch()"))
    assert not marker.exists()


@pytest.mark.parametrize("changes", [{"is_fixture": True}, {"source": "rym"}])
def test_rows_cannot_override_source_or_fixture_boundary(changes):
    with pytest.raises(ValueError):
        normalize("aoty", aoty_row(**changes))


def test_optional_vibes_and_source_evidence_preserve_legacy_rows_and_stay_separate():
    legacy = normalize("aoty", aoty_row())
    assert "vibes" not in legacy and "source_evidence" not in legacy
    assert normalize("aoty", aoty_row(vibes=[], source_evidence=[])) == legacy
    evidence = [{"url": "https://www.albumoftheyear.org/album/123-example.php",
                 "retrieved_at": "2026-11-02T00:00:00Z", "field": "vibes", "sha256": "a" * 64}]
    enriched = normalize("aoty", aoty_row(vibes=[" energetic ", "dense", "energetic", " "],
                                          source_evidence=evidence))
    assert enriched["vibes"] == ["energetic", "dense"]
    assert enriched["genres"] == legacy["genres"]
    assert enriched["descriptors"] == []
    assert enriched["source_evidence"] == evidence
    evidence[0]["field"] = "changed"
    assert enriched["source_evidence"][0]["field"] == "vibes"


@pytest.mark.parametrize("changes", [{"vibes": "['energetic']"}, {"vibes": [None]},
    {"vibes": None}, {"source_evidence": None}, {"source_evidence": [None]},
    {"source_evidence": [{"value": float("nan")}]}, {"source_evidence": [{"value": object()}]}])
def test_invalid_optional_source_enrichment_is_rejected(changes):
    with pytest.raises(ValueError):
        normalize("aoty", aoty_row(**changes))


def test_optional_aoty_secondary_genres_leave_legacy_flat_rows_identical():
    legacy = normalize("aoty", aoty_row())
    assert normalize("aoty", aoty_row(secondary_genres=[])) == legacy
    enriched = normalize("aoty", aoty_row(secondary_genres=[" Art Rock ", "Art Rock", " ", "Ambient"]))
    assert enriched["genres"] == legacy["genres"]
    assert enriched["secondary_genres"] == ["Art Rock", "Ambient"]


@pytest.mark.parametrize("value", [None, "['Art Rock']", {"Art Rock": True}, [None], [1]])
def test_aoty_secondary_genres_require_a_list_of_strings(value):
    with pytest.raises(ValueError, match="secondary source genre"):
        normalize("aoty", aoty_row(secondary_genres=value))
