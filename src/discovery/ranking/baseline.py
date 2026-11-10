"""Transparent baseline. Unknown popularity never becomes evidence of rarity."""
from bisect import bisect_right
from collections import Counter
from datetime import date
from math import ceil, fsum

from discovery.contracts import (
    ALL_GENRES,
    Catalog,
    RankingResult,
    RecommendationItem,
    RequestIntent,
    UserProfile,
    scope_values,
)

from .source_descriptors import SOUND_POLICY_VERSION, descriptor_weights

MIN_COHORT = 30
FEATURE_VERSION = "genre-tags-v2-artist-frequency"
RANKER_VERSION = "baseline-v7-artist-peak-popularity"
# A promoted model only nudges the baseline: score += MODEL_WEIGHT * (p - 0.5).
MODEL_WEIGHT = .3
MAX_OBSERVATION_AGE_DAYS = 90
# Compilation placeholders are not real artists: they never share a popularity peak.
COLLECTIVE_ARTIST_NAMES = frozenset({"various artists", "[unknown artist]", "[unknown]", "[anonymous]",
                                     "[traditional]", "[no artist]"})
MODE_NOVELTY_TARGET = {"close": .35, "explore": .65, "surprise": .9}
EXPLORATION_NOVELTY_BOUNDS = {"close": (.5, .8), "explore": (.5, 1.0), "surprise": (.5, 1.0)}


def _jaccard(left: set[str], right: set[str]) -> float:
    return len(left & right) / len(left | right) if left or right else 0.0


def _weighted_jaccard(tags: set[str], anchor: dict[str, float]) -> float:
    intersection = fsum(anchor[tag] for tag in sorted(tags & anchor.keys()))
    union = len(tags) + fsum(anchor[tag] for tag in sorted(anchor)) - intersection
    return intersection / union if union else 0.0


def _age(year: int | None, as_of: date) -> str | None:
    if year is None or year > as_of.year:
        return None
    years = as_of.year-year
    return "under_1" if years == 0 else "1_to_5" if years <= 5 else "over_5"


def _tags(catalog: Catalog, scope: str) -> tuple[set[str], dict[str, set[str]]]:
    allowed = catalog.scope_ids(scope)
    # Remove ancestor tags when a more specific assertion already carries them.
    children = {g: catalog.descendant_ids(g)-{g} for g in allowed}
    tags = {}
    for r in catalog.releases:
        selected = set(r.genre_ids) & allowed
        tags[r.id] = {g for g in selected if not children[g] & selected}
    return allowed, tags


def _anchors(catalog: Catalog, profile: UserProfile, scope: str,
             tags: dict[str, set[str]], *, feature_version: str = FEATURE_VERSION
             ) -> tuple[list[dict[str, float]], str]:
    liked = [tags[r] for r in scope_values(profile.liked_release_ids_by_genre, scope) if tags.get(r)]
    if liked:
        return [dict.fromkeys(values, 1.0) for values in liked], "rated_releases"
    artist_map = {a.id: a for a in catalog.artists}
    allowed = catalog.scope_ids(scope)
    anchors = []
    for artist_id in scope_values(profile.seed_artist_ids_by_genre, scope):
        artist = artist_map.get(artist_id)
        if artist is None:
            continue
        releases = [tags[r.id] for r in catalog.releases if artist_id in r.artist_ids]
        if feature_version == "genre-tags-v1":
            # Historical decisions/models can still explicitly replay old features.
            artist_tags = set().union(*releases) | (set(artist.genre_ids) & allowed)
            if len(artist_tags) > 1:
                artist_tags.discard(scope)
            if artist_tags:
                anchors.append(dict.fromkeys(artist_tags, 1.0))
            continue
        # A scope-only release has no subgenre information; it is not negative
        # evidence for every specific style. Count each canonical release once,
        # independently of how many source rows describe it.
        informative = [values - {scope} for values in releases if values - {scope}]
        if informative:
            counts = Counter(tag for values in informative for tag in values)
            anchors.append({tag: count / len(informative) for tag, count in sorted(counts.items())})
        else:
            artist_tags = (set(artist.genre_ids) & allowed) - {scope}
            specific = {tag for tag in artist_tags
                        if not (catalog.descendant_ids(tag) - {tag}) & artist_tags}
            if specific:
                anchors.append(dict.fromkeys(specific, 1.0))
    return anchors, "artist_prior" if anchors else "unavailable"


