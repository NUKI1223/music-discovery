"""Run: uv run streamlit run src/discovery/ui/app.py"""

import json
import os
import sqlite3
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import streamlit as st

from discovery.catalog.io import load_catalog
from discovery.cli import training_records
from discovery.config import DEFAULT_CATALOG, load_project_env
from discovery.contracts import Catalog, Feedback, RequestIntent, UserProfile
from discovery.service import discovery_metrics, recommend
from discovery.storage import Store, catalog_digest, database_path

load_project_env()
st.set_page_config(page_title="Music Discovery", page_icon="♫", layout="wide")


def initial_profile(catalog: Catalog) -> UserProfile:
    """Pilot choices are configuration; the recommendation engine is genre neutral."""
    path = Path("configs/pilot.json")
    if not path.exists():
        return UserProfile()
    settings = json.loads(path.read_text())
    scopes = {g.id for g in catalog.genres}
    seeds, avoids = {}, {}
    for scope, config in settings.get("genres", {}).items():
        if scope not in scopes:
            continue
        names = {n.casefold() for n in config.get("seed_artist_names", [])}
        configured_ids = set(config.get("seed_artist_ids", []))
        seeds[scope] = [a.id for a in catalog.artists
                        if a.id in configured_ids or a.name.casefold() in names]
        avoids[scope] = [g for g in config.get("avoid_tag_ids", []) if g in scopes]
    return UserProfile(seed_artist_ids_by_genre=seeds, avoid_tags_by_genre=avoids)


def record(decision_id, release_id, kind, familiarity=None):
    try:
        store.append_feedback(Feedback(
            id=str(uuid4()), decision_id=decision_id, release_id=release_id,
            kind=kind, familiar_before_recommendation=familiarity,
        ))
        return True
    except (ValueError, sqlite3.Error):
        st.error("Не удалось сохранить ответ. Попробуй ещё раз.")
        return False


st.title("Music Discovery")
st.caption("Знакомый жанр. Новые открытия. Твой вкус.")
configured = Path(os.environ.get("DISCOVERY_CATALOG", DEFAULT_CATALOG))
paths = [configured]
# Raw acquisition caches can contain thousands of JSON responses; they are not UI catalogues.
paths.extend(p for directory in ("data/catalogs", "data/demo", "data/samples")
             for p in sorted(Path(directory).glob("*.json"))
             if p != configured and p.name != "manifest.json")
available = {}
for path in paths:
    try:
        candidate = load_catalog(path)  # also reads the committed catalog.json.gz in a fresh clone
        label = f"{'Учебный' if candidate.is_fixture else 'Реальный'} · {candidate.snapshot_id}"
        available[label] = (path, candidate)
    except (ValueError, OSError):
        continue
if not available:
    st.error("Нет доступного каталога. Сначала импортируй каталог по инструкции в README.")
    st.stop()

with st.sidebar:
    st.header("Коллекция")
    label = st.selectbox("Каталог", list(available))
catalog_path, catalog = available[label]
db_path = Path(os.environ.get("DISCOVERY_DB", "runtime/discovery.sqlite3"))
# A demo's invented outcomes must never train a personal model alongside real music.
db_path = database_path(db_path, catalog)
store = Store(db_path)
profile = store.profile(initial_profile(catalog))
if catalog.is_fixture:
    st.warning("Учебный каталог: релизы и числовые показатели вымышлены. "
               "Он проверяет механику приложения; оценки сохраняются отдельно от реальной музыки.")
else:
    st.info("Рейтинг и известность показываются только при достаточных данных. "
            "Реальный каталог пока может быть небольшим.")
    rated = sum(any(o.rating is not None for o in release.observations)
                for release in catalog.releases)
    counted = sum(any(o.rating_count is not None for o in release.observations)
                  for release in catalog.releases)
    st.caption(f"Общая оценка сообщества есть у {rated} из {len(catalog.releases)} релизов; "
               f"число голосов — у {counted}. Это данные источника, а не твои оценки.")
    historical = sum(bool(r.historical_observations) for r in catalog.releases)
    if historical:
        st.caption(f"Исторические данные RYM/AOTY есть у {historical} релизов. "
                   "Дата измерения неизвестна; для текущей малоизвестности они не используются.")

