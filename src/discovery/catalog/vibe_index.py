"""Offline enrichment from verified, readable AOTY Vibe index receipts.

An index membership is a source annotation, not audio measurement, genre,
rating or current popularity. Only existing, uniquely matched releases qualify.
"""

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

from discovery.catalog import load_catalog
from discovery.catalog.community import build_snapshot
from discovery.catalog.matching import normalized_name
from discovery.catalog.web.common import validate_url
from discovery.contracts import Catalog
from discovery.storage import catalog_digest

VERSION = "aoty-readable-vibe-index-v1"
MAX_PART_BYTES = 20_000_000  # Leave headroom below community.build_snapshot's 25 MB limit.
_LINK = re.compile(r"【\d+†([^】]+)】")
_LINE = re.compile(r"L(\d+): ?(.*)")
_DATE = re.compile(r"((?:18|19|20|21)\d{2})\s*•\s*(.+)")
_FORMATS = {"LP", "EP", "Mixtape", "Single", "Compilation", "Live", "Remix", "Soundtrack",
            "Reissue", "Demo", "Box Set", "DJ Mix", "Music Video", "Video", "Audiobook",
            "Miscellaneous", "Holiday", "Instrumental", "Unofficial"}
_END = {"related", "albums", "artists", "site details", "more albums", "recommended albums",
        "popular user reviews", "recent user reviews", "user reviews", "critic reviews"}


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=2)


def _label(value: str) -> str:
    return " ".join(_LINK.sub(lambda match: match[1].split("†", 1)[0], value).split())


def validate_receipt(receipt: dict, content: bytes) -> None:
    if (not isinstance(receipt, dict) or receipt.get("is_fixture") is not False
            or receipt.get("transport") != "web_readable_text"):
        raise ValueError("Expected a real web_readable_text receipt")
    if hashlib.sha256(content).hexdigest() != receipt.get("text_sha256"):
        raise ValueError("Readable page hash does not match its receipt")
    if len(content) > 5_000_000:
        raise ValueError("Readable index exceeds 5 MB")
    vibe = receipt.get("vibe")
    if not isinstance(vibe, str) or not vibe.strip() or len(vibe) > 100:
        raise ValueError("Missing or invalid Vibe label")
    try:
        retrieved = datetime.fromisoformat(receipt["retrieved_at"])
        parts = urlsplit(receipt["url"])
        path = re.fullmatch(r"/all/releases/vibe/([^/]+)/(?:([1-9]\d*)/)?", parts.path)
        if (retrieved.utcoffset() is None or parts.scheme != "https"
                or parts.hostname not in {"www.albumoftheyear.org", "albumoftheyear.org"}
                or parts.port not in (None, 443) or parts.username or parts.password
                or parts.query or parts.fragment or not path):
            raise ValueError("Invalid Vibe index URL or retrieval timestamp")
        if unquote(path[1]).casefold() not in {vibe.strip().casefold(), vibe.strip().casefold().replace(" ", "-")}:
            raise ValueError("Receipt Vibe does not match the positive index URL")
    except (TypeError, KeyError) as exc:
        raise ValueError("Missing index URL or retrieval timestamp") from exc