def _fresh_count(observation, as_of: date) -> int | None:
    """Count of a latest observation if it is usable (not future, not stale, has a count)."""
    if (observation is not None and observation.rating_count is not None
            and 0 <= (as_of-observation.observed_at.date()).days <= MAX_OBSERVATION_AGE_DAYS):
        return observation.rating_count
    return None


def _latest_by_source(release, as_of: date) -> dict:
    latest = {}
    for o in sorted(release.observations, key=lambda o: o.observed_at):
        if o.observed_at.date() <= as_of:
            latest[o.source] = o
    return latest


def _popularity(catalog: Catalog, scope: str, as_of: date) -> dict[str, dict]:
    """Deterministic source/cohort choice, shared independently of user exclusions.

    Cohort membership is genre group + age group + own fresh count of the chosen source. Counts are compared as
    effective counts: max(own count, peak fresh count of the same source over all catalog releases of any of the
    release's artists), so a rarely-attributed release of a well-known artist does not look rare. `rating_count`
    stays the release's own count; `artist_peak_count` is the effective one (None without a fresh own count).
    The source is the alphabetically first one with a fresh count (any latest source if none is fresh). Collective
    "artists" (Various Artists, [unknown artist], ...) get no peak, so their effective count is the own count.
    An own count of 0 means unknown, so it is never rarity-eligible.
    """
    allowed = catalog.scope_ids(scope)
    scoped = [r for r in catalog.releases if scope == ALL_GENRES or set(r.genre_ids) & allowed]
    observations = {r.id: _latest_by_source(r, as_of) for r in scoped}
    peak: dict[str, dict[str, int]] = {}  # source -> artist id -> max fresh count over the whole catalog
    scoped_ids = set(observations)
    collective = {a.id for a in catalog.artists if a.name.strip().casefold() in COLLECTIVE_ARTIST_NAMES}
    for r in catalog.releases:
        if not r.observations:
            continue
        for source, o in (observations[r.id] if r.id in scoped_ids else _latest_by_source(r, as_of)).items():
            count = _fresh_count(o, as_of)
            if count is not None:
                by_artist = peak.setdefault(source, {})
                for artist_id in r.artist_ids:
                    if artist_id not in collective and count > by_artist.get(artist_id, -1):
                        by_artist[artist_id] = count
    parents = {g.id: g.parent_ids for g in catalog.genres}
    descendants: dict[str, frozenset[str] | set[str]] = {}

    def desc(group: str):
        if group not in descendants:
            descendants[group] = catalog.descendant_ids(group)
        return descendants[group]

    ages = [_age(r.year, as_of) for r in scoped]
    effective: dict[str, list] = {}  # source -> per scoped position effective count (None: not a member)
    genre_index: dict[tuple[str, str], dict[str, list[int]]] = {}  # (source, age) -> genre -> positions
    cohorts: dict[tuple[str, str, str], tuple[list[int], list[int]]] = {}  # key -> (positions, sorted counts)
    priors: dict[tuple[str, str, str], float | None] = {}

    def source_index(source: str) -> list:
        if source not in effective:
            by_artist = peak.get(source, {})
            counts = []
            for pos, r in enumerate(scoped):
                own = _fresh_count(observations[r.id].get(source), as_of)
                if own is None:
                    counts.append(None)
                    continue
                counts.append(max([own, *(by_artist.get(a, own) for a in r.artist_ids if a not in collective)]))
                if ages[pos] is not None:
                    index = genre_index.setdefault((source, ages[pos]), {})
                    for g in set(r.genre_ids):
                        index.setdefault(g, []).append(pos)
            effective[source] = counts
        return effective[source]

    def cohort(source: str, group: str, age: str):
        key = (source, group, age)
        if key not in cohorts:
            counts = source_index(source)
            index = genre_index.get((source, age), {})
            positions = sorted({pos for g in desc(group) for pos in index.get(g, ())})
            cohorts[key] = (positions, sorted(counts[pos] for pos in positions))
        return cohorts[key]

    output = {}
    for position, r in enumerate(scoped):
        own = observations[r.id]
        # Stable source preference; never choose the source yielding the lowest percentile.
        fresh = [s for s in own if _fresh_count(own[s], as_of) is not None]
        source = min(fresh) if fresh else min(own) if own else None
        observation = own.get(source)
        value = {"rarity": None, "quality": None, "rating_count": None, "artist_peak_count": None,
                 "popularity_source": source, "cohort_id": None, "cohort_size": 0,
                 "rarity_eligible": 0.0}
        age = ages[position]
        if observation is None:
            output[r.id] = value
            continue
        if (as_of-observation.observed_at.date()).days > MAX_OBSERVATION_AGE_DAYS:
            output[r.id] = value
            continue
        value["rating_count"] = observation.rating_count
        if observation.rating_count is None:
            output[r.id] = value
            continue
        own_effective = source_index(source)[position]
        value["artist_peak_count"] = own_effective
        if age is None:
            output[r.id] = value
            continue
        # Most specific direct tag first, then ancestors, finally requested scope.
        groups = set(r.genre_ids) & allowed
        pending = list(groups)
        while pending:
            child = pending.pop()
            for parent in parents[child]:
                if parent in allowed and parent not in groups:
                    groups.add(parent)
                    pending.append(parent)
        if scope != ALL_GENRES:
            groups.add(scope)
        group_order = sorted(groups, key=lambda g: (len(desc(g)), g))
        for group in group_order:
            positions, counts = cohort(source, group, age)
            if len(positions) < MIN_COHORT:
                continue
            # Conservative tie handling: an equal-count cohort is not all "rare".
            percentile = bisect_right(counts, own_effective)/len(counts)
            eligible = percentile <= .4 and observation.rating_count > 0  # 0 means unknown
            value.update(rarity=1-percentile, rarity_eligible=float(eligible),
                         cohort_id=f"{source}:{group}:{age}", cohort_size=len(positions))
            if observation.rating is not None:
                key = (source, group, age)
                if key not in priors:
                    rated = [o.rating/o.rating_scale for pos in positions
                             if (o := observations[scoped[pos].id][source]).rating is not None]
                    priors[key] = sum(rated)/len(rated) if rated else None
                if priors[key] is not None:
                    n = observation.rating_count
                    value["quality"] = (n*(observation.rating/observation.rating_scale)+20*priors[key])/(n+20)
            break
        output[r.id] = value
    return output