genre_map = {g.id: g for g in catalog.genres}
root_ids = [g.id for g in catalog.genres if not g.parent_ids]
with st.sidebar:
    scopes = root_ids or list(genre_map)
    initial_scope = next((i for i, gid in enumerate(scopes)
                          if profile.seed_artist_ids_by_genre.get(gid)), 0)
    scope = st.selectbox("Жанр", scopes, index=initial_scope,
                         format_func=lambda gid: genre_map[gid].name)
    mode_names = {"close": "Близко к вкусу", "explore": "Расширить вкус", "surprise": "Удиви меня"}
    mode = st.radio("Насколько новое?", list(mode_names), format_func=mode_names.get, index=1)
    strict = st.checkbox("Только подтверждённо малоизвестное")
    scope_ids = catalog.descendant_ids(scope)
    children = sorted(scope_ids - {scope})
    locked = st.multiselect("Ограничить поджанрами", children,
                            format_func=lambda gid: genre_map[gid].name)
    artist_map = {a.id: a.name for a in catalog.artists}
    scoped_artist_ids = sorted({aid for r in catalog.releases
                               if set(r.genre_ids) & scope_ids
                               for aid in r.artist_ids})
    with st.expander("Настроить мой вкус"):
        seeds = st.multiselect("Любимые исполнители", scoped_artist_ids,
                               default=[a for a in profile.seed_artist_ids_by_genre.get(scope, [])
                                        if a in scoped_artist_ids],
                               format_func=artist_map.get, key=f"seeds-{catalog.snapshot_id}-{scope}")
        avoid = st.multiselect("Меньше этих направлений", list(genre_map),
                               default=[g for g in profile.avoid_tags_by_genre.get(scope, [])
                                        if g in genre_map], format_func=lambda g: genre_map[g].name,
                               key=f"avoid-{catalog.snapshot_id}-{scope}")
        if st.button("Сохранить предпочтения"):
            profile.seed_artist_ids_by_genre[scope] = seeds
            profile.avoid_tags_by_genre[scope] = avoid
            store.set_base_profile(profile)
            st.success("Предпочтения сохранены для этого жанра.")
    use_bandit = st.checkbox("Эксперимент: исследовать пятый вариант", value=False)
    st.caption(f"{len(catalog.releases)} релизов · {len(catalog.artists)} исполнителей")

intent = RequestIntent(genre_scope_id=scope, mode=mode, obscurity="require" if strict else "prefer",
                       locked_subgenre_ids=locked)
text_request = st.text_input("Пожелания обычными словами — через OpenRouter",
                            placeholder="Например: удиви меня, но сохрани выбранный жанр")
if st.button("Найти музыку", type="primary"):
    try:
        artifact = None
        if os.environ.get("DISCOVERY_MODEL_PATH"):
            from discovery.training import load_optional_artifact

            artifact, model_warning = load_optional_artifact(os.environ["DISCOVERY_MODEL_PATH"])
            if model_warning:
                st.warning("Личная модель недоступна. Подборка использует базовое ранжирование.")
        if text_request.strip():
            from discovery.llm import BudgetLedger, LLMError, OpenRouterClient, OpenRouterSettings

            client = None
            try:
                client = OpenRouterClient(OpenRouterSettings.from_env(),
                                          BudgetLedger(db_path.parent / "llm-budget.sqlite3"))
                intent = client.parse_request(text_request, catalog, intent)
            except LLMError as exc:
                st.error(f"Не удалось применить текстовый запрос: {exc}. "
                         "Уточни настройки слева или очисти текст.")
                st.stop()
            finally:
                if client:
                    client.close()
        decision = recommend(store, catalog, intent, store.profile(initial_profile(catalog)),
                             use_bandit=use_bandit, artifact=artifact)
        st.session_state["decision_id"] = decision.id
        st.session_state["decision_db"] = str(db_path)
    except (ValueError, OSError, sqlite3.Error) as exc:
        st.error(str(exc))

decision = None
if st.session_state.get("decision_db") == str(db_path):
    decision = store.get_decision(st.session_state.get("decision_id", ""))
current_catalog_hash = catalog_digest(catalog) if decision else None
if decision and decision.catalog_hash != current_catalog_hash:
    st.info("Каталог изменился. Создай новую подборку; предыдущая сохранена в истории.")