def parse_index(text: str, receipt: dict) -> dict:
    """Parse full line-numbered readable output, never link lists or review text."""
    validate_receipt(receipt, text.encode("utf-8"))
    header = text.splitlines()[0] if text else ""
    page = re.search(r"/(\d+)/$", urlsplit(receipt["url"]).path)
    page_title = f"Page {page[1]} - " if page else ""
    if header != f"All Album Releases, Music Releases - {page_title}Album of The Year ({receipt['url']})":
        raise ValueError("Readable index header URL/title differs from receipt")
    numbered = [(int(match[1]), match[2]) for line in text.splitlines()
                if (match := _LINE.fullmatch(line))]
    if not numbered or [number for number, _ in numbered] != list(range(len(numbered))):
        raise ValueError("Expected complete consecutive readable page lines starting at L0")
    lines = [line.strip() for _, line in numbered]
    try:
        plus = lines.index("+ Filter")
        minus = lines.index("- Filter", plus + 1)
        genre = lines.index("Genre", minus + 1)
    except ValueError as exc:
        raise ValueError("Missing index filter controls or Genre boundary") from exc
    if receipt["vibe"].strip().casefold() not in {line.casefold() for line in lines[:plus]}:
        raise ValueError("Expected selected Vibe label is absent before filter controls")
    end = next((index for index in range(genre + 1, len(lines))
                if _label(lines[index]).casefold().lstrip("# ") in _END
                or "NEXT »" in _label(lines[index])), None)
    if end is None:
        raise ValueError("Missing index end boundary; incomplete readable page")
    cards, issues = [], []
    seen = set()
    for index in range(genre + 1, end):
        date_match = _DATE.fullmatch(lines[index])
        if not date_match:
            if " • " in lines[index] and lines[index].rsplit(" • ", 1)[-1] in _FORMATS:
                issues.append({"line": index, "reason": "MISSING_KNOWN_YEAR"})
            continue
        before = [position for position in range(genre + 1, index) if lines[position]]
        if not before or date_match[2] not in _FORMATS:
            issues.append({"line": index, "reason": "MALFORMED_CARD_OR_FORMAT"})
            continue
        title_index = before[-1]
        title_line = lines[title_index]
        links = list(_LINK.finditer(title_line))
        if not links or title_line[links[-1].end():].strip():
            issues.append({"line": index, "reason": "MISSING_LINKED_CARD_TITLE"})
            continue
        title = _label(links[-1][0])
        credit = _label(title_line[:links[-1].start()])
        if not credit and len(before) > 1:
            credit_line = lines[before[-2]]
            credit = _label(credit_line)
            if not _LINK.search(credit_line) and re.fullmatch(r"\(?[\d.,]+\s*[KkMmBb]?\)?", credit):
                credit = ""  # A preceding score/count is not an artist credit.
        if (not credit or not title
                or credit.casefold() in {"advertisement", "critic score", "user score", "genre", "type"}
                or re.search(r"[•【】]", credit)):
            issues.append({"line": index, "reason": "MISSING_FULL_ARTIST_CREDIT"})
            continue
        identity = normalized_name(credit), normalized_name(title), int(date_match[1])
        if identity in seen:
            issues.append({"line": index, "reason": "DUPLICATE_CARD"})
            continue
        seen.add(identity)
        cards.append({"artist_credit": credit, "title": title, "year": int(date_match[1]),
                      "vibe": receipt["vibe"].strip(), "line": index})
    return {"cards": cards, "issues": issues}


def _album_urls(release) -> list[str]:
    urls = set()
    for url in [*release.links.values(),
                *(item.url for item in [*release.observations, *release.historical_observations] if item.url)]:
        try:
            urls.add(validate_url(url, "aoty"))
        except ValueError:
            continue
    return sorted(urls)


