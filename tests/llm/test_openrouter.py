import json
import time
from dataclasses import replace

import httpx
import pytest

from discovery.contracts import Artist, Catalog, Genre, Release, RequestIntent
from discovery.llm import BudgetLedger, LLMError, OpenRouterClient, OpenRouterSettings


def settings(**changes):
    return replace(OpenRouterSettings('test-secret', 'test/model', 1, 2, 1, 10), **changes)


@pytest.fixture
def catalog():
    return Catalog(snapshot_id='test', genres=[Genre(id='hip-hop', name='Hip hop'),
        Genre(id='abstract', name='Abstract', parent_ids=['hip-hop']), Genre(id='rock', name='Rock')],
        artists=[Artist(id='a', name='Artist')], releases=[Release(id='r', title='Release', artist_ids=['a'],
        genre_ids=['abstract'], year=2020)])


def parsed(**changes):
    return dict(genre_scope_id='hip-hop', mode='explore', obscurity='prefer', count=5,
                locked_subgenre_ids=[], excluded_release_ids=[], excluded_artist_ids=[],
                unsupported_preferences=[]) | changes


def response(content, **extra):
    return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant',
        'content': json.dumps(content) if not isinstance(content, str) else content}}], **extra})


def client(tmp_path, handler, **changes):
    return OpenRouterClient(settings(**changes), BudgetLedger(tmp_path / 'cost.sqlite'),
                            httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda _: None)


def test_parser_preserves_ui_constraints_and_does_not_send_history(tmp_path, catalog):
    requests = []
    def handle(req):
        requests.append(req)
        return response(parsed(mode='surprise', obscurity='require', locked_subgenre_ids=['abstract']))
    llm = client(tmp_path, handle)
    default = RequestIntent(genre_scope_id='hip-hop', obscurity='require', locked_subgenre_ids=['abstract'],
                            excluded_release_ids=['r'], excluded_artist_ids=['a'])
    result = llm.parse_request('Удиви меня', catalog, default)
    assert result.mode == 'surprise'
    assert result.obscurity == 'require'
    assert result.locked_subgenre_ids == ['abstract']
    assert result.excluded_release_ids == ['r']
    assert result.excluded_artist_ids == ['a']
    payload = json.loads(requests[0].content)
    assert payload['provider']['require_parameters'] is True
    assert payload['response_format']['json_schema']['strict'] is True
    sent_genres = json.loads(payload['messages'][1]['content'])['genres']
    assert {g['id'] for g in sent_genres} == {'hip-hop', 'abstract'}
    assert 'test-secret' not in repr(llm.events)
    assert 'Удиви' not in repr(llm.events)
    assert 'test-secret' not in repr(llm.settings)


@pytest.mark.parametrize('patch', [
    {'obscurity': 'prefer', 'locked_subgenre_ids': ['abstract']},
    {'obscurity': 'require', 'locked_subgenre_ids': []},
    {'obscurity': 'require', 'locked_subgenre_ids': ['hip-hop']},
])
def test_hard_filter_conflict_is_reported_instead_of_silent_override(tmp_path, catalog, patch):
    llm = client(tmp_path, lambda _: response(parsed(**patch)))
    default = RequestIntent(genre_scope_id='hip-hop', obscurity='require', locked_subgenre_ids=['abstract'])
    with pytest.raises(LLMError) as err:
        llm.parse_request('Расширь жанр и найди что-нибудь популярное', catalog, default)
    assert err.value.code == 'CONSTRAINT_CONFLICT'
    assert len(llm.events) == 1
    assert default.locked_subgenre_ids == ['abstract'] and default.obscurity == 'require'


@pytest.mark.parametrize('patch,code', [({'genre_scope_id': 'rock'}, 'CONSTRAINT_CONFLICT'),
    ({'unsupported_preferences': ['energy']}, 'UNSUPPORTED_FILTER'),
    ({'locked_subgenre_ids': ['rock']}, 'INVALID_SCHEMA'),
    ({'excluded_release_ids': ['fake']}, 'INVALID_SCHEMA')])
def test_parser_rejects_unsupported_intents(tmp_path, catalog, patch, code):
    llm = client(tmp_path, lambda _: response(parsed(**patch)))
    with pytest.raises(LLMError) as err:
        llm.parse_request('новое', catalog, RequestIntent(genre_scope_id='hip-hop'))
    assert err.value.code == code
    assert len(llm.events) <= 2


def test_one_schema_repair(tmp_path, catalog):
    responses = iter([response('broken json'), response(parsed(count=3))])
    llm = client(tmp_path, lambda _: next(responses))
    assert llm.parse_request('три', catalog, RequestIntent(genre_scope_id='hip-hop')).count == 3
    assert len(llm.events) == 2


