"""Convert verified public CSV snapshots into historical AOTY community metadata.

No network access, chart-year fallback, inferred IDs, review prose or taste labels.
"""

import argparse
import csv
import hashlib
import io
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from .community import build_snapshot, clean
from .web.common import validate_url

VERSION = "public-aoty-csv-v1"
FORMATS = {"matteodelseppia", "yodyamahesa"}


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _years(value: str) -> set[int]:
    return {int(year) for year in re.findall(r"\b(?:18|19|20|21)\d{2}\b", value)}


def _row(raw: dict, format_name: str, index: int) -> tuple[str, dict]:
    matteo = format_name == "matteodelseppia"
    artist = clean(raw.get("artist" if matteo else "artis"))
    title = clean(raw.get("title" if matteo else "album"))
    if not artist or not title:
        raise ValueError("MISSING_ARTIST_OR_TITLE")
    link = clean(raw.get("link" if matteo else "link_album"))
    if not matteo and (link.startswith("album/") or link.startswith("/album/")):
        link = "https://www.albumoftheyear.org/" + link.lstrip("/")
    url = validate_url(link, "aoty")
    source_id = str(int(re.search(r"/album/(\d+)", url)[1]))
    genre_text = clean(raw.get("genres" if matteo else "genre"))
    genres = [clean(value).strip("; ") for value in re.split("," if matteo else r";?\|", genre_text)]
    role = clean(raw.get("rating_type")).casefold() if matteo else "metadata"
    return source_id, {
        "artist_name": artist, "album_name": title, "link_to_album": url,
        "release_date": clean(raw.get("release_date" if matteo else "tanggal_rilis")),
        "genres": [value for value in genres if value],
        "user_score": clean(raw.get("rating")) if role == "users" else None,
        "number_of_ratings": clean(raw.get("num_ratings")) if role == "users" else None,
        "role": role, "input_row": index,
    }


def convert_rows(content: bytes, format_name: str) -> tuple[list[dict], list[dict], dict]:
    """Group only by source URL ID; quarantine contradictory identity or dated years."""
    if format_name not in FORMATS:
        raise ValueError("Unsupported public CSV format")
    groups = defaultdict(list)
    quarantine = []
    roles = Counter()
    input_rows = 0
    with io.StringIO(content.decode("utf-8-sig"), newline="") as handle:
        reader = csv.DictReader(handle, delimiter="," if format_name == "matteodelseppia" else ";")
        required = ({"artist", "title", "link", "rating_type", "release_date", "genres", "rating", "num_ratings"}
                    if format_name == "matteodelseppia" else {"artis", "album", "link_album", "tanggal_rilis", "genre"})
        if not required <= set(reader.fieldnames or []):
            raise ValueError("Missing required CSV columns")
        for index, raw in enumerate(reader, 1):
            input_rows += 1
            try:
                source_id, row = _row(raw, format_name, index)
                groups[source_id].append(row)
                roles[row["role"]] += 1
            except (ValueError, TypeError) as exc:
                # Deliberately omit unrelated prose/cover columns from quarantine.
                quarantine.append({"input_rows": [index], "reason": str(exc)})
    rows = []
    aggregate_conflicts = 0
    for source_id, group in groups.items():
        identity = {(row["artist_name"].casefold(), row["album_name"].casefold()) for row in group}
        years = set().union(*(_years(row["release_date"]) for row in group))
        reason = "CONFLICTING_ARTIST_OR_TITLE" if len(identity) != 1 else "CONFLICTING_RELEASE_YEARS" if len(years) > 1 else None
        if reason:
            quarantine.append({"source_release_id": source_id, "input_rows": [row["input_row"] for row in group],
                               "reason": reason})
            continue
        users = [row for row in group if row["role"] == "users"]
        selected = (users or group)[0]
        result = {key: selected[key] for key in ("artist_name", "album_name", "link_to_album", "user_score", "number_of_ratings")}
        # A known release date can come from either role; neither chart year nor retrieval time is used.
        result["release_date"] = next((row["release_date"] for row in [selected, *group] if _years(row["release_date"])), selected["release_date"])
        result["genres"] = list(dict.fromkeys(genre for row in group for genre in row["genres"]))
        result["upstream_rows"] = [row["input_row"] for row in group]
        if len({(row["user_score"], row["number_of_ratings"]) for row in users}) > 1:
            result["user_score"] = result["number_of_ratings"] = None
            result["aggregate_warning"] = "CONFLICTING_USER_AGGREGATES_METADATA_ONLY"
            aggregate_conflicts += 1
        rows.append(result)
    return rows, quarantine, {"input_rows": input_rows, "source_role_rows": dict(roles),
                               "converted_rows": len(rows), "quarantined_groups": len(quarantine),
                               "quarantined_input_rows": sum(len(row["input_rows"]) for row in quarantine),
                               "conflicting_user_aggregate_groups": aggregate_conflicts}


