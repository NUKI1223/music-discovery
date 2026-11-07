"""Three regression cases, including live free-form requests after a legacy refusal."""

import argparse
import json
import os
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from web_live_smoke import SmokeLedger

from discovery.catalog import load_catalog
from discovery.config import DEFAULT_CATALOG, PROJECT_ROOT, load_project_env
from discovery.contracts import UserProfile
from discovery.llm import OpenRouterClient, OpenRouterSettings
from discovery.storage import Store
from discovery.web.app import runtime_dir
from discovery.web.chat import create_chat, get_chat, handle_message, now, update_filters
from discovery.web.schemas import ChatCreate, Filters, Message, MessageRequest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=["surprise-after-refusal", "personal-exclusion-proposal"])
    args = parser.parse_args()
    load_project_env()
    settings = OpenRouterSettings.from_env()
    settings = replace(settings, request_cost_limit=min(settings.request_cost_limit, .01),
                       daily_cost_limit=min(settings.daily_cost_limit, .10))
    catalog = load_catalog(PROJECT_ROOT / os.environ.get("DISCOVERY_CATALOG", DEFAULT_CATALOG))
    ledger = SmokeLedger(runtime_dir() / "llm-budget.sqlite3")
    events, results, responses = [], [], []

    def factory():
        client = OpenRouterClient(settings, ledger)
        original_close = client.close
        original_complete = client.complete

        def complete(*args, **kwargs):
            response = original_complete(*args, **kwargs)
            try:
                responses.append(json.loads(response.get("content", "")))
            except (TypeError, ValueError):
                responses.append({"invalid_json": True})
            return response

        def close():
            events.extend(client.events)
            original_close()

        client.close = close
        client.complete = complete
        return client

    with TemporaryDirectory(prefix="discovery-known-artists-") as directory:
        store = Store(Path(directory) / "user.sqlite3")
        config = json.loads((PROJECT_ROOT / "configs/pilot.json").read_text())["genres"]["hip-hop"]
        seeds = {a.id for a in catalog.artists if a.id in config["seed_artist_ids"]
                 or a.name.casefold() in {n.casefold() for n in config["seed_artist_names"]}}
        store.set_base_profile(UserProfile(seed_artist_ids_by_genre={"hip-hop": sorted(seeds)}))
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(catalog=catalog)))
        chat = create_chat(ChatCreate(filters=Filters()), request, store)
        text = "Мне не нужны альбомы тех исполнителей, которых я знаю"
        # Reproduce the old refusal without charging or modifying a real user's history.
        with store.connection() as conn:
            for role, content in [("user", text), ("assistant", "Такие пожелания пока не поддерживаются: исключить известных исполнителей. Доступны жанр, поджанры, новизна и малоизвестность.")]:
                item = Message(id="legacy-" + role, role=role, text=content, created_at=now())
                conn.execute("INSERT INTO web_messages(id,chat_id,payload) VALUES (?,?,?)",
                             (item.id, chat.id, item.model_dump_json()))
        for name, prompt in [("surprise-after-refusal", "Более неожиданное"),
                              ("personal-exclusion-proposal", "Не рекомендуй исполнителей, которых я уже знаю"),
                              ("confirmed-exclusion-surprise", "Подбери ещё, более неожиданное")]:
            if args.scenario and name != args.scenario:
                continue
            body = MessageRequest(message_id=name, text=prompt)
            result = handle_message(store, chat.id, body, catalog, factory)
            count = len(events)
            assert handle_message(store, chat.id, body, catalog, factory) == result
            assert len(events) == count
            if name == "personal-exclusion-proposal":
                passed = bool(result.proposed_filters and result.proposed_filters.exclude_known_artists
                              and not result.cards and not get_chat(store, chat.id).filters.exclude_known_artists)
                if passed:
                    update_filters(chat.id, result.proposed_filters, request, store)
            else:
                passed = bool(result.cards and get_chat(store, chat.id).filters.mode == "surprise")
                if name == "confirmed-exclusion-surprise":
                    decision = store.decisions()[-1] if store.decisions() else None
                    passed = passed and bool(decision and seeds <= set(decision.request.excluded_artist_ids))
            results.append({"scenario": name, "passed": passed and result.status == "completed",
                            "status": result.status, "error_code": result.error_code})
    report = {"model": settings.model, "scenarios": results, "passed": all(r["passed"] for r in results),
              "reserved_or_actual_usd": ledger.smoke_spend, "events": events,
              "action_responses": responses}
    output = PROJECT_ROOT / "artifacts/web/known-artists-smoke.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "events"}, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
