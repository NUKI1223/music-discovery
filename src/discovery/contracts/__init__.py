"""Shared validated contracts. IDs, rather than names, cross module boundaries."""

from datetime import UTC, date, datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

ALL_GENRES = "all"


def scope_values(mapping: dict[str, list[str]], scope: str) -> list[str]:
    """Free scope shares explicit taste across genres; global taste also applies locally."""
    groups = mapping.values() if scope == ALL_GENRES else [mapping.get(scope, []), mapping.get(ALL_GENRES, [])]
    return sorted({value for values in groups for value in values})


def utcnow() -> datetime:
    return datetime.now(UTC)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, allow_inf_nan=False)


class Genre(Contract):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    parent_ids: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)


class Artist(Contract):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    genre_ids: list[str] = Field(default_factory=list)


class Observation(Contract):
    source: str = Field(min_length=1)
    url: str = ""
    observed_at: datetime
    rating: float | None = None
    rating_scale: float | None = Field(default=None, gt=0)
    rating_count: int | None = Field(default=None, ge=0)
    access_basis: str = Field(min_length=1)

    @model_validator(mode="after")
    def valid_rating(self) -> Self:
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must include a timezone")
        if self.rating is not None and (
            self.rating_scale is None or not 0 <= self.rating <= self.rating_scale
        ):
            raise ValueError("rating requires its original scale and must lie within it")
        return self


class HistoricalObservation(Contract):
    """Published aggregates with unknown capture time; never freshness evidence."""

    source: Literal["rym", "aoty"]
    source_row_id: str = Field(min_length=1)
    provenance_id: str = Field(min_length=1)
    url: str | None = None
    observed_at: None = None
    retrieved_at: datetime | None = None
    rating: float | None = None
    rating_scale: float
    rating_count: int | None = Field(default=None, ge=0)
    rating_count_text: str | None = None
    count_semantics_verified: Literal[False] = False
    evidence_kind: Literal["publisher_reported_historical"] = "publisher_reported_historical"

    @model_validator(mode="after")
    def valid_historical(self) -> Self:
        if self.rating_scale != (5 if self.source == "rym" else 100):
            raise ValueError("Historical rating must retain its source scale")
        if self.rating is not None and not 0 <= self.rating <= self.rating_scale:
            raise ValueError("Historical rating is outside its source scale")
        if self.retrieved_at is not None and self.retrieved_at.utcoffset() is None:
            raise ValueError("Retrieval timestamp must have a timezone")
        if self.rating_count == 0 and self.rating is not None:
            raise ValueError("A zero-count observation cannot have a mean rating")
        return self


class SourceMetadata(Contract):
    source: Literal["rym", "aoty"]
    source_row_id: str
    title: str
    artist_credit: str
    release_date_text: str | None = None
    primary_labels: list[str] = Field(default_factory=list)
    secondary_labels: list[str] = Field(default_factory=list)
    primary_ids: list[str] = Field(default_factory=list)
    secondary_ids: list[str] = Field(default_factory=list)
    descriptors: list[str] = Field(default_factory=list)
    # AOTY's Vibe vocabulary is source annotation, not a genre taxonomy.
    vibes: list[str] = Field(default_factory=list)
    unmapped: list[dict] = Field(default_factory=list)
    mapping_evidence: list[dict] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def serialize_compatible(self, handler):
        payload = handler(self)
        if not payload.get("vibes"):
            payload.pop("vibes", None)
        return payload


class Release(Contract):
    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    artist_ids: list[str] = Field(min_length=1)
    genre_ids: list[str] = Field(default_factory=list)
    year: int | None = Field(default=None, ge=1800, le=2200)
    release_type: str = "album"
    observations: list[Observation] = Field(default_factory=list)
    links: dict[str, str] = Field(default_factory=dict)
    historical_observations: list[HistoricalObservation] = Field(default_factory=list)
    source_metadata: list[SourceMetadata] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def serialize_compatible(self, handler):
        payload = handler(self)
        # Empty new fields must not change frozen legacy catalogue hashes.
        for key in ("historical_observations", "source_metadata"):
            if not payload.get(key):
                payload.pop(key, None)
        return payload


