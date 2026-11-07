"""Replay confirmed filters from a live diagnostic; never invoke the model.

This isolates ranking changes. It does not measure fresh language understanding.
"""
import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from discovery.catalog import load_catalog
from discovery.config import DEFAULT_CATALOG, PROJECT_ROOT, load_project_env
from discovery.contracts import UserProfile
from discovery.evals.user_recommendations import grade_case, name_key
from discovery.storage import Store
from discovery.training.serving import artifact_hash, load_optional_artifact
from discovery.web.chat import create_chat, handle_message
from discovery.web.schemas import ChatCreate, Filters, MessageRequest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output already exists")
    load_project_env()
    path = PROJECT_ROOT / os.environ.get("DISCOVERY_CATALOG", DEFAULT_CATALOG)
    dataset_path = PROJECT_ROOT / "data/evals/user-recommendation-cases-v1.json"
    recording = json.loads(args.recording.read_text())
    for field, source in (("catalog_sha256", path), ("dataset_sha256", dataset_path)):
        if recording[field] != hashlib.sha256(source.read_bytes()).hexdigest():
            parser.error(f"Changed {field}; this replay requires the original data")
    catalog = load_catalog(path)
    dataset = json.loads(dataset_path.read_text())
    artifact, warning = load_optional_artifact(os.environ.get("DISCOVERY_MODEL_PATH"))
    if (artifact_hash(artifact) if artifact else None) != recording.get("model_artifact_hash"):
        parser.error("Changed personal model; this replay requires the original artifact")
    results = []
    for case in dataset["cases"]:
        old = next(c for c in recording["cases"] if c["id"] == case["id"])
        if not old["test_confirmed_proposal"]:
            parser.error("Only recorded, explicitly confirmed filter proposals can be replayed")
        filters = Filters.model_validate(old["filters"])
        with TemporaryDirectory(prefix="replay-user-filters-") as directory:
            store = Store(Path(directory) / "isolated.sqlite3")
            names = {name_key(n) for n in case["listener_artist_names"]}
            store.set_base_profile(UserProfile(seed_artist_ids_by_genre={"hip-hop": [
                a.id for a in catalog.artists if name_key(a.name) in names]}))
            req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(catalog=catalog)))
            chat = create_chat(ChatCreate(filters=filters), req, store)
            result = handle_message(store, chat.id, MessageRequest(message_id="replay-" + case["id"], apply_filters=True),
                                    catalog, artifact=artifact)
            results.append({"id": case["id"], "filters": filters.model_dump(),
                            "result": result.model_dump(mode="json"),
                            "grading": grade_case(dataset, case, catalog, result, filters)})
    code = hashlib.sha256()
    for path in sorted((PROJECT_ROOT / "src/discovery").rglob("*.py")):
        code.update(str(path.relative_to(PROJECT_ROOT)).encode())
        code.update(path.read_bytes())
    report = {"timestamp": datetime.now(UTC).isoformat(), "boundary": "offline_confirmed_filter_replay",
              "source_recording": str(args.recording),
              "source_recording_sha256": hashlib.sha256(args.recording.read_bytes()).hexdigest(),
              "catalog_sha256": recording["catalog_sha256"], "dataset_sha256": recording["dataset_sha256"],
              "code_hash": code.hexdigest(), "live_api_verified": False, "reserved_or_actual_usd": 0,
              "artifact_warning": warning, "cases": results, "passed": False}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"report": str(args.output), "boundary": report["boundary"], "cost": 0}))


if __name__ == "__main__":
    main()