def enrich_rows(catalog: Catalog, pages: list[tuple[dict, dict, str]]) -> tuple[list[dict], dict]:
    """Create metadata-only raw rows, accepting exact full credits and unique years."""
    if catalog.is_fixture:
        raise ValueError("Cannot enrich a fixture catalogue with real index receipts")
    artists = {artist.id: artist.name for artist in catalog.artists}
    releases = {release.id: release for release in catalog.releases}
    by_identity = defaultdict(set)
    for release in catalog.releases:
        if release.year is None:
            continue
        credits = {" / ".join(artists[aid] for aid in release.artist_ids)}
        credits.update(metadata.artist_credit for metadata in release.source_metadata
                       if metadata.source == "aoty"
                       and normalized_name(metadata.title) == normalized_name(release.title))
        for credit in credits:
            by_identity[normalized_name(credit), normalized_name(release.title), release.year].add(release.id)
    matches, unmatched = defaultdict(list), []
    for parsed, receipt, filename in pages:
        for card in parsed["cards"]:
            identity = normalized_name(card["artist_credit"]), normalized_name(card["title"]), card["year"]
            candidates = sorted(by_identity.get(identity, set()))
            reason = "NO_EXACT_CATALOG_MATCH" if not candidates else "AMBIGUOUS_CATALOG_MATCH" if len(candidates) != 1 else None
            urls = _album_urls(releases[candidates[0]]) if len(candidates) == 1 else []
            if reason is None and not urls:
                reason = "NO_EXISTING_AOTY_ALBUM_URL"
            if reason is None and len({re.search(r"/album/(\d+)", url)[1] for url in urls}) > 1:
                reason = "AMBIGUOUS_EXISTING_AOTY_IDENTITY"
            if reason:
                unmatched.append({**card, "input_file": filename, "reason": reason, "candidate_ids": candidates})
                continue
            matches[candidates[0]].append((card, receipt, filename, urls[0]))
    rows = []
    for release_id, items in sorted(matches.items()):
        release = releases[release_id]
        evidence = {}
        for card, receipt, filename, url in items:
            item = {"field": "vibes", "vibe": card["vibe"], "index_page_url": receipt["url"],
                    "retrieved_at": receipt["retrieved_at"], "text_sha256": receipt["text_sha256"],
                    "input_file": filename, "line": card["line"], "transport": "web_readable_text",
                    "match_rule": "exact_full_credit_title_known_year",
                    "index_artist_credit": card["artist_credit"], "index_title": card["title"],
                    "index_year": card["year"], "catalog_release_id": release_id,
                    "existing_catalog_album_url": url, "external_album_id_read_from_index": False}
            evidence[_json(item)] = item
        rows.append({"artist_name": " / ".join(artists[aid] for aid in release.artist_ids),
                     "album_name": release.title, "release_date": str(release.year),
                     "link_to_album": items[0][3], "genres": [], "user_score": None,
                     "number_of_ratings": None, "vibes": sorted({card["vibe"] for card, _, _, _ in items}),
                     "source_evidence": list(evidence.values())})
    return rows, {"matched_releases": len(rows), "matched_index_cards": sum(map(len, matches.values())),
                  "matched_release_vibe_pairs": sum(len(row["vibes"]) for row in rows),
                  "unmatched_cards": unmatched,
                  "unmatched_reason_counts": dict(Counter(item["reason"] for item in unmatched))}


