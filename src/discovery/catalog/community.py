"""Normalize published community snapshots without inventing capture dates or IDs."""

import argparse
import ast
import csv
import hashlib
import io
import json
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from discovery.catalog.web.common import exact_count, validate_url


def clean(value) -> str:
    return " ".join(str(value).split()) if value is not None else ""


def number(value, scale):
    value = clean(value)
    if not value or value.casefold() in {"none", "null", "n/a"}:
        return None
    parsed = float(value)
    if not 0 <= parsed <= scale:
        raise ValueError("Rating outside source scale")
    return parsed


def source_evidence(value: object) -> list[dict]:
    """Copy explicit JSON evidence without accepting missing/opaque objects."""
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError("Expected a list of source evidence objects")
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValueError("Source evidence must contain finite JSON values") from exc
    return json.loads(encoded)


def normalize_row(source: str, raw: dict, *, row_number: int, input_sha256: str) -> dict:
    """Keep artist credit intact; neither title matching nor chart year is identity evidence."""
    if source not in {"rym", "aoty"}:
        raise ValueError("Unsupported source")
    if raw.get("is_fixture", False) is not False:
        raise ValueError("Fixture row cannot be imported into a real snapshot")
    if "source" in raw and raw["source"] != source:
        raise ValueError("Row source conflicts with snapshot source")
    artist = clean(raw.get("artist_name"))
    title = clean(raw.get("release_name" if source == "rym" else "album_name"))
    if not artist or not title:
        raise ValueError("Missing title or artist credit")
    url, source_id = None, None
    secondary, descriptors = [], []
    if source == "rym":
        genres = [clean(x) for x in clean(raw.get("primary_genres")).split(",") if clean(x)]
        secondary = [clean(x) for x in clean(raw.get("secondary_genres")).split(",") if clean(x)]
        descriptors = [clean(x) for x in clean(raw.get("descriptors")).split(",") if clean(x)]
        scale, score = 5, raw.get("avg_rating")
        count_text = clean(raw.get("rating_count"))
    else:
        encoded = raw.get("genres")
        if isinstance(encoded, str):
            if len(encoded) > 10_000:
                raise ValueError("Genre field exceeds limit")
            encoded = ast.literal_eval(encoded) if encoded.strip() else []
        if encoded is None:
            encoded = []
        if not isinstance(encoded, list) or any(not isinstance(x, str) for x in encoded):
            raise ValueError("Expected list of source genre strings")
        genres = [clean(x) for x in encoded if clean(x)]
        secondary_labels = raw.get("secondary_genres", [])
        if (not isinstance(secondary_labels, list)
                or any(not isinstance(label, str) for label in secondary_labels)):
            raise ValueError("Expected list of secondary source genre strings")
        secondary = [clean(label) for label in secondary_labels if clean(label)]
        link = clean(raw.get("link_to_album"))
        url = validate_url("https://www.albumoftheyear.org" + link if link.startswith("/")
                           else link, "aoty")
        source_id = re.search(r"/album/(\d+)", url)[1]
        scale = 100
        score = raw.get("user_score_float")
        if not clean(score):
            score = raw.get("user_score")
        count_text = clean(raw.get("number_of_ratings"))
    rating = number(score, scale)
    count = exact_count(count_text)
    warnings = ["THIRD_PARTY_SNAPSHOT_NOT_INDEPENDENTLY_VERIFIED", "SOURCE_CAPTURE_TIME_UNKNOWN"]
    if count_text and count is None:
        warnings.append("COUNT_NOT_EXACT")
    if rating is not None and count == 0:
        rating = None
        warnings.append("SCORE_WITH_ZERO_COUNT_IGNORED")
    release_date = clean(raw.get("release_date"))
    years = re.findall(r"\b(?:18|19|20|21)\d{2}\b", release_date)
    result = {
        "row_id": f"{source}:{input_sha256[:16]}:{row_number}",
        "source": source, "source_release_id": source_id, "canonical_release_id": None,
        "source_url": url, "title": title, "artist_credit": artist,
        "year": int(years[0]) if len(years) == 1 else None,
        "release_date_text": release_date or None,
        "chart_year": raw.get("list_year") if source == "aoty" else None,
        "genres": list(dict.fromkeys(genres)), "secondary_genres": list(dict.fromkeys(secondary)),
        "descriptors": list(dict.fromkeys(descriptors)),
        "rating": rating, "rating_scale": scale, "rating_count": count,
        "rating_count_text": count_text or None,
        "rating_evidence": "publisher_reported_community_aggregate",
        "count_semantics_verified": False,
        "observed_at": None, "is_fixture": False, "personal_label": None,
        "input_row": row_number, "input_sha256": input_sha256,
        "warnings": warnings,
    }
    # Optional enrichments must not change serialized legacy normalized rows.
    if source == "aoty":
        vibes = raw.get("vibes", [])
        if not isinstance(vibes, list) or any(not isinstance(label, str) for label in vibes):
            raise ValueError("Expected a list of Vibe strings")
        vibes = list(dict.fromkeys(clean(label) for label in vibes if clean(label)))
        if vibes:
            result["vibes"] = vibes
    evidence = source_evidence(raw.get("source_evidence", []))
    if evidence:
        result["source_evidence"] = evidence
    return result