def _write_verified(path: Path, content: bytes) -> None:
    """Permit reproducible reruns, never overwrite a different derived artifact."""
    if path.exists():
        if path.read_bytes() != content:
            raise FileExistsError(f"Existing derived artifact differs: {path}")
        return
    with path.open("xb") as handle:
        handle.write(content)


def convert_snapshot(manifest_path: Path, format_name: str, output_dir: Path) -> dict:
    if output_dir.exists():
        raise FileExistsError(f"Output directory already exists: {output_dir}")
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    if not isinstance(manifest, dict) or manifest.get("source") != "aoty" or manifest.get("is_fixture") is not False:
        raise ValueError("Manifest must describe a real AOTY snapshot")
    raw_name = manifest.get("raw_file")
    if not isinstance(raw_name, str) or Path(raw_name).name != raw_name:
        raise ValueError("Manifest raw_file must be a sibling filename")
    raw_path = manifest_path.parent / raw_name
    if raw_path.stat().st_size > 50_000_000:
        raise ValueError("Public CSV exceeds 50 MB")
    raw_bytes = raw_path.read_bytes()
    digest = hashlib.sha256(raw_bytes).hexdigest()
    if manifest.get("raw_sha256") != digest:
        raise ValueError("Raw CSV hash does not match acquisition manifest")
    rows, quarantine, summary = convert_rows(raw_bytes, format_name)
    payload = ("".join(_json(row) + "\n" for row in rows)).encode()
    if len(payload) > 25_000_000:
        raise ValueError("Converted JSONL exceeds community input limit")
    prefix = f"{raw_path.stem}.{VERSION}"
    converted = raw_path.with_name(prefix + ".jsonl")
    provenance_path = raw_path.with_name(prefix + ".provenance.json")
    quarantine_path = raw_path.with_name(prefix + ".quarantine.json")
    quarantine_bytes = (_json(quarantine) + "\n").encode()
    provenance = {
        "source": "aoty", "is_fixture": False, "converter_version": VERSION, "format": format_name,
        "upstream_manifest_file": manifest_path.name,
        "upstream_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "upstream_raw_file": raw_name, "upstream_raw_sha256": digest,
        "upstream_manifest": manifest, "conversion": summary,
        "files": {converted.name: {"sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)},
                  quarantine_path.name: {"sha256": hashlib.sha256(quarantine_bytes).hexdigest()}},
        "limitations": [
            "Source role rows are flattened by numeric AOTY album URL ID; genres are unioned.",
            "Only Users rows supply user_score/number_of_ratings; Critics-only rows are metadata-only.",
            "Conflicting user aggregates are cleared; no capture timestamp exists to choose the newest.",
            "Conflicting artist/title identities or dated release years are quarantined.",
            "Chart year is never a release-year fallback; no current popularity or personal labels inferred.",
            "CSV review prose, track lists and cover URLs are not imported.",
        ],
    }
    _write_verified(converted, payload)
    _write_verified(quarantine_path, quarantine_bytes)
    _write_verified(provenance_path, (_json(provenance) + "\n").encode())
    return build_snapshot("aoty", converted, provenance_path, output_dir)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--format", required=True, choices=sorted(FORMATS))
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        report = convert_snapshot(args.manifest, args.format, args.output_dir)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Error: {exc}\n")
    print(json.dumps({key: report[key] for key in ("source", "accepted_rows", "quarantined_rows", "records_sha256")}, indent=2))


if __name__ == "__main__":
    main()
