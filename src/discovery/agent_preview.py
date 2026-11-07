"""Wire a bounded LLM agent to frozen local data, without any persistence or sampling."""

from dataclasses import asdict

from discovery.agent import BoundedAgent
from discovery.contracts import Catalog, RequestIntent, UserProfile
from discovery.llm import OpenRouterClient
from discovery.llm.evidence import available_claims
from discovery.ranking import score_candidates


def agent_preview(catalog: Catalog, request: RequestIntent, profile: UserProfile,
                  client: OpenRouterClient, text: str) -> dict:
    catalog, request, profile = (value.model_copy(deep=True) for value in (catalog, request, profile))
    scored = score_candidates(catalog, request, profile)
    score_map = {item.release_id: item for item in scored.items}
    release_map = {r.id: r for r in catalog.releases}
    genres = catalog.scope_ids(request.genre_scope_id)
    if request.locked_subgenre_ids:
        genres = set().union(*(catalog.descendant_ids(g) for g in request.locked_subgenre_ids))

    def search_catalog(genre_ids, limit):
        narrowed = set().union(*(catalog.descendant_ids(g) for g in genre_ids)) if genre_ids else genres
        return [{"release_id": item.release_id, "title": release_map[item.release_id].title,
                 "genre_ids": release_map[item.release_id].genre_ids}
                for item in scored.items if set(release_map[item.release_id].genre_ids) & narrowed
                or (request.genre_scope_id == "all" and not request.locked_subgenre_ids and not genre_ids)][:limit]

    def rank_candidates(candidate_ids):
        return [item.model_dump(mode="json") for item in scored.items if item.release_id in candidate_ids]

    def validate_recommendations(release_ids):
        if len(release_ids) > request.count or len(set(release_ids)) != len(release_ids):
            return {"valid": False}
        if not set(release_ids) <= score_map.keys():
            return {"valid": False}
        artists = []
        for rid in release_ids:
            artists.extend(release_map[rid].artist_ids)
        return {"valid": len(artists) == len(set(artists))}

    def get_release_evidence(release_ids):
        return [c.model_dump() for c in available_claims(catalog, release_ids)]

    agent = BoundedAgent(client, {
        "search_catalog": search_catalog, "rank_candidates": rank_candidates,
        "validate_recommendations": validate_recommendations,
        "get_release_evidence": get_release_evidence,
    }, allowed_release_ids=set(score_map), allowed_genre_ids=genres)
    result = agent.run(text + "\nSelected genre IDs: " + ", ".join(sorted(genres)))
    return {**asdict(result), "committed": False, "catalog_snapshot_id": catalog.snapshot_id,
            "profile_version": profile.version, "model_events": client.events}
