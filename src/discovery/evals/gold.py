"""Gold data isolation; never exposes sound descriptions as ranking features.

Name matching here is deliberately conservative exclusion, not entity resolution.
The same rules must be applied to historical catalogs before training.
"""

import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

from discovery.config import PROJECT_ROOT
from discovery.contracts import Catalog
from discovery.evals.user_recommendations import name_key
from discovery.storage import catalog_digest

SOUND_PATH = PROJECT_ROOT / "data/evals/rage-sound-annotations-v1.json"
CASES_PATH = PROJECT_ROOT / "data/evals/user-recommendation-cases-v1.json"


def _read(path: Path) -> tuple[dict, str]:
    raw = path.read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def _aoty_id(url: str) -> str | None:
    parsed = urlsplit(url)
    if parsed.hostname not in {"www.albumoftheyear.org", "albumoftheyear.org"}:
        return None
    parts = parsed.path.strip("/").split("/")
    if len(parts) != 2 or parts[0] != "album":
        return None
    ident = parts[1].split("-", 1)[0]
    return "aoty:" + ident if ident.isdigit() else None


def build_gold_manifest(catalog: Catalog, sound_path: Path | None = None,
                        cases_path: Path | None = None) -> dict:
    sound, sound_hash = _read(sound_path or SOUND_PATH)
    cases, cases_hash = _read(cases_path or CASES_PATH)
    releases = sound["releases"]
    if not releases or not cases["cases"]:
        raise ValueError("Gold collections cannot be empty")
    ids = [r["id"] for r in releases]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate gold release ID")
    for row in releases:
        if _aoty_id(row["aoty_url"]) != row["id"]:
            raise ValueError("Gold source URL and release ID disagree")
        if row["annotation_status"] not in {"annotated", "awaiting_user", "not_listened"}:
            raise ValueError("Unknown annotation status")
        if (row.get("normalized_sound") is not None) != (row["annotation_status"] == "annotated"):
            raise ValueError("Gold annotations must distinguish unknown from observed")
        if row.get("preference_label") is not None:
            raise ValueError("Sound descriptions are not taste labels")
    names = {name_key(r["artist"]) for r in releases}
    # Include source references and contexts, including examples awaiting identity
    # verification: quarantine is broader than the eligible evaluation subset.
    names.update(name_key(r["name"]) for r in cases.get("artist_references", []))
    for case in cases["cases"]:
        for field in ("positive_artists", "negative_artists", "listener_artist_names"):
            names.update(name_key(n) for n in case.get(field, []))
        names.update(name_key(r["name"]) for r in case.get("confirmed_artist_references", []))
        names.update(name_key(r["name"]) for r in case.get("excluded_examples", []))
    # Request-only anchors may not occur in positive/negative or listener lists.
    # Token boundaries avoid accidental Che/Cheese exclusions.
    import re

    requests = [case.get("request", "").casefold() for case in cases["cases"]]
    for artist in catalog.artists:
        pattern = r"(?<!\w)" + re.escape(artist.name.casefold()) + r"(?!\w)"
        if any(re.search(pattern, request) for request in requests):
            names.add(name_key(artist.name))
    protected_artists = {a.id for a in catalog.artists if name_key(a.name) in names}
    protected = set(ids)
    for release in catalog.releases:
        source_ids = {_aoty_id(url) for url in release.links.values()}
        source_ids.update(_aoty_id(o.url) for o in release.historical_observations if o.url)
        # Imported source row IDs may have a prefix; URLs remain authoritative
        # when present. Matching raw numeric IDs is exclusion-only.
        source_ids.update("aoty:" + m.source_row_id.removeprefix("aoty:")
                          for m in release.source_metadata if m.source == "aoty")
        if set(release.artist_ids) & protected_artists or source_ids & set(ids):
            protected.add(release.id)
    manifest = {
        "schema_version": 1,
        "role": "regression_gold_not_training_or_blind_test",
        "source_hashes": {"sound": sound_hash, "word_cases": cases_hash},
        "word_document_sha256": cases.get("source", {}).get("sha256"),
        "catalog_hash": catalog_digest(catalog),
        "protected_release_ids": sorted(protected),
        "protected_artist_ids": sorted(protected_artists),
        "protected_artist_name_keys": sorted(names),
        "sound_release_count": sum(r["annotation_status"] == "annotated" for r in releases),
        "pending_sound_release_count": sum(r["annotation_status"] != "annotated" for r in releases),
        "recommendation_case_count": len(cases["cases"]),
        "matching_policy": "conservative exclusion; name match does not prove identity",
    }
    manifest["digest"] = hashlib.sha256(json.dumps(
        manifest, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return manifest


def sound_readiness(sound_path: Path | None = None) -> dict:
    """Report the real boundary instead of grading descriptions against themselves."""
    sound, digest = _read(sound_path or SOUND_PATH)
    annotated = [r for r in sound["releases"] if r["annotation_status"] == "annotated"]
    return {
        "status": "needs_audio_and_independent_predictions",
        "passed": False,
        "dataset_sha256": digest,
        "labelled_release_ids": [r["id"] for r in annotated],
        "label_scope": "release; do not propagate to every track",
        "musical_quality_measured": False,
        "reason": "Album descriptions alone do not provide audio features or model predictions",
        "independent_holdout": False,
    }