def inspect_inputs(catalog_path: Path, input_dir: Path, *, allow_incomplete: bool = False) -> tuple[list[dict], dict, list[dict]]:
    """Read-only plan; audit mode reports and excludes unfinished/unverified files."""
    catalog = load_catalog(catalog_path)
    if catalog.is_fixture:
        raise ValueError("Cannot enrich a fixture catalogue")
    paths = sorted(input_dir.glob("*.txt"))
    if not paths:
        raise ValueError("No readable index pages found")
    pages, rejected, inputs, unverified = [], [], [], []
    for path in paths:
        try:
            content = path.read_bytes()
            receipt_bytes = path.with_suffix(".json").read_bytes()
            receipt = json.loads(receipt_bytes)
            validate_receipt(receipt, content)
        except (ValueError, OSError) as exc:
            if not allow_incomplete:
                raise
            unverified.append({"file": path.name, "reason": str(exc)})
            continue
        inputs.append({"file": path.name, "receipt": receipt,
                       "receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest()})
        try:
            parsed = parse_index(content.decode("utf-8"), receipt)
        except (ValueError, UnicodeError) as exc:
            rejected.append({"file": path.name, "reason": str(exc)})
            continue
        pages.append((parsed, receipt, path.name))
    rows, summary = enrich_rows(catalog, pages)
    report = {"version": VERSION, "catalog_sha256": catalog_digest(catalog), "input_pages": len(paths),
              "accepted_pages": len(pages), "rejected_pages": rejected,
              "unverified_pages": unverified, "parsed_index_cards": sum(len(page[0]["cards"]) for page in pages), **summary,
              "parse_issues": [{"file": filename, **issue} for parsed, _, filename in pages for issue in parsed["issues"]],
              "limitations": ["Index membership is subjective source annotation, not measured audio or popularity.",
                              "No external album IDs are present in readable cards; matching is exact metadata only.",
                              "No new catalogue entities, ratings, release dates or genres are inferred."]}
    return rows, report, inputs


def build(catalog_path: Path, input_dir: Path, output_dir: Path) -> dict:
    if output_dir.exists():
        raise FileExistsError("Vibe index output must be a new directory")
    rows, report, inputs = inspect_inputs(catalog_path, input_dir)
    parts = serialize_parts(rows)
    payload = b"".join(parts)
    part_names = (["raw.jsonl"] if len(parts) == 1 else
                  [f"raw.part-{number:04d}.jsonl" for number in range(1, len(parts) + 1)])
    snapshot_paths = (["snapshot"] if len(parts) == 1 else
                      [f"snapshots/part-{number:04d}" for number in range(1, len(parts) + 1)])
    files = {"raw.jsonl": {"sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)}}
    files.update({name: {"sha256": hashlib.sha256(part).hexdigest(), "bytes": len(part)}
                  for name, part in zip(part_names, parts, strict=True)})
    report.update(raw_bytes=len(payload), raw_part_paths=part_names, snapshot_paths=snapshot_paths,
                  max_raw_part_bytes=MAX_PART_BYTES)
    provenance = {"source": "aoty", "is_fixture": False, "converter_version": VERSION,
                  "catalog_sha256": report["catalog_sha256"], "inputs": inputs,
                  "raw_part_paths": part_names, "snapshot_paths": snapshot_paths, "files": files}
    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "raw.jsonl").write_bytes(payload)
    for name, part in zip(part_names, parts, strict=True):
        if name != "raw.jsonl":
            (output_dir / name).write_bytes(part)
    (output_dir / "provenance.json").write_text(_json(provenance) + "\n")
    (output_dir / "report.json").write_text(_json(report) + "\n")
    for name, snapshot_path in zip(part_names, snapshot_paths, strict=True):
        build_snapshot("aoty", output_dir / name, output_dir / "provenance.json", output_dir / snapshot_path)
    return report


def serialize_parts(rows: list[dict], max_bytes: int | None = None) -> list[bytes]:
    """Split only between complete UTF-8 JSONL records, preserving every record."""
    limit = MAX_PART_BYTES if max_bytes is None else max_bytes
    if not isinstance(limit, int) or not 0 < limit <= 20_000_000:
        raise ValueError("Raw part limit must be between 1 and 20,000,000 bytes")
    parts, current = [], bytearray()
    for row in rows:
        record = (json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        if len(record) > limit:
            raise ValueError("One evidence record exceeds the raw part limit; cannot safely split a record")
        if current and len(current) + len(record) > limit:
            parts.append(bytes(current))
            current.clear()
        current.extend(record)
    if current or not parts:
        parts.append(bytes(current))
    return parts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument("--input-dir", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--output-dir", type=Path)
    mode.add_argument("--audit", action="store_true", help="Read-only statistics; unverified/incomplete receipts are excluded")
    args = parser.parse_args()
    try:
        if args.audit:
            _, report, _ = inspect_inputs(args.catalog, args.input_dir, allow_incomplete=True)
        else:
            report = build(args.catalog, args.input_dir, args.output_dir)
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Error: {exc}\n")
    print(json.dumps({**{key: report[key] for key in ("input_pages", "accepted_pages", "parsed_index_cards", "matched_releases",
                                                "matched_index_cards", "matched_release_vibe_pairs", "unmatched_reason_counts")},
                      "unverified_pages": len(report["unverified_pages"]),
                      "rejected_pages": len(report["rejected_pages"])}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
