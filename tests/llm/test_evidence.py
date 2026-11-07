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