if decision and decision.catalog_hash == current_catalog_hash:
    st.subheader("Твоя подборка")
    st.caption(f"{genre_map.get(decision.request.genre_scope_id).name} · "
               f"{mode_names[decision.request.mode]}")
    if decision.result.profile_basis == "unavailable":
        st.info("В этом жанре пока нет истории вкуса. Начинаем с разнообразной подборки; "
                "выбери любимых исполнителей или оцени прослушанные релизы.")
    if not decision.result.items:
        st.warning("Под заданные условия пока нет релизов. Можно расширить поджанры "
                   "или отключить обязательную малоизвестность.")
    elif len(decision.result.items) < decision.request.count:
        st.info("Под все условия нашлось меньше пяти разных исполнителей.")
    releases = {r.id: r for r in catalog.releases}
    for index, item in enumerate(decision.result.items, 1):
        release = releases.get(item.release_id)
        if release is None:
            continue
        with st.container(border=True):
            st.markdown(f"### {index}. {release.title}")
            st.write(" · ".join(artist_map.get(a, a) for a in release.artist_ids))
            st.caption(" · ".join(genre_map[g].name for g in release.genre_ids)
                       + (f" · {release.year}" if release.year else ""))
            latest_observations = {o.source: o for o in sorted(
                release.observations, key=lambda observation: observation.observed_at)}
            for observation in latest_observations.values():
                if observation.rating is not None:
                    votes = (f" · голосов: {observation.rating_count}"
                             if observation.rating_count is not None else " · число голосов неизвестно")
                    st.caption(f"{observation.source}: {observation.rating:g}/{observation.rating_scale:g}"
                               f"{votes} · данные от {observation.observed_at.date().isoformat()}")
            if release.historical_observations:
                with st.expander("Исторические оценки RYM/AOTY"):
                    for observation in release.historical_observations:
                        score = (f"{observation.rating:g}/{observation.rating_scale:g}"
                                 if observation.rating is not None else "неизвестна")
                        count = observation.rating_count_text or "неизвестно"
                        st.caption(f"{observation.source.upper()}: {score} · "
                                   f"счётчик в выгрузке: {count} · дата измерения неизвестна")
                    st.caption("Это исторические сведения издателей выгрузок; "
                               "значения разных источников не усредняются.")
            if "tag_affinity" in item.reason_codes:
                st.write("Есть общие жанровые признаки с твоими музыкальными ориентирами.")
            if "low_source_vote_percentile" in item.reason_codes:
                st.write("Входит в менее заметную часть сопоставимой группы этого каталога.")
            if "popularity_unknown_or_insufficient" in item.uncertainty_flags:
                st.caption("Известность: недостаточно данных для уверенного сравнения.")
            for name, url in release.links.items():
                if urlparse(url).scheme in {"http", "https"}:
                    st.link_button(name, url)
            familiarity = st.selectbox("Ты знал этот релиз до рекомендации?",
                                       ["Не уверен / не отвечать", "Нет", "Да"],
                                       key=f"familiarity-{decision.id}-{release.id}")
            fam_value = {"Не уверен / не отвечать": None, "Нет": False, "Да": True}[familiarity]
            if fam_value is not None and st.button("Сохранить знакомость",
                                                   key=f"save-familiarity-{decision.id}-{release.id}"):
                if record(decision.id, release.id, "familiarity", fam_value):
                    st.success("Ответ сохранён.")
            columns = st.columns(4)
            options = [("Уже знаю", "known"), ("Послушать позже", "save_for_later"),
                       ("Прослушал: нравится", "listened_like"),
                       ("Прослушал: не нравится", "listened_dislike")]
            for column, (button_label, kind) in zip(columns, options):
                if column.button(button_label, key=f"{decision.id}-{release.id}-{kind}"):
                    if record(decision.id, release.id, kind, True if kind == "known" else fam_value):
                        st.success("Сохранено. Следующая подборка учтёт твой ответ.")
            if st.button("Не тот жанр", key=f"wrong-{decision.id}-{release.id}"):
                if record(decision.id, release.id, "wrong_genre", fam_value):
                    st.success("Замечание о жанре сохранено отдельно от оценки вкуса.")
            with st.expander("Почему предложен этот релиз"):
                st.json({"показатели": item.components, "неопределённость": item.uncertainty_flags})
    store.mark_shown(decision)

with st.expander("История и мои данные"):
    st.caption("Показатели основаны только на твоих ответах. Отсутствие оценки не означает дизлайк.")
    # Streamlit executes closed expanders too. Large immutable snapshots belong
    # only in explicitly requested exports, never in a routine recommendation rerun.
    if st.button("Показать статистику"):
        st.json(discovery_metrics(store))
    if st.button("Подготовить экспорт истории"):
        with st.spinner("Подготавливаем историю…"):
            history_json = json.dumps(store.export(), ensure_ascii=False, indent=2)
        st.download_button("Экспортировать историю", history_json,
                           file_name="discovery-history.json", mime="application/json",
                           on_click="ignore")
    if st.button("Подготовить данные для обучения"):
        with st.spinner("Подготавливаем данные для обучения…"):
            training_json = json.dumps(training_records(store), ensure_ascii=False, indent=2)
        st.download_button("Данные для обучения", training_json,
                           file_name="training-records.json", mime="application/json",
                           on_click="ignore")
