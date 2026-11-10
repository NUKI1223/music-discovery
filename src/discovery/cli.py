"""Local commands; importing or evaluating never silently calls a paid API."""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from discovery.catalog.listenbrainz import load_with_listeners
from discovery.config import DEFAULT_CATALOG, load_project_env
from discovery.contracts import RequestIntent
from discovery.service import discovery_metrics, recommend
from discovery.storage import Store, database_path


def training_records(store: Store, *, cutoff: datetime | None = None) -> list[dict]:
    """Export corrections available at cutoff, using the original decision features."""
    from discovery.training import feature_values

    if cutoff is not None and cutoff.utcoffset() is None:
        raise ValueError("Training export cutoff must include a timezone")
    decisions = {d.id: d for d in store.decisions()}
    latest = {}
    familiar = {}
    for event in store.feedback():
        if cutoff is not None and (event.recorded_at > cutoff
                                   or decisions[event.decision_id].created_at > cutoff):
            continue
        key = event.decision_id, event.release_id
        if event.kind == "known":
            familiar[key] = True
        elif event.familiar_before_recommendation is not None:
            familiar[key] = event.familiar_before_recommendation
        if event.kind == "listened_mixed":
            latest.pop(event.release_id, None)  # neutral: supersedes any earlier label
        elif event.kind in {"listened_like", "listened_dislike"}:
            latest[event.release_id] = event
    rows = []
    fixture_by_catalog: dict[str, bool] = {}
    for rid, event in latest.items():
        # The model predicts whether NEW music will be liked; ratings of music the
        # user already knew before the recommendation are not that outcome.
        if familiar.get((event.decision_id, rid)) is True:
            continue
        decision = decisions[event.decision_id]
        item = next(i for i in decision.result.items if i.release_id == rid)
        if decision.catalog_hash not in fixture_by_catalog:
            fixture_by_catalog[decision.catalog_hash] = store.get_catalog(decision.catalog_hash).is_fixture
        rows.append({
            "release_id": rid, "genre_scope_id": decision.request.genre_scope_id,
            "decision_at": decision.created_at.isoformat(),
            "available_at": event.recorded_at.isoformat(),
            "label": int(event.kind == "listened_like"),
            "features": feature_values(item.components),
            "feature_snapshot_id": decision.catalog_hash + ":" + decision.profile.version,
            "taxonomy_version": decision.taxonomy_version,
            "feature_version": decision.feature_version,
            "is_fixture": fixture_by_catalog[decision.catalog_hash],
        })
    return rows


def main() -> None:
    load_project_env()
    parser = argparse.ArgumentParser(description="Music Discovery local tools")
    parser.add_argument("--db", default=os.environ.get("DISCOVERY_DB", "runtime/discovery.sqlite3"))
    sub = parser.add_subparsers(dest="command", required=True)
    rec = sub.add_parser("recommend")
    rec.add_argument("--catalog", default=os.environ.get("DISCOVERY_CATALOG", DEFAULT_CATALOG))
    rec.add_argument("--genre", required=True)
    rec.add_argument("--mode", choices=["close", "explore", "surprise"], default="explore")
    rec.add_argument("--obscurity", choices=["prefer", "require"], default="prefer")
    rec.add_argument("--request-id")
    rec.add_argument("--bandit", action="store_true", help="Experimental fifth slot")
    rec.add_argument("--model", help="Optional JSON artifact; unvalidated genres use baseline")
    preview = sub.add_parser("agent-preview", help="Optional live read-only agent; requires LLM budgets")
    preview.add_argument("--catalog", default=os.environ.get("DISCOVERY_CATALOG", DEFAULT_CATALOG))
    preview.add_argument("--genre", required=True)
    preview.add_argument("--text", required=True)
    sub.add_parser("metrics")
    export = sub.add_parser("export")
    export.add_argument("--output", required=True)
    train_export = sub.add_parser("export-training")
    train_export.add_argument("--output", required=True)
    train_export.add_argument("--cutoff", type=datetime.fromisoformat,
                              help="Historical availability cutoff, ISO 8601 with timezone")
    backup = sub.add_parser("backup")
    backup.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        if args.command == "recommend":
            catalog = load_with_listeners(args.catalog)
            store = Store(database_path(args.db, catalog))
            artifact = None
            if args.model:
                from discovery.training import load_optional_artifact

                artifact, warning = load_optional_artifact(args.model)
                if warning:
                    print(warning, file=sys.stderr)
            decision = recommend(store, catalog, RequestIntent(
                genre_scope_id=args.genre, mode=args.mode, obscurity=args.obscurity,
            ), request_id=args.request_id, use_bandit=args.bandit, artifact=artifact)
            store.mark_shown(decision)
            print(decision.model_dump_json(indent=2))
        elif args.command == "metrics":
            print(json.dumps(discovery_metrics(Store(args.db)), indent=2, ensure_ascii=False))
        elif args.command in {"export", "export-training"}:
            target = Path(args.output)
            if target.exists():
                raise ValueError("Output already exists; choose a new filename")
            target.parent.mkdir(parents=True, exist_ok=True)
            store = Store(args.db)
            payload = (store.export() if args.command == "export"
                       else training_records(store, cutoff=args.cutoff))
            target.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
            print(f"Saved {target}")
        elif args.command == "backup":
            Store(args.db).backup(args.output)
            print(f"Saved {args.output}")
        elif args.command == "agent-preview":
            from discovery.agent_preview import agent_preview
            from discovery.llm import BudgetLedger, LLMError, OpenRouterClient, OpenRouterSettings

            catalog = load_with_listeners(args.catalog)
            store = Store(database_path(args.db, catalog))
            client = None
            try:
                client = OpenRouterClient(OpenRouterSettings.from_env(),
                                          BudgetLedger(store.path.parent / "llm-budget.sqlite3"))
                report = agent_preview(catalog, RequestIntent(genre_scope_id=args.genre),
                                       store.profile(), client, args.text)
                print(json.dumps(report, ensure_ascii=False, indent=2))
            except LLMError as exc:
                parser.exit(2, f"{exc.code}: {exc}\n")
            finally:
                if client:
                    client.close()
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