def test_unified_catalog_intent_fits_existing_input_budget(tmp_path):
    from discovery.catalog import load_catalog
    from discovery.config import DEFAULT_CATALOG

    catalog = load_catalog(DEFAULT_CATALOG)
    requests = []

    def handle(req):
        requests.append(req)
        return response(parsed(mode='surprise'))

    llm = client(tmp_path, handle)
    result = llm.parse_request('Удиви меня в пределах хип-хопа', catalog,
                               RequestIntent(genre_scope_id='hip-hop'))
    assert result.mode == 'surprise'
    assert len(requests) == 1
    payload = json.loads(requests[0].content)
    sent = json.loads(payload['messages'][1]['content'])['genres']
    assert {g['id'] for g in sent} == catalog.descendant_ids('hip-hop')
    assert len(sent) < len(catalog.genres)


@pytest.mark.parametrize('status,code', [(401, 'AUTH'), (403, 'AUTH'), (400, 'PROVIDER_REJECTED'),
                                       (302, 'PROVIDER_REJECTED')])
def test_no_retry_for_permanent_errors(tmp_path, status, code):
    llm = client(tmp_path, lambda _: httpx.Response(status, text='private secret in provider error'))
    with pytest.raises(LLMError) as err:
        llm.complete([{'role': 'user', 'content': 'hello'}], budget=llm.new_budget())
    assert err.value.code == code
    assert 'private secret' not in str(err.value)
    assert len(llm.events) == 1


def test_transient_retry_then_success(tmp_path):
    responses = iter([httpx.Response(429, headers={'Retry-After': '2'}), response({'ok': True}, usage={'cost': .001})])
    llm = client(tmp_path, lambda _: next(responses))
    sleeps = []
    llm.sleep = sleeps.append
    budget = llm.new_budget()
    llm.complete([{'role': 'user', 'content': 'hello'}], budget=budget)
    assert budget.attempts == 2 and budget.turns == 1 and budget.retries == 1
    assert sleeps == [2]
    assert llm.ledger.total() == pytest.approx(llm.settings.attempt_reservation + .001)


def test_schema_error_429_timeout_is_bounded_and_reserves_unknown_cost(tmp_path, catalog):
    attempts = []
    def handle(req):
        attempts.append(req)
        if len(attempts) == 1:
            return response('{')
        if len(attempts) == 2:
            return httpx.Response(429)
        raise httpx.ReadTimeout('sensitive request detail', request=req)
    llm = client(tmp_path, handle)
    with pytest.raises(LLMError) as err:
        llm.parse_request('новое', catalog, RequestIntent(genre_scope_id='hip-hop'))
    assert err.value.code == 'PROVIDER_UNAVAILABLE'
    assert len(attempts) == 3
    assert llm.ledger.total() == pytest.approx(3 * llm.settings.attempt_reservation)


def test_daily_reservations_survive_restart(tmp_path):
    reserve = settings().attempt_reservation
    llm = client(tmp_path, lambda _: response('ok'), daily_cost_limit=reserve * 1.5)
    llm.complete([{'role': 'user', 'content': 'hello'}], budget=llm.new_budget())
    restarted = client(tmp_path, lambda _: pytest.fail('No second HTTP request'), daily_cost_limit=reserve * 1.5)
    with pytest.raises(LLMError, match='Дневной'):
        restarted.complete([{'role': 'user', 'content': 'hello'}], budget=restarted.new_budget())


def test_request_cost_rejects_before_http(tmp_path):
    llm = client(tmp_path, lambda _: pytest.fail('No HTTP'), request_cost_limit=.0000001)
    with pytest.raises(LLMError) as err:
        llm.complete([{'role': 'user', 'content': 'hello'}], budget=llm.new_budget())
    assert err.value.code == 'REQUEST_BUDGET'
    assert llm.ledger.total() == 0


def test_actual_price_over_reservation_stops(tmp_path):
    llm = client(tmp_path, lambda _: response('ok', usage={'cost': 2}))
    with pytest.raises(LLMError) as err:
        llm.complete([{'role': 'user', 'content': 'hello'}], budget=llm.new_budget())
    assert err.value.code == 'RATE_MISMATCH'
    assert llm.ledger.total() == 2
    with pytest.raises(LLMError) as err:
        llm.complete([{'role': 'user', 'content': 'again'}], budget=llm.new_budget())
    assert err.value.code == 'BUDGET_SUSPENDED'
    assert len(llm.events) == 1


def test_deadline_refuses_long_retry_after(tmp_path):
    llm = client(tmp_path, lambda _: httpx.Response(429, headers={'Retry-After': '600'}))
    llm.sleep = lambda _: pytest.fail('Do not sleep beyond deadline')
    with pytest.raises(LLMError) as err:
        llm.complete([{'role': 'user', 'content': 'hello'}], budget=llm.new_budget())
    assert err.value.code == 'DEADLINE'


def test_config_missing_fails_closed(monkeypatch):
    monkeypatch.delenv('OPENROUTER_API_KEY', raising=False)
    with pytest.raises(LLMError) as err:
        OpenRouterSettings.from_env()
    assert err.value.code == 'CONFIG'


