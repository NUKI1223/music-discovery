"""Public release cards built exclusively from immutable recommendation snapshots."""

import threading
from collections import OrderedDict
from urllib.parse import urlsplit

from discovery.catalog.web.common import validate_url
from discovery.contracts import ALL_GENRES, Catalog, Decision
from discovery.ranking.source_descriptors import matched_descriptions
from discovery.storage import Store, catalog_digest

from .schemas import CollectionChat, HistoricalRating, ReleaseCard, SourceLink


def safe_link(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return (parsed.scheme in {"http", "https"} and bool(parsed.hostname)
                and parsed.username is None and parsed.password is None
                and not any(ord(char) < 32 for char in value))
    except ValueError:
        return False


def decision_cards(catalog: Catalog, decision: Decision) -> list[ReleaseCard]:
    releases = {release.id: release for release in catalog.releases}
    artists = {artist.id: artist.name for artist in catalog.artists}
    genres = {genre.id: genre.name for genre in catalog.genres}
    cards = []
    for item in decision.result.items:
        release = releases[item.release_id]
        reasons = (["Подборка среди всех жанров с учётом ваших предпочтений и условий поиска."]
                   if decision.request.genre_scope_id == ALL_GENRES else
                   [f"Соответствует выбранному жанру: {genres[decision.request.genre_scope_id]}."])
        if not release.genre_ids:
            reasons.append("Жанр этого релиза в каталоге пока не определён.")
        if "tag_affinity" in item.reason_codes:
            reasons.append("Есть общие жанровые признаки с вашими музыкальными ориентирами.")
        if "request_artist_reference" in item.reason_codes:
            reasons.append("Ориентир для этой подборки — исполнители из текущего запроса.")
        preferred = item.components.get("matched_preferred_descriptors")
        avoided = item.components.get("matched_avoided_descriptors")
        if preferred:
            for source, field, labels in matched_descriptions(release, decision.request.preferred_descriptors):
                name = f"{source.upper()} Vibe" if field == "vibes" else f"{source.upper()} Descriptors"
                reasons.append(f"В описании {name} совпадают пожелания: {', '.join(labels)}.")
        if item.components.get("ordered_vibe_matches"):
            reasons.append("Учтён порядок Vibe на странице AOTY: " + str(item.components["ordered_vibe_matches"]) + ". Первые метки имеют больший вес.")
        if "low_source_vote_percentile" in item.reason_codes:
            reasons.append("Входит в менее заметную часть сопоставимой группы этого каталога.")
        warnings = []
        if "vibe_order_is_heuristic" in item.uncertainty_flags:
            warnings.append("Порядок Vibe используется как ориентир, а не измерение силы звучания или числа голосов.")
        popularity = item.components.get("artist_popularity_classification")
        if popularity == "widely_known":
            warnings.append("По вашей явной оценке: широко известный исполнитель. Это личная метка, не измерение текущей аудитории.")
        elif popularity == "less_known":
            reasons.append("По вашей явной оценке: менее известный исполнитель.")
        elif "artist_popularity_unlabelled" in item.uncertainty_flags:
            warnings.append("Нет подтверждённой метки известности исполнителя; он не считается малоизвестным автоматически.")
        if "personally_familiar_artist" in item.uncertainty_flags:
            warnings.append("Исполнитель уже есть в вашем профиле или истории прослушивания.")
        if avoided:
            for source, field, labels in matched_descriptions(release, decision.request.avoided_descriptors):
                name = f"{source.upper()} Vibe" if field == "vibes" else f"{source.upper()} Descriptors"
                warnings.append(f"В описании {name} есть нежелательные признаки из запроса: {', '.join(labels)}.")
        if "source_descriptors_are_subjective" in item.uncertainty_flags:
            warnings.append("Описания источников субъективны и не являются измерением аудио или проверкой вокала; отсутствие метки не доказывает отсутствие свойства.")
        if "sound_descriptors_unavailable" in item.uncertainty_flags:
            warnings.append("Для этого релиза нет описаний звучания; пожелания по звучанию проверить нельзя.")
        if "popularity_unknown_or_insufficient" in item.uncertainty_flags:
            warnings.append("Сопоставимая статистика аудитории отсутствует; личная метка не заменяет измерение."
                            if popularity in {"widely_known", "less_known"} else
                            "Известность неизвестна: недостаточно данных для уверенного сравнения.")
        if "weak_artist_prior" in item.uncertainty_flags:
            warnings.append("Вкус пока оценён только по выбранным исполнителям.")
        if "unpersonalized" in item.uncertainty_flags:
            warnings.append("Для персонализации выберите любимых исполнителей или оцените прослушанное.")
        if catalog.is_fixture:
            warnings.append("Учебный каталог: этот релиз и его показатели вымышлены.")
        if release.historical_observations:
            warnings.append("Исторические оценки: дата измерения неизвестна; счётчики источников не сопоставимы.")
        links = dict(release.links)
        for observation in [*release.observations, *release.historical_observations]:
            if observation.url:
                links.setdefault(observation.source.upper(), observation.url)
        if any(source == "aoty" and field == "vibes" for source, field, _ in matched_descriptions(
            release, decision.request.preferred_descriptors + decision.request.avoided_descriptors
        )):
            # Reuse a source URL actually stored with the release, never fabricate a page.
            vibe_url = None
            for url in links.values():
                if not safe_link(url):
                    continue
                try:
                    vibe_url = validate_url(url, "aoty")
                    break
                except ValueError:
                    continue
            if vibe_url:
                links["AOTY Vibe"] = vibe_url
        cards.append(ReleaseCard(
            release_id=release.id, decision_id=decision.id, title=release.title,
            artists=[artists[aid] for aid in release.artist_ids], year=release.year,
            genres=[genres[gid] for gid in release.genre_ids], explanation=reasons,
            warnings=warnings,
            links=[SourceLink(label=label, url=url) for label, url in links.items() if safe_link(url)],
            historical_ratings=[HistoricalRating(source=o.source.upper(), rating=o.rating,
                                                scale=o.rating_scale, count=o.rating_count)
                                for o in release.historical_observations],
        ))
    return cards


_SNAPSHOTS: OrderedDict[str, Catalog] = OrderedDict()
_SNAPSHOTS_LOCK = threading.Lock()
_SNAPSHOT_LIMIT = 2


def _snapshot(store: Store, hashed: str) -> Catalog:
    """Process-wide LRU of immutable snapshots (hash-addressed, so safe across accounts)."""
    with _SNAPSHOTS_LOCK:
        if hashed in _SNAPSHOTS:
            _SNAPSHOTS.move_to_end(hashed)
            return _SNAPSHOTS[hashed]
    catalog = store.get_catalog(hashed)
    _remember(hashed, catalog)
    return catalog


def _remember(hashed: str, catalog: Catalog) -> None:
    with _SNAPSHOTS_LOCK:
        _SNAPSHOTS[hashed] = catalog
        _SNAPSHOTS.move_to_end(hashed)
        while len(_SNAPSHOTS) > _SNAPSHOT_LIMIT:
            _SNAPSHOTS.popitem(last=False)


def collection_cards(store: Store, catalog: Catalog | None = None) -> list[ReleaseCard]:
    latest = {}
    events = store.feedback()
    visible = store.collection_release_ids()
    listened = {event.release_id for event in events if event.kind in {"known", "listened_like", "listened_dislike"}}
    for feedback in events:
        if feedback.release_id in visible and feedback.kind in {"known", "save_for_later", "listened_like", "listened_dislike"}:
            latest[feedback.release_id] = feedback
    if not latest:
        return []
    origins: dict[str, list[CollectionChat]] = {}
    with store.connection() as conn:
        # Non-web/imported histories need not contain conversation tables.
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "web_messages" in tables:
            for rid, chat_id, title in conn.execute("""SELECT DISTINCT
                json_extract(card.value,'$.release_id'), c.id, c.title
                FROM web_messages m JOIN web_chats c ON c.id=m.chat_id,
                json_each(m.payload,'$.cards') card ORDER BY c.created_at DESC, c.id"""):
                origins.setdefault(rid, []).append(CollectionChat(id=chat_id, title=title))
        if "web_deleted_chat_releases" in tables:
            for chat_id, rid in conn.execute("SELECT chat_id,release_id FROM web_deleted_chat_releases ORDER BY chat_id"):
                origins.setdefault(rid, []).append(CollectionChat(id=chat_id, title="Удалённый чат", deleted=True))
    current = catalog_digest(catalog) if catalog is not None else None
    decisions = {}
    result = []
    # An updated mark moves the album to the top; dict insertion order does not.
    for feedback in reversed(events):
        if latest.get(feedback.release_id) is not feedback:
            continue
        if feedback.decision_id not in decisions:
            decision = store.get_decision(feedback.decision_id)
            if decision is None:
                continue
            snapshot = catalog if decision.catalog_hash == current else _snapshot(store, decision.catalog_hash)
            decisions[decision.id] = decision_cards(snapshot, decision)
        card = next(card for card in decisions[feedback.decision_id]
                    if card.release_id == feedback.release_id)
        result.append(card.model_copy(update={"feedback": feedback.kind, "listened": card.release_id in listened,
                                              "saved_at": feedback.recorded_at.isoformat(),
                                              "chats": origins.get(card.release_id, [])}))
    return result
