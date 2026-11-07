"""Coverage by default; explicit --live uses three scenarios and at most $0.01."""

import argparse
import hashlib
import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from discovery.catalog import load_catalog
from discovery.config import DEFAULT_CATALOG, PROJECT_ROOT, load_project_env
from discovery.evals.user_recommendations import EvaluationLedger, catalog_coverage, run_case
from discovery.llm import OpenRouterClient, OpenRouterSettings
from discovery.training.serving import artifact_hash, load_optional_artifact
from discovery.web.app import runtime_dir


def markdown_report(report: dict) -> str:
    lines = ["# Проверка пользовательских примеров", "", f"Дата: {report['timestamp']}",
             f"Каталог: {report['catalog_snapshot']}",
             f"Релизов с меткой rage: {report['coverage']['rage_release_count']}", ""]
    if report.get("trials_per_case", 0) == 0:
        lines += ["Только проверка покрытия каталога. Ответы бота не проверены.", ""]
    else:
        lines += [f"Модель: {report['model']}",
                  f"Расход с учётом неразрешённых резервов: ${report['reserved_or_actual_usd']:.6f}",
                  "Один прогон каждого запроса. Проверен сервис чата, не HTTP/браузер.", ""]
    for coverage in report["coverage"]["cases"]:
        positive = coverage["examples"]["positive"]
        missing = [r["name"] for r in positive if not r["catalog_name_match_ids"]]
        lines += [f"## {coverage['id']}", "",
                  "Нет совпадения имени в каталоге: " + (", ".join(missing) or "нет"), ""]
        case = next((c for c in report["cases"] if c["id"] == coverage["id"]), None)
        if case is None:
            continue
        result = case["messages"][-1]
        failed = [name for name, passed in case["grading"]["checks"].items() if not passed]
        lines += [case["input"], "", f"Ответ: {result['text']}", "",
                  "Подтверждение новых фильтров в тесте: " + ("да" if case["test_confirmed_proposal"] else "нет"),
                  "Фильтры: " + json.dumps(case["filters"], ensure_ascii=False), ""]
        for card in result["cards"]:
            lines.append(f"- {', '.join(card['artists'])} — {card['title']} ({card['year']})")
        lines += ["", "Невыполненные проверки: " + (", ".join(failed) or "нет"),
                  "Известность: " + case["grading"]["fame_mix"]["status"],
                  "Музыкальное соответствие: нужна оценка человека.", ""]
    lines += ["Положительный список не исчерпывающий: отсутствие попаданий само по себе не ошибка.",
              "Отрицательные примеры действуют только внутри своего запроса.",
              "Неизвестная известность не считается доказанной малоизвестностью.", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Real OpenRouter calls, total cap $0.01")
    parser.add_argument("--dataset", type=Path, default=PROJECT_ROOT / "data/evals/user-recommendation-cases-v1.json")
    parser.add_argument("--output", type=Path, help="New JSON path; existing reports are never overwritten")
    args = parser.parse_args()
    started = datetime.now(UTC)
    target = args.output or PROJECT_ROOT / "artifacts/evals" / ("user-" + started.strftime("%Y%m%dT%H%M%S%fZ") + ".json")
    if target.exists() or target.with_suffix(".md").exists():
        parser.error("Report path already exists; choose a new path to preserve the baseline")
    load_project_env()
    catalog_path = PROJECT_ROOT / os.environ.get("DISCOVERY_CATALOG", DEFAULT_CATALOG)
    catalog = load_catalog(catalog_path)
    dataset = json.loads(args.dataset.read_text())
    if len(dataset["cases"]) != 3:
        parser.error("This diagnostic is bounded to exactly three user scenarios")
    code = hashlib.sha256()
    for path in sorted((PROJECT_ROOT / "src/discovery").rglob("*.py")):
        code.update(str(path.relative_to(PROJECT_ROOT)).encode())
        code.update(path.read_bytes())
    report = {"timestamp": started.isoformat(), "code_hash": code.hexdigest(),
              "dataset_sha256": hashlib.sha256(args.dataset.read_bytes()).hexdigest(),
              "catalog_sha256": hashlib.sha256(catalog_path.read_bytes()).hexdigest(),
              "catalog_snapshot": catalog.snapshot_id, "coverage": catalog_coverage(dataset, catalog),
              "live_api_attempted": False, "live_api_verified": False,
              "musical_usefulness_verified": False,
              "boundary": "web_chat_service_with_simulated_filter_confirmation",
              "trials_per_case": 1 if args.live else 0, "cases": [], "passed": False}
    if args.live:
        settings = OpenRouterSettings.from_env()
        settings = replace(settings, request_cost_limit=min(settings.request_cost_limit, .01),
                           daily_cost_limit=min(settings.daily_cost_limit, .10))
        ledger = EvaluationLedger(runtime_dir() / "llm-budget.sqlite3")
        artifact, artifact_warning = load_optional_artifact(os.environ.get("DISCOVERY_MODEL_PATH"))
        clients = []

        def factory():
            client = OpenRouterClient(settings, ledger)
            clients.append(client)
            return client

        for case in dataset["cases"]:
            report["cases"].append(run_case(dataset, case, catalog, factory, artifact=artifact))
        report.update(model=settings.model, reserved_or_actual_usd=ledger.run_spend,
                      events=[event for client in clients for event in client.events],
                      live_api_attempted=any(client.events for client in clients),
                      live_api_verified=any(event["status"] == 200
                                           for client in clients for event in client.events),
                      model_artifact_hash=artifact_hash(artifact) if artifact else None,
                      model_artifact_warning=artifact_warning,
                      status="failed" if any(c["grading"]["status"] == "failed" for c in report["cases"])
                      else "needs_human_review")
    else:
        report["status"] = "coverage_only"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    target.with_suffix(".md").write_text(markdown_report(report))
    print(json.dumps({"report": str(target), "status": report["status"],
                      "reserved_or_actual_usd": report.get("reserved_or_actual_usd", 0)}, ensure_ascii=False))
    # A diagnostic with pending human review is not a successful product-quality gate.
    return 1 if report["status"] == "failed" else 0 if not args.live else 2


if __name__ == "__main__":
    raise SystemExit(main())