def test_token_budget_and_input_limits(tmp_path):
    llm = client(tmp_path, lambda _: pytest.fail('No HTTP'), request_token_limit=1)
    with pytest.raises(LLMError) as err:
        llm.complete([{'role': 'user', 'content': 'hello'}], budget=llm.new_budget())
    assert err.value.code == 'TOKEN_BUDGET'
    with pytest.raises(LLMError) as err:
        llm.complete([{'role': 'user', 'content': 'x' * 70000}], budget=llm.new_budget())
    assert err.value.code == 'INPUT_LIMIT'


def test_wall_deadline_does_not_retry_inflight_request(tmp_path):
    calls = []
    def slow(req):
        calls.append(req)
        time.sleep(.1)
        return response('late', usage={'cost': 0})
    llm = client(tmp_path, slow, attempt_timeout=.02)
    started = time.monotonic()
    with pytest.raises(LLMError) as err:
        llm.complete([{'role': 'user', 'content': 'hello'}], budget=llm.new_budget())
    assert err.value.code == 'DEADLINE'
    assert time.monotonic() - started < .09
    assert len(calls) == 1
    assert llm.ledger.total() == pytest.approx(llm.settings.attempt_reservation)


def test_expired_deadline_rejects_before_reservation(tmp_path):
    llm = client(tmp_path, lambda _: pytest.fail('No HTTP'))
    budget = llm.new_budget()
    budget.started -= 46
    with pytest.raises(LLMError) as err:
        llm.complete([{'role': 'user', 'content': 'hello'}], budget=budget)
    assert err.value.code == 'DEADLINE'
    assert llm.ledger.total() == 0


def test_agent_http_six_attempts_and_two_retries_global(tmp_path):
    responses = iter([httpx.Response(500), response('one'), httpx.Response(503),
                      response('two'), response('three'), response('four')])
    llm = client(tmp_path, lambda _: next(responses))
    budget = llm.new_budget(agent=True)
    for _ in range(4):
        llm.complete([{'role': 'user', 'content': 'hello'}], budget=budget)
    assert (budget.turns, budget.attempts, budget.retries) == (4, 6, 2)
    with pytest.raises(LLMError) as err:
        llm.complete([{'role': 'user', 'content': 'hello'}], budget=budget)
    assert err.value.code == 'TURN_LIMIT'


def test_explanation_selects_only_verifiable_claims(tmp_path, catalog):
    from discovery.llm import available_claims
    valid = next(f for f in available_claims(catalog, ['r']) if f.field == 'year')
    llm = client(tmp_path, lambda _: response({'claims': [valid.model_dump()]}))
    assert llm.explain_releases(catalog, ['r']) == {'r': ['Год релиза в каталоге: 2020.']}
    forged = valid.model_copy(update={'value': 1990})
    bad = client(tmp_path, lambda _: response({'claims': [forged.model_dump()]}))
    with pytest.raises(LLMError) as err:
        bad.explain_releases(catalog, ['r'])
    assert err.value.code == 'UNSUPPORTED_CLAIM'
    assert len(bad.events) == 2


def test_account_daily_limit_applies_per_account(tmp_path):
    reserve = settings().attempt_reservation
    ledger = BudgetLedger(tmp_path / 'cost.sqlite')
    cfg = settings(daily_cost_limit=reserve * 10, account_daily_cost_limit=reserve * 1.5)

    def build(account):
        return OpenRouterClient(cfg, ledger, httpx.Client(transport=httpx.MockTransport(
            lambda _: response('ok'))), sleep=lambda _: None, account_id=account)
    budget_a = build('a')
    budget_a.complete([{'role': 'user', 'content': 'x'}], budget=budget_a.new_budget())
    with pytest.raises(LLMError) as error:
        budget_a.complete([{'role': 'user', 'content': 'x'}], budget=budget_a.new_budget())
    assert error.value.code == 'ACCOUNT_DAILY_BUDGET'
    other = build('b')
    assert other.complete([{'role': 'user', 'content': 'x'}], budget=other.new_budget())


def test_account_limit_env_defaults_to_daily(monkeypatch):
    env = {'OPENROUTER_API_KEY': 'k', 'OPENROUTER_MODEL': 'm', 'OPENROUTER_INPUT_USD_PER_MILLION': '1',
           'OPENROUTER_OUTPUT_USD_PER_MILLION': '1', 'MAX_COST_PER_REQUEST_USD': '0.01',
           'MAX_COST_PER_DAY_USD': '0.10'}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv('MAX_COST_PER_ACCOUNT_DAY_USD', raising=False)
    assert OpenRouterSettings.from_env().account_daily_cost_limit == 0.10
    monkeypatch.setenv('MAX_COST_PER_ACCOUNT_DAY_USD', '0.05')
    assert OpenRouterSettings.from_env().account_daily_cost_limit == 0.05
