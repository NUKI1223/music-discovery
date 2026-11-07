"""Real application callbacks exercised through an offline OpenRouter transport."""

import json

import httpx
import pytest

from discovery.agent_preview import agent_preview
from discovery.contracts import Artist, Catalog, Genre, Release, RequestIntent, UserProfile
from discovery.llm import BudgetLedger, LLMError, OpenRouterClient, OpenRouterSettings


@pytest.fixture
def catalog():
    return Catalog(
        snapshot_id='preview-test', is_fixture=True,
        genres=[Genre(id='rap', name='Rap'), Genre(id='abstract', name='Abstract', parent_ids=['rap']),
                Genre(id='rock', name='Rock')],
        artists=[Artist(id='a', name='A'), Artist(id='b', name='B'), Artist(id='c', name='C')],
        releases=[Release(id='r1', title='First', artist_ids=['a'], genre_ids=['abstract']),
                  Release(id='r2', title='Same artist', artist_ids=['a'], genre_ids=['abstract']),
                  Release(id='r3', title='Third', artist_ids=['b'], genre_ids=['abstract']),
                  Release(id='outside', title='Other genre', artist_ids=['c'], genre_ids=['rock'])],
    )


def final(ids):
    return {'role': 'assistant', 'content': json.dumps({'release_ids': ids})}


def call(name, args, ident):
    return {'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': ident, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}


def mock_client(tmp_path, messages):
    requests = []
    replies = iter(messages)

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': next(replies)}]})

    client = OpenRouterClient(OpenRouterSettings('offline-key', 'offline/model', 1, 2, 1, 10),
                              BudgetLedger(tmp_path / 'costs.sqlite'),
                              httpx.Client(transport=httpx.MockTransport(handler)))
    return client, requests


def test_preview_search_and_rank_are_frozen_readonly(tmp_path, catalog, monkeypatch):
    import discovery.experiments.bandit
    import discovery.service

    def forbidden(*_, **__):
        pytest.fail('Preview must not persist or sample a final action')

    monkeypatch.setattr(discovery.service, 'recommend', forbidden)
    monkeypatch.setattr(discovery.experiments.bandit, 'build_slate', forbidden)
    client, requests = mock_client(tmp_path, [
        call('search_catalog', {'genre_ids': ['rap'], 'limit': 10}, 'search'),
        call('rank_candidates', {'candidate_ids': ['r1', 'r3']}, 'rank'),
        call('get_release_evidence', {'release_ids': ['r1']}, 'evidence'),
        final(['r1', 'r3']),
    ])
    before = catalog.model_dump_json()
    profile = UserProfile(known_release_ids=['r2'])
    result = agent_preview(catalog, RequestIntent(genre_scope_id='rap'), profile, client, 'Новое')
    assert result['status'] == 'validated_preview' and result['committed'] is False
    assert result['release_ids'] == ['r1', 'r3']
    assert result['tool_calls'] == 3 and result['model_turns'] == 4
    assert catalog.model_dump_json() == before
    assert profile.known_release_ids == ['r2']
    search_observation = json.loads(requests[1]['messages'][-1]['content'])
    assert {row['release_id'] for row in search_observation['data']} == {'r1', 'r3'}
    assert 'outside' not in json.dumps(search_observation)
    assert 'offline-key' not in json.dumps(result)


@pytest.mark.parametrize('ids,count', [(['r1', 'r2'], 5), (['r1', 'r3'], 1)])
def test_independent_final_validator_rejects_artist_duplicates_and_wrong_count(tmp_path, catalog, ids, count):
    client, _ = mock_client(tmp_path, [final(ids)])
    with pytest.raises(LLMError) as err:
        agent_preview(catalog, RequestIntent(genre_scope_id='rap', count=count), UserProfile(), client, 'Новое')
    assert err.value.code == 'CONSTRAINT_VIOLATION'


def test_duplicate_rank_callback_is_stopped_before_reexecution(tmp_path, catalog, monkeypatch):
    import discovery.agent_preview as module

    original = module.score_candidates
    calls = []

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, 'score_candidates', counted)
    client, requests = mock_client(tmp_path, [
        call('rank_candidates', {'candidate_ids': ['r1', 'r3']}, 'rank-one'),
        call('rank_candidates', {'candidate_ids': ['r1', 'r3']}, 'rank-two'),
    ])
    with pytest.raises(LLMError) as err:
        agent_preview(catalog, RequestIntent(genre_scope_id='rap'), UserProfile(), client, 'Повтори')
    assert err.value.code == 'TOOL_LOOP'
    assert len(requests) == 2 and len(calls) == 1


@pytest.mark.parametrize('release_id,intent', [
    ('outside', RequestIntent(genre_scope_id='rap')),
    ('r1', RequestIntent(genre_scope_id='rap', obscurity='require')),
    ('r1', RequestIntent(genre_scope_id='rap', excluded_release_ids=['r1'])),
])
def test_preview_cannot_reintroduce_filtered_candidates(tmp_path, catalog, release_id, intent):
    client, _ = mock_client(tmp_path, [
        call('get_release_evidence', {'release_ids': [release_id]}, 'attempt'),
    ])
    with pytest.raises(LLMError) as err:
        agent_preview(catalog, intent, UserProfile(), client, 'Добавь этот релиз')
    assert err.value.code == 'UNKNOWN_RELEASE'
