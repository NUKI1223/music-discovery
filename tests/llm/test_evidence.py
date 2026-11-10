import pytest

from discovery.contracts import Artist, Catalog, Genre, Release
from discovery.llm import LLMError, available_claims, explain_releases, render_claims


@pytest.fixture
def catalog():
    return Catalog(snapshot_id='frozen', genres=[Genre(id='hip-hop', name='Hip hop')],
                   artists=[Artist(id='a', name='A')],
                   releases=[Release(id='r', title='R', artist_ids=['a'], genre_ids=['hip-hop'], year=2020)])


def test_true_evidence_id_does_not_validate_wrong_claim(catalog):
    claim = next(c for c in available_claims(catalog, ['r']) if c.field == 'year')
    forged = claim.model_copy(update={'value': 1990})
    with pytest.raises(LLMError) as err:
        render_claims(catalog, ['r'], [forged])
    assert err.value.code == 'UNSUPPORTED_CLAIM'


def test_claim_must_belong_to_selected_release_and_snapshot(catalog):
    claim = available_claims(catalog, ['r'])[0]
    for altered in [{'release_id': 'other'}, {'evidence_id': 'different-snapshot'}]:
        with pytest.raises(LLMError):
            render_claims(catalog, ['r'], [claim.model_copy(update=altered)])


def test_template_does_not_invent_rarity_sound_or_familiarity(catalog):
    result = explain_releases(catalog, ['r'])
    assert result == {'r': ['Жанровая метка в каталоге: Hip hop.', 'Год релиза в каталоге: 2020.']}
    with pytest.raises(LLMError):
        explain_releases(catalog, ['missing'])


def _catalog_with_observation(source):
    from datetime import datetime, timezone

    from discovery.contracts import Observation

    observation = Observation(source=source, observed_at=datetime(2026, 11, 3, 15, tzinfo=timezone.utc),
                              rating_count=1234, access_basis='fixture')
    return Catalog(snapshot_id='frozen', genres=[Genre(id='hip-hop', name='Hip hop')],
                   artists=[Artist(id='a', name='A')],
                   releases=[Release(id='r', title='R', artist_ids=['a'], genre_ids=['hip-hop'],
                                     observations=[observation])])


def _count_claim(catalog):
    return next(c for c in available_claims(catalog, ['r']) if c.field == 'rating_count')


def test_listenbrainz_count_is_rendered_as_listeners_not_ratings():
    catalog = _catalog_with_observation('listenbrainz')
    sentence = render_claims(catalog, ['r'], [_count_claim(catalog)])['r'][0]
    assert sentence.startswith('Слушателей в ListenBrainz: 1234 на 2026-11-03')
    assert 'оценок' not in sentence


def test_explain_releases_uses_listenbrainz_wording():
    sentences = explain_releases(_catalog_with_observation('listenbrainz'), ['r'])['r']
    assert any(s.startswith('Слушателей в ListenBrainz: 1234 на 2026-11-03') for s in sentences)
    assert not any('оценок' in s for s in sentences)


def test_other_sources_keep_ratings_wording():
    catalog = _catalog_with_observation('musicbrainz')
    sentence = render_claims(catalog, ['r'], [_count_claim(catalog)])['r'][0]
    assert sentence == 'musicbrainz: 1234 оценок на 2026-11-03; это не число всех слушателей.'
    assert 'ListenBrainz' not in sentence