class Catalog(Contract):
    snapshot_id: str = Field(min_length=1)
    taxonomy_version: str = "1"
    is_fixture: bool = False
    genres: list[Genre] = Field(default_factory=list)
    artists: list[Artist] = Field(default_factory=list)
    releases: list[Release] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_references(self) -> Self:
        for label, rows in (("genres", self.genres), ("artists", self.artists),
                            ("releases", self.releases)):
            ids = [row.id for row in rows]
            if len(ids) != len(set(ids)):
                raise ValueError(f"duplicate IDs in {label}")
        genres = {g.id: g for g in self.genres}
        artists = {a.id for a in self.artists}
        visited: set[str] = set()
        visiting: set[str] = set()

        def visit(gid: str) -> None:
            if gid not in genres:
                raise ValueError(f"unknown genre reference: {gid}")
            if gid in visiting:
                raise ValueError("taxonomy has a cycle")
            if gid in visited:
                return
            visiting.add(gid)
            for parent in genres[gid].parent_ids:
                visit(parent)
            visiting.remove(gid)
            visited.add(gid)

        for gid in genres:
            visit(gid)
        for row in [*self.artists, *self.releases]:
            if not set(row.genre_ids) <= genres.keys():
                raise ValueError(f"unknown genre on {row.id}")
        for release in self.releases:
            if not set(release.artist_ids) <= artists:
                raise ValueError(f"unknown artist on {release.id}")
            for metadata in release.source_metadata:
                if not set(metadata.primary_ids + metadata.secondary_ids) <= genres.keys():
                    raise ValueError(f"Unknown mapped source genre on {release.id}")
        return self

    def scope_ids(self, scope: str) -> set[str]:
        """Request scope, including a virtual all-genres mode; no taxonomy mutation."""
        return {g.id for g in self.genres} if scope == ALL_GENRES else self.descendant_ids(scope)

    def descendant_ids(self, genre_id: str) -> set[str]:
        if genre_id not in {g.id for g in self.genres}:
            raise ValueError(f"unknown genre scope: {genre_id}")
        found = {genre_id}
        while True:
            expanded = found | {g.id for g in self.genres if set(g.parent_ids) & found}
            if expanded == found:
                return found
            found = expanded


class RequestIntent(Contract):
    genre_scope_id: str = Field(min_length=1)
    mode: Literal["close", "explore", "surprise"] = "explore"
    obscurity: Literal["prefer", "require"] = "prefer"
    count: int = Field(default=5, ge=1, le=5)
    locked_subgenre_ids: list[str] = Field(default_factory=list)
    excluded_release_ids: list[str] = Field(default_factory=list)
    excluded_artist_ids: list[str] = Field(default_factory=list)
    reference_artist_ids: list[str] = Field(default_factory=list, max_length=20)
    requested_artist_ids: list[str] = Field(default_factory=list, max_length=20)
    preferred_descriptors: list[str] = Field(default_factory=list, max_length=8)
    avoided_descriptors: list[str] = Field(default_factory=list, max_length=8)

    def validate_catalog(self, catalog: Catalog) -> None:
        allowed = catalog.scope_ids(self.genre_scope_id)
        if not set(self.locked_subgenre_ids) <= allowed:
            raise ValueError("locked subgenres must belong to the selected scope")
        artists = {a.id for a in catalog.artists}
        if not set(self.reference_artist_ids) <= artists:
            raise ValueError("Unknown artist reference")
        if not set(self.requested_artist_ids) <= artists:
            raise ValueError("Unknown explicitly requested artist")
        if set(self.requested_artist_ids) & set(self.excluded_artist_ids):
            raise ValueError("Requested artist is explicitly excluded")
        descriptors = {d.strip().casefold() for r in catalog.releases if self.genre_scope_id == ALL_GENRES or set(r.genre_ids) & allowed
                       for m in r.source_metadata for d in [*m.descriptors, *m.vibes] if d.strip()}
        if not set(self.preferred_descriptors + self.avoided_descriptors) <= descriptors:
            raise ValueError("Unknown source descriptor in this genre")
        if set(self.preferred_descriptors) & set(self.avoided_descriptors):
            raise ValueError("Conflicting sound preferences")

    @model_serializer(mode="wrap")
    def serialize_compatible(self, handler):
        payload = handler(self)
        for key in ("reference_artist_ids", "requested_artist_ids", "preferred_descriptors", "avoided_descriptors"):
            if not payload.get(key):
                payload.pop(key, None)
        return payload


