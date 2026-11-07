"""Live conversational regression: sound wish, follow-up, and an unsupported exact demand."""

import json
import os
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from web_live_smoke import SmokeLedger

from discovery.catalog import load_catalog
from discovery.config import DEFAULT_CATALOG, PROJECT_ROOT, load_project_env
from discovery.llm import OpenRouterClient, OpenRouterSettings
from discovery.storage import Store
from discovery.web.app import runtime_dir
from discovery.web.chat import create_chat, get_chat, handle_message, update_filters
from discovery.web.schemas import ChatCreate, Filters, MessageRequest


def main():
    load_project_env()
    settings = OpenRouterSettings.from_env()
    settings = replace(settings, request_cost_limit=min(settings.request_cost_limit, .01),
                       daily_cost_limit=min(settings.daily_cost_limit, .10))
    catalog = load_catalog(PROJECT_ROOT / os.environ.get('DISCOVERY_CATALOG', DEFAULT_CATALOG))
    ledger = SmokeLedger(runtime_dir() / 'llm-budget.sqlite3')
    events, results = [], []

    def factory():
        client = OpenRouterClient(settings, ledger)
        close = client.close

        def recorded_close():
            events.extend(client.events)
            close()

        client.close = recorded_close
        return client

    with TemporaryDirectory(prefix='discovery-sound-smoke-') as directory:
        store = Store(Path(directory) / 'user.sqlite3')
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(catalog=catalog)))
        chat = create_chat(ChatCreate(filters=Filters(exclude_known_artists=True)), request, store)
        confirmed = None
        scenarios = [
            ('sound-reference', 'Нужно больше жесткости в звучании, как например у Death Grips'),
            ('followup', 'Теперь предложи другие варианты в этом звучании, но более неожиданные'),
            ('strict-unsupported', 'Только записи со строго 137 BPM, проверенными по аудио'),
        ]
        for name, prompt in scenarios:
            body = MessageRequest(message_id="sound-" + name, text=prompt)
            result = handle_message(store, chat.id, body, catalog, factory)
            calls = len(events)
            assert handle_message(store, chat.id, body, catalog, factory) == result
            assert len(events) == calls
            if name == 'sound-reference':
                proposal = result.proposed_filters
                passed = bool(proposal and proposal.genre_scope_id == 'hip-hop'
                              and proposal.exclude_known_artists
                              and set(proposal.locked_subgenre_ids) & {'industrial-hip-hop', 'hardcore'}
                              and not result.cards and not get_chat(store, chat.id).filters.locked_subgenre_ids)
                if passed:
                    confirmed = proposal
                    update_filters(chat.id, proposal, request, store)
            elif name == 'followup':
                filters = get_chat(store, chat.id).filters
                passed = bool(confirmed and result.cards and filters.mode == 'surprise'
                              and filters.locked_subgenre_ids == confirmed.locked_subgenre_ids
                              and filters.exclude_known_artists)
                if passed:
                    allowed = set().union(*(catalog.descendant_ids(gid) for gid in filters.locked_subgenre_ids))
                    releases = {release.id: release for release in catalog.releases}
                    passed = all(allowed.intersection(releases[card.release_id].genre_ids) for card in result.cards)
            else:
                passed = bool('не поддерживаются' in result.text and not result.cards
                              and not result.proposed_filters)
            results.append({'scenario': name, 'passed': passed and result.status == 'completed',
                            'status': result.status, 'error_code': result.error_code,
                            'text': result.text,
                            'proposed_filters': result.proposed_filters.model_dump() if result.proposed_filters else None})
        assert not store.feedback()
    report = {'model': settings.model, 'scenarios': results, 'passed': all(r['passed'] for r in results),
              'reserved_or_actual_usd': ledger.smoke_spend, 'events': events}
    output = PROJECT_ROOT / 'artifacts/web/sound-smoke.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: v for k, v in report.items() if k != 'events'}, ensure_ascii=False, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