def score_candidates(catalog: Catalog, request: RequestIntent, profile: UserProfile,
                     as_of: date | None = None, *, artifact: dict | None = None,
                     feature_version: str = FEATURE_VERSION,
                     _experimental: bool = False) -> RankingResult:
    """Score every eligible candidate before slate selection; useful for frozen logs."""
    request.validate_catalog(catalog)
    if feature_version not in {"genre-tags-v1", FEATURE_VERSION}:
        raise ValueError("Unsupported ranking feature version")
    today = as_of or date.today()
    allowed, tags = _tags(catalog, request.genre_scope_id)
    if request.locked_subgenre_ids:
        allowed = set().union(*(catalog.descendant_ids(g) for g in request.locked_subgenre_ids))
    anchor_profile = (UserProfile(seed_artist_ids_by_genre={request.genre_scope_id: request.reference_artist_ids})
                      if request.reference_artist_ids else profile)
    anchors, basis = _anchors(catalog, anchor_profile, request.genre_scope_id, tags,
                             feature_version=feature_version)
    if request.reference_artist_ids and anchors:
        basis = "artist_prior"
    negative = [tags[r] for r in scope_values(profile.disliked_release_ids_by_genre, request.genre_scope_id) if tags.get(r)]
    avoid_ids = scope_values(profile.avoid_tags_by_genre, request.genre_scope_id)
    avoid = set().union(*(catalog.descendant_ids(g) for g in avoid_ids)) if avoid_ids else set()
    popularity = _popularity(catalog, request.genre_scope_id, today)
    excluded = set(profile.known_release_ids) | set(request.excluded_release_ids)
    excluded |= set(scope_values(profile.liked_release_ids_by_genre, request.genre_scope_id))
    excluded |= set(scope_values(profile.disliked_release_ids_by_genre, request.genre_scope_id))
    requested_artists = set(request.requested_artist_ids)
    request_only = set(profile.request_only_artist_ids) - requested_artists
    items = []
    for release in catalog.releases:
        if (release.id in excluded or set(release.artist_ids) & set(request.excluded_artist_ids)
            or set(release.artist_ids) & request_only
            or (profile.limit_widely_known_artists and profile.max_widely_known_artists == 0
                and counts_toward_popularity_limit(release, request, profile))
            or (requested_artists and not set(release.artist_ids) & requested_artists)
            or (not set(release.genre_ids) & allowed
                and (request.genre_scope_id != ALL_GENRES or request.locked_subgenre_ids))):
            continue
        pop = popularity[release.id]
        if request.obscurity == "require" and not pop["rarity_eligible"]:
            continue
        similarity = max((_weighted_jaccard(tags[release.id], a) for a in anchors), default=0.0)
        novelty = 1-similarity if anchors else None
        # Only scope-level (or no) tags carry no subgenre information, so similarity 0
        # is "unknown", not "far". Historical feature replays keep the old behaviour.
        uninformative = (feature_version == FEATURE_VERSION and bool(anchors)
                         and not tags[release.id] - {request.genre_scope_id})
        if uninformative:
            novelty = None
        dislike = max((_jaccard(tags[release.id], a) for a in negative), default=0.0)
        avoidance = float(bool(set(release.genre_ids) & avoid))
        novelty_fit = (1-abs(novelty-MODE_NOVELTY_TARGET[request.mode]) if novelty is not None
                       else .5 if uninformative else 0)
        quality = pop["quality"] if pop["quality"] is not None else .5
        rarity = pop["rarity"] if pop["rarity"] is not None else 0
        # Similarity must not outweigh the distance explicitly requested by surprise.
        similarity_weight, distance_weight = (.15, .55) if request.mode == "surprise" else (.45, .25)
        score = similarity_weight*similarity + distance_weight*novelty_fit + .15*rarity + .15*quality - .3*avoidance - .2*dislike
        components = dict(pop, similarity=similarity, novelty=novelty, avoidance=avoidance,
                          negative_affinity=dislike, novelty_fit=novelty_fit,
                          genre_scope=request.genre_scope_id, mode=request.mode,
                          profile_confidence=.3 if basis == "artist_prior" else 1.0 if anchors else 0.0)
        reasons = ["all_genres_selection" if request.genre_scope_id == ALL_GENRES else "genre_membership"]
        if similarity > 0:
            reasons.append("tag_affinity")
        if pop["rarity_eligible"]:
            reasons.append("low_source_vote_percentile")
        flags = []
        if request.genre_scope_id == ALL_GENRES and not release.genre_ids:
            score -= .05
            components["missing_genre_penalty"] = .05
            flags.append("genre_unknown")
        if uninformative:
            flags.append("genre_tags_not_specific")
        if pop["rarity"] is None:
            flags.append("popularity_unknown_or_insufficient")
        if basis != "rated_releases":
            flags.append("weak_artist_prior" if anchors else "unpersonalized")
        items.append(RecommendationItem(release_id=release.id, score=score,
                                       components=components, reason_codes=reasons,
                                       uncertainty_flags=flags))
    warnings = ["Personal taste profile is unavailable"] if not anchors else []
    if artifact is not None:
        from discovery.training.serving import (
            artifact_hash,
            compatibility_reason,
            predict_components,
            validate_artifact,
        )

        reason = compatibility_reason(artifact, request.genre_scope_id, catalog.taxonomy_version, feature_version)
        if _experimental:
            # Offline evaluation only. No HTTP/CLI recommendation input enables
            # this flag; artifacts remain unvalidated and are never rewritten.
            validate_artifact(artifact)
            reason = ("taxonomy_version_mismatch" if artifact["taxonomy_version"] != catalog.taxonomy_version
                      else "feature_version_mismatch" if artifact["feature_version"] != feature_version
                      else "genre_not_trained" if request.genre_scope_id not in artifact["trained_genre_ids"]
                      else None)
        if reason:
            warnings.append(f"Personal model fallback: {reason}")
        else:
            version = artifact_hash(artifact)
            # Apply to candidate scores BEFORE the same diversity/novelty selector.
            # The frozen input fields remain unchanged for subsequent feedback export.
            updated = []
            try:
                for item in items:
                    probability = predict_components(artifact, request.genre_scope_id, item.components,
                                                     allow_experimental=_experimental)
                    # Additive correction: quality, similarity, genre penalties and the
                    # mode's novelty target stay in the score; p=0.5 changes nothing.
                    learned_score = item.score + MODEL_WEIGHT*(probability-.5)
                    components = dict(item.components, model_probability=probability,
                                      baseline_score=item.score, ranking_model_version=version)
                    updated.append(item.model_copy(update={"score": learned_score, "components": components,
                                                            "reason_codes": [*item.reason_codes,
                                                                "experimental_personal_model" if _experimental
                                                                else "validated_personal_model"]}))
                items = updated
            except (ValueError, OverflowError, TypeError) as exc:
                warnings.append(f"Personal model fallback: inference_error:{type(exc).__name__}")
    # Request-local wishes are applied after optional learned scoring. No inference
    # of missing tags, and no persistent taste label is created from these words.
    requested_sound = bool(request.preferred_descriptors or request.avoided_descriptors)
    release_map = {r.id: r for r in catalog.releases}
    familiar_artists = {a for ids in profile.seed_artist_ids_by_genre.values() for a in ids}
    familiar_releases = set(profile.known_release_ids)
    for mapping in (profile.liked_release_ids_by_genre, profile.disliked_release_ids_by_genre):
        familiar_releases.update(r for ids in mapping.values() for r in ids)
    for rid in familiar_releases & release_map.keys():
        familiar_artists.update(release_map[rid].artist_ids)
    for item in items:
        if profile.limit_widely_known_artists or profile.artist_popularity_labels:
            classification = artist_popularity(release_map[item.release_id], profile)
            item.components["artist_popularity_classification"] = classification
            if classification != "unknown":
                item.components["artist_popularity_source"] = "explicit_user"
            else:
                item.uncertainty_flags.append("artist_popularity_unlabelled")
        familiar = bool(set(release_map[item.release_id].artist_ids) & familiar_artists)
        penalty = {"close": 0.0, "explore": .2, "surprise": .3}[request.mode] if familiar else 0.0
        item.score -= penalty
        item.components["familiar_artist_penalty"] = penalty
        if familiar:
            item.uncertainty_flags.append("personally_familiar_artist")
        if request.reference_artist_ids and item.components["similarity"] > 0:
            item.reason_codes.append("request_artist_reference")
        if requested_sound:
            weights, positions = descriptor_weights(release_map[item.release_id])
            descriptors = set(weights)
            preferred = sorted(descriptors & set(request.preferred_descriptors))
            avoided = sorted(descriptors & set(request.avoided_descriptors))
            adjustment = (.25 * fsum(weights[label] for label in preferred) / max(1, len(request.preferred_descriptors))
                          - .35 * fsum(weights[label] for label in avoided) / max(1, len(request.avoided_descriptors)))
            item.score += adjustment
            item.components.update(sound_adjustment=adjustment,
                                   sound_policy_version=SOUND_POLICY_VERSION,
                                   ordered_vibe_matches=", ".join(f"{label}: {positions[label]}" for label in sorted(set(preferred + avoided) & positions.keys())),
                                   matched_preferred_descriptors=", ".join(preferred),
                                   matched_avoided_descriptors=", ".join(avoided))
            item.uncertainty_flags.append("source_descriptors_are_subjective")
            if set(preferred + avoided) & positions.keys():
                item.uncertainty_flags.append("vibe_order_is_heuristic")
            if not descriptors:
                item.uncertainty_flags.append("sound_descriptors_unavailable")
    items.sort(key=lambda item: (-item.score, item.release_id))
    return RankingResult(items=items, profile_basis=basis,
                         candidate_ids=[item.release_id for item in items],
                         warnings=warnings)


