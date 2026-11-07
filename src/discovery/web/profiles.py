"""Explicit per-account taste onboarding and local catalog lookups."""

import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from discovery.config import PROJECT_ROOT
from discovery.contracts import ALL_GENRES, UserProfile
from discovery.storage import Store

from .deps import get_store, get_user
from .schemas import Filters, Lookup, Profile

router = APIRouter(prefix="/api", tags=["profile"])


def validate_filters(filters: Filters, catalog) -> None:
    try:
        filters.validate_catalog(catalog)
    except ValueError:
        raise HTTPException(422, "Выберите существующий жанр и его поджанры.") from None


def read_profile(store: Store) -> Profile:
    with store.connection() as conn:
        row = conn.execute("SELECT payload FROM settings WHERE key='web_profile'").fetchone()
        core = conn.execute("SELECT payload FROM settings WHERE key='profile'").fetchone()
    result = Profile.model_validate_json(row[0]) if row else Profile()
    base = UserProfile.model_validate_json(core[0]) if core else UserProfile()
    for field in ("request_only_artist_ids", "limit_widely_known_artists", "max_widely_known_artists", "artist_popularity_labels"):
        setattr(result, field, getattr(base, field))
    return result


def write_profile(store: Store, profile: Profile, catalog) -> Profile:
    validate_filters(profile.filters, catalog)
    if not set(profile.seed_artist_ids) <= {a.id for a in catalog.artists}:
        raise HTTPException(422, "Выберите исполнителей из каталога.")
    if not set(profile.request_only_artist_ids) <= {a.id for a in catalog.artists}:
        raise HTTPException(422, "Выберите исполнителей правила из каталога.")
    if not set(profile.artist_popularity_labels) <= {a.id for a in catalog.artists}:
        raise HTTPException(422, "Метки известности должны относиться к исполнителям каталога.")
    if not set(profile.avoid_tag_ids) <= {g.id for g in catalog.genres}:
        raise HTTPException(422, "Неизвестный исключаемый жанр.")
    with store.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT payload FROM settings WHERE key='profile'").fetchone()
        base = UserProfile.model_validate_json(row[0]) if row else UserProfile()
        if "request_only_artist_ids" in profile.model_fields_set:
            base.request_only_artist_ids = list(dict.fromkeys(profile.request_only_artist_ids))
        else:
            profile.request_only_artist_ids = list(base.request_only_artist_ids)
        for field in ("limit_widely_known_artists", "max_widely_known_artists", "artist_popularity_labels"):
            if field in profile.model_fields_set:
                setattr(base, field, getattr(profile, field))
            else:
                setattr(profile, field, getattr(base, field))
        scope = profile.filters.genre_scope_id
        base.seed_artist_ids_by_genre[scope] = list(dict.fromkeys(profile.seed_artist_ids))
        base.avoid_tags_by_genre[scope] = list(dict.fromkeys(profile.avoid_tag_ids))
        for key, payload in (("profile", base.model_dump_json()), ("web_profile", profile.model_dump_json())):
            conn.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload", (key, payload))
    return profile


@router.get("/profile", response_model=Profile)
def profile(request: Request, genre_scope_id: str | None = Query(default=None, max_length=100),
            store: Store = Depends(get_store)):
    result = read_profile(store)
    if genre_scope_id is not None:
        if genre_scope_id != ALL_GENRES and genre_scope_id not in {g.id for g in request.app.state.catalog.genres}:
            raise HTTPException(422, "Неизвестный жанр.")
        base = store.profile()
        result.filters.genre_scope_id = genre_scope_id
        result.filters.locked_subgenre_ids = []
        result.filters.reference_artist_ids = []
        result.filters.requested_artist_ids = []
        result.filters.excluded_artist_ids = []
        result.filters.preferred_descriptors = []
        result.filters.avoided_descriptors = []
        result.seed_artist_ids = base.seed_artist_ids_by_genre.get(genre_scope_id, [])
        result.avoid_tag_ids = base.avoid_tags_by_genre.get(genre_scope_id, [])
    return result


@router.put("/profile", response_model=Profile)
def update_profile(data: Profile, request: Request, store: Store = Depends(get_store)):
    return write_profile(store, data, request.app.state.catalog)


@router.post("/profile/starter", response_model=Profile)
def starter(request: Request, store: Store = Depends(get_store), preview: bool = False):
    catalog = request.app.state.catalog
    settings = json.loads((PROJECT_ROOT / "configs/pilot.json").read_text())
    config = settings["genres"]["hip-hop"]
    names = {n.casefold() for n in config["seed_artist_names"]}
    ids = set(config["seed_artist_ids"])
    profile = read_profile(store)
    profile.onboarded = True
    profile.filters = Filters(genre_scope_id="hip-hop")
    profile.seed_artist_ids = [a.id for a in catalog.artists if a.id in ids or a.name.casefold() in names]
    profile.avoid_tag_ids = [g for g in config["avoid_tag_ids"] if g in {i.id for i in catalog.genres}]
    return profile if preview else write_profile(store, profile, catalog)


@router.get("/catalog/genres", response_model=list[Lookup], dependencies=[Depends(get_user)])
def genres(request: Request, q: str = Query(default="", max_length=100),
           genre_scope_id: str | None = None):
    catalog = request.app.state.catalog
    try:
        scope = catalog.scope_ids(genre_scope_id) if genre_scope_id else None
    except ValueError:
        raise HTTPException(422, "Неизвестный жанр.") from None
    free = [Lookup(id=ALL_GENRES, name="Все жанры")] if genre_scope_id is None and (
        not q or q.casefold() in "все жанры свободный all genres") else []
    return free + [Lookup(id=g.id, name=g.name) for g in catalog.genres
            if (scope is None or g.id in scope)
            and (q.casefold() in g.name.casefold() or q.casefold() in g.id.casefold())]


@router.get("/catalog/artists", response_model=list[Lookup], dependencies=[Depends(get_user)])
def artists(request: Request, q: str = Query(default="", max_length=100),
            ids: str | None = Query(default=None, max_length=6000)):
    selected = set(ids.split(",")) if ids else None
    if selected is not None and len(selected) > 50:
        raise HTTPException(422, "Можно выбрать до 50 исполнителей.")
    return [Lookup(id=a.id, name=a.name) for a in request.app.state.catalog.artists
            if (selected is None or a.id in selected) and q.casefold() in a.name.casefold()][:50]
