"""Explicit three-scenario live smoke, sharing the real ledger and capped at $0.01."""

import json
import os
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from discovery.catalog import load_catalog
from discovery.config import DEFAULT_CATALOG, PROJECT_ROOT, load_project_env
from discovery.llm import BudgetLedger, LLMError, OpenRouterClient, OpenRouterSettings
from discovery.storage import Store
from discovery.web.chat import create_chat, get_chat, handle_message
from discovery.web.schemas import ChatCreate, Filters, MessageRequest


class SmokeLedger(BudgetLedger):
    """A second bound for this run; reservations still use the shared daily ledger."""

    def __init__(self, path):
        super().__init__(path)
        self.smoke_spend = 0.0
        self.reservations = {}

    def reserve(self, amount, daily_limit):
        if self.smoke_spend + amount > .01:
            raise LLMError("SMOKE_BUDGET", "Достигнут лимит live-проверки.")
        ident = super().reserve(amount, daily_limit)
        self.smoke_spend += amount
        self.reservations[ident] = amount
        return ident

    def settle(self, reservation, actual):
        super().settle(reservation, actual)
        self.smoke_spend += actual - self.reservations[reservation]


def run(settings, ledger):
    """Run the same three scenarios with injected settings and a shared spend cap."""
    catalog = load_catalog(PROJECT_ROOT / os.environ.get("DISCOVERY_CATALOG", DEFAULT_CATALOG))
    events = []

    def client_factory():
        client = OpenRouterClient(settings, ledger)
        original_close = client.close

        def close():
            events.extend(client.events)
            original_close()

        client.close = close
        return client

    results = []
    with TemporaryDirectory(prefix="discovery-live-smoke-") as temporary:
        store = Store(Path(temporary) / "discovery.sqlite3")
        chat = create_chat(ChatCreate(filters=Filters()),
                           SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(catalog=catalog))), store)
        initial = handle_message(store, chat.id, MessageRequest(message_id="live-initial-free",
                                  apply_filters=True), catalog)
        assert initial.cards, initial.text
        first = initial.cards[0]
        scenarios = [
            ("followup", "Ещё варианты, более неожиданное, но внутри выбранного жанра", None),
            ("previous-card", "Почему этот альбом?", first.release_id),
            ("unsupported", "Хочу только альбомы, записанные под дождём в 2025 году", None),
        ]
        for name, prompt, release_id in scenarios:
            body = MessageRequest(message_id="live-smoke-" + name, text=prompt, release_id=release_id)
            result = handle_message(store, chat.id, body, catalog, client_factory)
            before_replay = len(events)
            replay = handle_message(store, chat.id, body, catalog, client_factory)
            assert replay == result and len(events) == before_replay
            if name == "followup":
                passed = bool(result.cards) and not ({c.release_id for c in initial.cards}
                                                     & {c.release_id for c in result.cards})
            elif name == "previous-card":
                passed = bool(result.cards) and result.cards[0].release_id == first.release_id
            else:
                passed = "не поддерживаются" in result.text and not result.cards
            results.append({"scenario": name, "status": result.status, "error_code": result.error_code,
                            "passed": passed and result.status == "completed", "cards": len(result.cards),
                            "genre": get_chat(store, chat.id).filters.genre_scope_id,
                            "idempotency_passed": True})
    report = {"model": settings.model, "scenarios": results,
              "reserved_or_actual_usd": ledger.smoke_spend, "events": events,
              "passed": all(r["passed"] for r in results)}
    return report


def main():
    load_project_env()
    settings = OpenRouterSettings.from_env()
    settings = replace(settings, request_cost_limit=min(settings.request_cost_limit, .01),
                       daily_cost_limit=min(settings.daily_cost_limit, .10))
    db = Path(os.environ.get("DISCOVERY_DB", "runtime/discovery.sqlite3"))
    if not db.is_absolute():
        db = PROJECT_ROOT / db
    report = run(settings, SmokeLedger(db.parent / "llm-budget.sqlite3"))
    target = PROJECT_ROOT / "artifacts/web/live-smoke.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key != "events"},
                     ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
