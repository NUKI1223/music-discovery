"""Claims are validated against a caller-owned snapshot; prose is templated."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from discovery.contracts import Catalog

from .errors import LLMError


class Claim(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    release_id: str
    field: Literal['genre_id', 'year', 'rating_count']
    operator: Literal['eq']
    value: str | int
    evidence_id: str


class Claims(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    claims: list[Claim] = Field(max_length=20)


def available_claims(catalog: Catalog, release_ids: list[str]) -> list[Claim]:
    wanted = set(release_ids)
    records = {release.id: release for release in catalog.releases}
    if not wanted <= records.keys():
        raise LLMError('UNKNOWN_RELEASE', 'Релиз отсутствует в выбранном снимке каталога.')
    claims = []
    for release_id in dict.fromkeys(release_ids):
        release = records[release_id]
        prefix = f'{catalog.snapshot_id}:{release.id}'
        for genre in release.genre_ids:
            claims.append(Claim(release_id=release_id, field='genre_id', operator='eq', value=genre,
                                evidence_id=f'{prefix}:genre:{genre}'))
        if release.year is not None:
            claims.append(Claim(release_id=release_id, field='year', operator='eq', value=release.year,
                                evidence_id=f'{prefix}:year'))
        for index, observation in enumerate(release.observations):
            if observation.rating_count is not None:
                claims.append(Claim(release_id=release_id, field='rating_count', operator='eq',
                                    value=observation.rating_count,
                                    evidence_id=f'{prefix}:observation:{index}:rating_count'))
    return claims


def render_claims(catalog: Catalog, release_ids: list[str], claims: list[Claim]) -> dict[str, list[str]]:
    facts = {claim.evidence_id: claim for claim in available_claims(catalog, release_ids)}
    genres = {genre.id: genre.name for genre in catalog.genres}
    releases = {release.id: release for release in catalog.releases}
    rendered = {rid: [] for rid in release_ids}
    for claim in claims:
        # Equality checks all fields and types, not merely whether the evidence ID exists.
        fact = facts.get(claim.evidence_id)
        if fact is None or fact.model_dump() != claim.model_dump() or type(fact.value) is not type(claim.value):
            raise LLMError('UNSUPPORTED_CLAIM', 'Объяснение не подтверждено выбранным снимком каталога.')
        if claim.field == 'genre_id':
            sentence = f'Жанровая метка в каталоге: {genres[str(claim.value)]}.'
        elif claim.field == 'year':
            sentence = f'Год релиза в каталоге: {claim.value}.'
        else:
            index = int(claim.evidence_id.rsplit(':', 2)[1])
            observation = releases[claim.release_id].observations[index]
            observed = observation.observed_at.date().isoformat()
            if observation.source == 'listenbrainz':
                sentence = (f'Слушателей в ListenBrainz: {claim.value} на {observed}; '
                            'это пользователи одного сервиса, а не вся аудитория.')
            else:
                sentence = (f'{observation.source}: {claim.value} оценок '
                            f'на {observed}; это не число всех слушателей.')
        if sentence not in rendered[claim.release_id]:
            rendered[claim.release_id].append(sentence)
    return rendered


def explain_releases(catalog: Catalog, release_ids: list[str]) -> dict[str, list[str]]:
    """Safe zero-cost fallback. Presence in a catalog does not imply unfamiliarity."""
    claims = available_claims(catalog, release_ids)
    return render_claims(catalog, release_ids, claims)