def exploration_candidates(scored: RankingResult, request: RequestIntent) -> list[RecommendationItem]:
    lower, upper = EXPLORATION_NOVELTY_BOUNDS[request.mode]
    return [item for item in scored.items if item.components.get("novelty") is not None
            and lower < item.components["novelty"] <= upper]



def artist_popularity(release, profile: UserProfile) -> str:
    """Account evidence only; missing or mixed incomplete evidence stays unknown."""
    labels = [profile.artist_popularity_labels.get(aid) for aid in release.artist_ids]
    if any(label and label.classification == "widely_known" for label in labels):
        return "widely_known"
    if labels and all(label and label.classification == "less_known" for label in labels):
        return "less_known"
    return "unknown"


def counts_toward_popularity_limit(release, request: RequestIntent, profile: UserProfile) -> bool:
    explicit = set(request.requested_artist_ids)
    return any(aid not in explicit and (label := profile.artist_popularity_labels.get(aid))
               and label.classification == "widely_known" for aid in release.artist_ids)


def rank(catalog: Catalog, request: RequestIntent, profile: UserProfile,
         as_of: date | None = None, *, artifact: dict | None = None,
         feature_version: str = FEATURE_VERSION, _experimental: bool = False) -> RankingResult:
    scored = score_candidates(catalog, request, profile, as_of, artifact=artifact,
                              feature_version=feature_version, _experimental=_experimental)
    releases = {r.id: r for r in catalog.releases}
    _, tags = _tags(catalog, request.genre_scope_id)
    # Broad-family redundancy complements fine tags only in unrestricted mode.
    # It is a preference, never a genre quota that could override hard filters.
    roots = {g.id: catalog.descendant_ids(g.id) for g in catalog.genres if not g.parent_ids} if request.genre_scope_id == ALL_GENRES and not request.locked_subgenre_ids else {}
    families = {rid: {gid for gid, members in roots.items() if set(r.genre_ids) & members}
                for rid, r in releases.items()} if roots else {}
    selected = []
    used_artists: set[str] = set()

    def take(pool: list[RecommendationItem], count: int) -> None:
        for _ in range(count):
            eligible = [item for item in pool if item.release_id not in {r.release_id for r in selected}
                        and not set(releases[item.release_id].artist_ids) & used_artists
                        and (not profile.limit_widely_known_artists
                             or not counts_toward_popularity_limit(releases[item.release_id], request, profile)
                             or sum(counts_toward_popularity_limit(releases[s.release_id], request, profile)
                                    for s in selected) < min(profile.max_widely_known_artists, (request.count - 1) // 2))]
            if not eligible:
                break
            def utility(item: RecommendationItem) -> tuple[float, str]:
                redundancy = max((_jaccard(tags[item.release_id], tags[s.release_id]) for s in selected), default=0)
                if families:
                    redundancy = max(redundancy, max((_jaccard(families[item.release_id], families[s.release_id]) for s in selected), default=0))
                return (-item.score + .18*redundancy, item.release_id)
            chosen = min(eligible, key=utility)
            selected.append(chosen)
            used_artists.update(releases[chosen.release_id].artist_ids)

    if scored.profile_basis == "unavailable":
        take(scored.items, request.count)
    else:
        # Candidates without specific tags have unknown novelty: ordinary pool only.
        close = [item for item in scored.items
                 if item.components["novelty"] is None or item.components["novelty"] <= .5]
        exploratory = exploration_candidates(scored, request)
        close_n = min(request.count, ceil(request.count*{"close": .8, "explore": .6, "surprise": .2}[request.mode]))
        if request.count == 1:
            close_n = int(request.mode == "close")
        take(close, close_n)
        take(exploratory, request.count-close_n)
        if len(selected) < request.count:
            # Quotas are preferences, not strict filters. Fill from the SAME
            # permitted pools without relaxing scope, rarity or exclusions.
            before = len(selected)
            take(close + exploratory, request.count-len(selected))
            if len(selected) > before:
                scored.warnings.append("Requested novelty balance unavailable; used remaining eligible candidates")
    warnings = list(scored.warnings)
    if profile.limit_widely_known_artists:
        # Strict minority uses the actual resulting length, not the requested
        # five slots. Removing one popular item can shrink the allowed count again.
        while selected:
            popular = [item for item in selected if counts_toward_popularity_limit(
                releases[item.release_id], request, profile)]
            if len(popular) <= min(profile.max_widely_known_artists, (len(selected) - 1) // 2):
                break
            selected.remove(min(popular, key=lambda item: (item.score, item.release_id)))
        warnings.append("Личный лимит: исполнители с меткой «широко известный» доступны только по прямому запросу."
                        if profile.max_widely_known_artists == 0 else
                        f"Личный лимит: исполнители с меткой «широко известный» — не больше {profile.max_widely_known_artists} и меньше половины фактической подборки; прямой запрос — исключение.")
        if any(artist_popularity(releases[item.release_id], profile) == "unknown" for item in scored.items):
            warnings.append("Известность части кандидатов неизвестна: лимит проверен только по вашим явным меткам. Неизвестные не объявляются малоизвестными; полную гарантию дать нельзя.")
    if len(selected) < request.count:
        warnings.append("Not enough eligible distinct artists in the requested novelty pools")
    return RankingResult(items=selected, warnings=warnings, profile_basis=scored.profile_basis,
                         candidate_ids=scored.candidate_ids)