class ArtistPopularityLabel(Contract):
    classification: Literal["widely_known", "less_known"]
    source: Literal["explicit_user"] = "explicit_user"
    evidence: str = Field(min_length=1, max_length=500)
    labelled_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def aware_evidence_date(self) -> Self:
        if self.labelled_at.utcoffset() is None:
            raise ValueError("Popularity evidence date must include a timezone")
        return self


class UserProfile(Contract):
    version: str = "empty"
    known_release_ids: list[str] = Field(default_factory=list)
    liked_release_ids_by_genre: dict[str, list[str]] = Field(default_factory=dict)
    disliked_release_ids_by_genre: dict[str, list[str]] = Field(default_factory=dict)
    seed_artist_ids_by_genre: dict[str, list[str]] = Field(default_factory=dict)
    avoid_tags_by_genre: dict[str, list[str]] = Field(default_factory=dict)
    request_only_artist_ids: list[str] = Field(default_factory=list, max_length=50)
    limit_widely_known_artists: bool = False
    max_widely_known_artists: int = Field(default=2, ge=0, le=2)
    artist_popularity_labels: dict[str, ArtistPopularityLabel] = Field(default_factory=dict, max_length=500)

    @model_serializer(mode="wrap")
    def serialize_compatible(self, handler):
        payload = handler(self)
        for key in ("request_only_artist_ids", "limit_widely_known_artists", "artist_popularity_labels"):
            if not payload.get(key):
                payload.pop(key, None)
        if payload.get("max_widely_known_artists") == 2:
            payload.pop("max_widely_known_artists", None)
        return payload


class RecommendationItem(Contract):
    release_id: str
    score: float
    components: dict[str, float | str | None] = Field(default_factory=dict)
    reason_codes: list[str] = Field(default_factory=list)
    uncertainty_flags: list[str] = Field(default_factory=list)


class RankingResult(Contract):
    items: list[RecommendationItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    profile_basis: Literal["artist_prior", "rated_releases", "unavailable"] = "unavailable"
    candidate_ids: list[str] = Field(default_factory=list)


class Decision(Contract):
    id: str
    request: RequestIntent
    created_at: datetime = Field(default_factory=utcnow)
    as_of: date = Field(default_factory=lambda: utcnow().date())
    catalog_snapshot_id: str
    catalog_hash: str
    taxonomy_version: str
    profile: UserProfile
    result: RankingResult
    ranker_version: str = "baseline-v1"
    policy_version: str = "deterministic-v1"
    feature_version: str = "genre-tags-v1"
    policy_details: dict = Field(default_factory=dict)


class Feedback(Contract):
    id: str
    decision_id: str
    release_id: str
    kind: Literal["known", "save_for_later", "listened_like", "listened_mixed",
                  "listened_dislike", "wrong_genre", "not_now", "familiarity"]
    familiar_before_recommendation: bool | None = None
    score: int | None = Field(default=None, ge=0, le=100)
    review: str | None = Field(default=None, max_length=4000)
    occurred_at: datetime = Field(default_factory=utcnow)
    recorded_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def aware_dates(self) -> Self:
        if self.occurred_at.tzinfo is None or self.recorded_at.tzinfo is None:
            raise ValueError("feedback timestamps must include a timezone")
        if ((self.score is not None or self.review is not None)
                and self.kind not in {"listened_like", "listened_mixed", "listened_dislike"}):
            raise ValueError("score and review are allowed only for listened feedback")
        if self.kind == "known" and self.familiar_before_recommendation is False:
            raise ValueError("known release cannot be unfamiliar before this recommendation")
        return self