def build_snapshot(source: str, input_path: Path, provenance_path: Path, output_dir: Path) -> dict:
    if input_path.stat().st_size > 25_000_000:
        raise ValueError("Input exceeds 25 MB")
    content = input_path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    if not isinstance(provenance, dict):
        raise ValueError("Expected acquisition provenance object")
    if provenance.get("is_fixture") is not False or provenance.get("source") != source:
        raise ValueError("Provenance must describe a real snapshot of the selected source")
    declared_files = provenance.get("files", {})
    if isinstance(declared_files, dict):
        candidates = [entry for name, entry in declared_files.items()
                      if Path(name).name == input_path.name]
    elif isinstance(declared_files, list):
        candidates = [entry for entry in declared_files
                      if Path(entry.get("local_path", "")).name == input_path.name]
    else:
        candidates = []
    if len(candidates) != 1 or candidates[0].get("sha256") != digest:
        raise ValueError("Input hash does not match acquisition provenance")
    if source == "rym":
        # newline='' preserves embedded newlines in quoted source cells.
        with io.StringIO(content.decode("utf-8-sig"), newline="") as handle:
            raw_rows = list(csv.DictReader(handle))
    elif source == "aoty":
        raw_rows = [json.loads(line) for line in content.decode("utf-8").splitlines() if line.strip()]
    else:
        raise ValueError("Unsupported source")
    records, quarantine = [], []
    for index, row in enumerate(raw_rows, start=1):
        try:
            if not isinstance(row, dict):
                raise ValueError("Expected source row object")
            records.append(normalize_row(source, row, row_number=index, input_sha256=digest))
        except (ValueError, TypeError, SyntaxError, RecursionError) as exc:
            quarantine.append({"input_row": index, "reason": str(exc), "raw": row})
    ids = Counter(row["source_release_id"] for row in records if row["source_release_id"])
    for row in records:
        if ids[row["source_release_id"]] > 1:
            row["warnings"].append("DUPLICATE_SOURCE_ID_RETAINED_FOR_REVIEW")
    payload = "".join(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n" for row in records)
    report = {
        "schema_version": 1, "normalizer_version": "community-historical-v1",
        "dataset_kind": "historical_community_metadata", "source": source,
        "is_fixture": False, "personal_labels": 0, "live_scrape": False,
        "normalized_at": datetime.now(UTC).isoformat(), "observed_at": None,
        "input_file": str(input_path), "input_sha256": digest,
        "records_file": "releases.jsonl",
        "records_sha256": hashlib.sha256(payload.encode()).hexdigest(),
        "input_rows": len(raw_rows), "accepted_rows": len(records),
        "quarantined_rows": len(quarantine), "unique_source_ids": len(ids) if ids else None,
        "duplicate_source_ids": sum(n > 1 for n in ids.values()),
        "with_genres": sum(bool(row["genres"]) for row in records),
        "with_ratings": sum(row["rating"] is not None for row in records),
        "with_integer_counts": sum(row["rating_count"] is not None for row in records),
        "genre_counts": dict(Counter(g for row in records for g in row["genres"]).most_common()),
        "acquisition_provenance": provenance,
        "limitations": [
            "Publisher snapshots are historical and are not independently verified live observations.",
            "Retrieval/commit dates do not establish source capture time.",
            "Chart-selected datasets are biased toward popular or highly rated releases.",
            "No automatic MusicBrainz identity or internal genre-taxonomy mapping is performed.",
            "Community aggregates are not personal preference labels or current obscurity evidence.",
            "Public availability does not establish redistribution rights; see acquisition provenance.",
        ],
    }
    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "releases.jsonl").write_text(payload, encoding="utf-8")
    (output_dir / "quarantine.json").write_text(json.dumps(quarantine, ensure_ascii=False, indent=2),
                                               encoding="utf-8")
    (output_dir / "manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                                             encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, choices=["rym", "aoty"])
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--provenance", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        report = build_snapshot(args.source, args.input, args.provenance, args.output_dir)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Error: {exc}\n")
    print(json.dumps({key: report[key] for key in ("source", "input_rows", "accepted_rows",
                                                  "quarantined_rows", "records_sha256")}, indent=2))


if __name__ == "__main__":
    main()
