# Локальный сайт

## Запуск

```bash
cd ~/Projects/hiphop-discovery
uv sync --locked
npm --prefix frontend ci
npm --prefix frontend run build
uv run discovery-web serve
```

Адрес: http://127.0.0.1:8000. Для следующих запусков достаточно последней команды.
FastAPI обслуживает API и сборку React одним сервером. Streamlit остаётся доступным
через прежнюю команду из README. Публичное размещение в эту версию не входит.

## Аккаунты и история

Логин: 3–32 латинских буквы, цифры, `_` или `-`, без различия регистра. Пароль:
12–128 символов. Каждый аккаунт получает собственные предпочтения, диалоги,
рекомендации и оценки. Стартовый вкус подключается отдельной кнопкой и не назначается
автоматически. «Уже знаю» влияет на исключения; «Послушать позже» сохраняет релиз.
Обучающие метки создают только «Прослушал — нравится» и «Прослушал — не нравится».
«Прослушал — смешанно» отмечает релиз прослушанным без метки; к любой оценке
«Прослушал» можно добавить балл 0–100 и рецензию, на подбор они не влияют
(см. [свои оценки](own-ratings-20261110.md)).

Фильтр «Исключать знакомых исполнителей» убирает все релизы любимых исполнителей
из профиля (любого жанра), а также авторов релизов с отметками «Уже знаю» или
«Прослушал». «Послушать позже» не означает знакомства. Фильтр включается отдельно
для диалога с подтверждением; сами ориентиры вкуса сохраняются. Приложение может
исключить только тех знакомых исполнителей, о которых есть эти данные.

По умолчанию аккаунты и сессии находятся в `runtime/web/accounts.sqlite3`, личные
данные — в `runtime/web/users/<внутренний-id>/discovery.sqlite3`. Корень можно выбрать
глобальным аргументом `--data-dir` перед подкомандой. Пароли хешируются Argon2
через pwdlib, как в [руководстве FastAPI](https://fastapi.tiangolo.com/tutorial/security/oauth2-jwt/).
Сессии действуют семь дней, cookie HttpOnly/SameSite=Lax; выход отзывает сессию.
Изменяющие API-запросы требуют Origin и CSRF-токен сессии (регистрация и вход —
Origin). Неудачные входы ограничены по логину и адресу.

В интерфейсе пароль меняется после проверки текущего. Если пароль забыт:

```bash
uv run discovery-web reset-password YOUR_LOGIN
```

CLI запрашивает новый пароль без отображения и отзывает все сессии аккаунта.
Пароль не передаётся аргументом командной строки.

Старую однопользовательскую историю можно импортировать в явно выбранный аккаунт
без созданных рекомендаций. Сначала зарегистрируй аккаунт, останови сервер, затем:

```bash
uv run discovery-web import-legacy YOUR_LOGIN runtime/discovery.sqlite3
```

Команда сохраняет копии исходной и целевой баз в `runtime/web/backups/`, переносит
каталоги, рекомендации, показы, предпочтения и оценки. Повторный импорт запрещён.
Старая база не изменяется. Полные переписки появляются только в новом веб-интерфейсе.
Для резервирования всего сайта останови сервер и скопируй каталог `runtime/web`.

## Помощник и расходы

Полная переписка остаётся локально. Модель получает до шести последних обменов,
фильтры и компактные сведения о предыдущих карточках; старые сообщения удаляются
из передаваемого контекста при превышении размера. Текст не становится оценкой вкуса.
Релизы и объяснения строятся по каталогу и существующему рекомендателю. Новая
подборка исключает уже показанное в этом диалоге. Смена жанра и строгих условий
требует подтверждения в интерфейсе.

Свободные пожелания к звучанию и сравнения с исполнителями преобразуются в
предложение жанровых направлений. Например, «больше жёсткости, как у Death Grips»
может предложить industrial/hardcore/experimental hip-hop. Для упомянутых по имени
исполнителей модели передаются их жанровые метки из каталога. Это приближение,
а не измеренная похожесть звучания. После подтверждения нажми «Применить и подобрать»
или продолжи диалог с помощником. Если подходящего направления нет, помощник уточняет запрос.
Точные требования без данных (например, строго 137 BPM) остаются неподдерживаемыми.

Стандартные команды «Ещё варианты», «Более неожиданное» и «Мне не нужны альбомы
тех исполнителей, которых я знаю» обрабатываются напрямую без платного вызова.
Дополнительные условия в сообщении передаются модели целиком. Старые отказы
сохраняются в переписке, но не передаются модели как действующие ограничения.

Модель — `openai/gpt-5.6-luna`; ключ только в серверном `.env`.
Веб-аккаунты используют прежний `runtime/llm-budget.sqlite3` рядом с `DISCOVERY_DB`,
вместе со Streamlit/CLI. Максимум $0.01 на сообщение с повторами/исправлениями,
$0.10 за сутки UTC; меньшие настроенные лимиты сохраняются. Смена `--data-dir`
не создаёт отдельный бюджет. При неизвестной цене сетевого сбоя резерв остаётся.

Повторная доставка того же `message_id` возвращает сохранённый результат, а
другое содержимое с тем же ID отклоняется. После остановки сервера незавершённое
сообщение помечается прерванным и автоматически не перезапускается. При отсутствии
ключа, ошибке провайдера или исчерпании лимита помощник не ответит; можно изменить фильтры или повторить запрос позже.

Исторические RYM/AOTY показаны отдельно: исходные шкалы 5 и 100, дата измерения
неизвестна. Они не подтверждают текущую малоизвестность. Строгий фильтр может
вернуть пустую подборку. Без подтверждённого источника обложки показана заглушка.

## Разработка и проверка

Контракт: `docs/web-openapi.json`; модели: `src/discovery/web/schemas.py`.
После согласованного изменения выполнить:

```bash
uv run python scripts/export_web_contract.py
npm --prefix frontend run generate:api-types
uv run pytest -q
uv run ruff check src tests scripts
npm --prefix frontend run build
npm --prefix frontend exec -- playwright install chromium
npm --prefix frontend run test:e2e
```

Браузерные проверки используют отдельные базы и синтетический каталог; платные
вызовы отключены в `scripts/web_test_server.py`. Проверки реального API сверяют
ответы с тем же OpenAPI, из которого генерируются TypeScript-типы.

Для явной сетевой проверки есть `uv run python scripts/web_live_smoke.py`:
три сценария, отдельная временная история, общий настоящий ledger и дополнительный
суммарный потолок $0.01. Первоначальный запуск на Mistral 2026-10-29 прошёл все три сценария (уточнение,
объяснение прошлой карточки, неподдерживаемое пожелание), включая повторную
доставку каждого ID без нового вызова. Фактическая общая стоимость $0.0003128.
Отчёт без ключей: `artifacts/web/live-smoke.json`.

На дату проверки публичная [страница модели OpenRouter](https://openrouter.ai/openai/gpt-5.6-luna)
подтверждает настроенные тарифы $0.20/$1.20 за миллион входных/выходных токенов.

Сравнение 2026-10-30: GPT-5.6 Luna и Gemini 3.5 Flash Lite прошли те же три
сценария без повторных вызовов. Стоимость соответственно $0.0017057 и $0.0024155.
Выбрана Luna как более дешёвая из двух прошедших проверку моделей. Это небольшой
smoke-тест, а не доказательство превосходства во всех диалогах. Отчёт:
`artifacts/web/model-comparison.json`.

## Результаты проверки 2026-10-30

- 475 Python-тестов, включая все исходные 421, прошёл; восемь offline-сценариев зелёные.
- Четыре браузерных сценария Playwright прошли: регистрация/вкус/подбор/уточнение/
  оценка/коллекция/повторный вход и второй аккаунт; мобильный экран/клавиатура/
  двойной клик/потерянный ответ/повтор того же ID; реальная остановка и повторный
  запуск серверного процесса с сохранением сессии, переписки и оценки.
- Подстановка чужих ID в чтение/изменение чата, отправку сообщения и оценку
  возвращает отказ; совпадающие ID в разных аккаунтах не смешивают их базы.
- Ruff, TypeScript и production-сборка проходят. `npm audit` — 0 уязвимостей.
- В собранных клиентских файлах нет настроенного ключа или серверной переменной
  `OPENROUTER_API_KEY`. Десктопный и мобильный скриншоты проверены визуально.
- Live OpenRouter — три успешных сценария, $0.0003128 суммарно, без повторных
  платных вызовов при доставке уже обработанных сообщений.

Дополнение: регрессия «знакомые исполнители → более неожиданное» проверяется
в `tests/web/test_chat.py` и четвёртом браузерном сценарии. Для явной проверки
свободных формулировок есть `uv run python scripts/web_known_artists_smoke.py`:
временная история со старым отказом, общий ledger, потолок $0.01 на запуск.
Последний прогон: три успешных случая, из них два вызова настоящей модели
(GPT-5.6 Luna, $0.00110155); стандартная команда выполнена локально без модели.

Отчёт Python/evals: `artifacts/evals/sound-release.json`. Браузерные артефакты:
`frontend/test-results/`. Проверки используют временные аккаунты и не импортируют
старую личную историю. Результаты подтверждают работу приложения; музыкальная
полезность персонализации по-прежнему требует реальных прослушиваний.

Проверка свободной формулировки: `uv run python scripts/web_sound_smoke.py`.
Три реальных сценария на GPT-5.6 Luna прошли: запрос с ориентиром Death Grips,
сохранение подтверждённых поджанров при уточнении и честный отказ от непроверяемого
точного BPM. Стоимость $0.0016783, отчёт `artifacts/web/sound-smoke.json`.
Отдельная браузерная проверка проходит ввод свободного текста, подтверждение и
получение релизов выбранного направления с mock-провайдером.

### Album artwork and listening links

Chat and collection share one release card. Authenticated
`GET /api/releases/{release_id}/artwork` now returns **all discovered platforms** in
`listening_links` (`service`, `url`, `source_url`) and per-source `link_status`.
Legacy `listening_url` / `listening_service` remain for older clients and cover attribution.

Apple and Deezer album searches run independently alongside MusicBrainz discovery.
An Apple match no longer suppresses other platforms. MusicBrainz requires an
unambiguous release group matching artist credits, title and catalogue year; it
collects explicit streaming/download relationships from the group and up to 300
official, matching editions. Edition/volume names are retained and disambiguated
deluxe, remastered, instrumental and clean editions are excluded. Truncated groups
are marked partial. Public album.link pages expand a matched store album after
robots.txt and page-entity identity checks. The retired Songlink API is not used.
No provider keys or paid model calls are needed.

Accepted direct pages: Spotify, Apple Music, YouTube Music album playlists,
Deezer, Bandcamp, Yandex Music, TIDAL, Amazon Music, Qobuz, Pandora, Napster,
Boomplay, Anghami and Beatport. Search pages, artist pages, tracks, arbitrary
YouTube playlists, credentials and unrecognized hosts are excluded. Each platform
appears once, with the catalogue link preferred where present. Metadata relationships
are evidence of a mapping, not a guarantee of playback availability in every country;
not every catalogue album has every platform mapped in these public sources.

AOTY cover handling is independent: the existing bounded robots-aware fetcher reads
verified album metadata and accepted CDN URLs, with a five-minute host cooldown
on access failures. Apple/Deezer covers and the existing MusicBrainz cover archive
remain fallbacks. Artwork loading has an eight-second UI deadline; platform discovery
starts immediately and shows a spinner until complete. Partial source outages are
reported without hiding successful links. Reduced motion disables spinner rotation.

Versioned `album-pages-v4` cache keys invalidate the former single-platform cache.
Positive complete discoveries last seven days; missing matches one day; partial
outages at most five minutes. Links update on opening cards, including old chats
and collections; no whole-catalogue claim or bulk crawl is implied. Identical in-flight
lookups within the server share a lock; different albums can resolve independently.
Two request workers isolate external lookup work from the main API pool. Persistent
SQLite rate reservations enforce provider spacing across local processes (Apple
3.1 seconds, MusicBrainz 1.1 seconds, album.link at least 3.1 seconds).
Metadata lives in `artwork.sqlite3`; recommendation snapshots, user ratings and chat
history are unchanged. API/browser tests use mock transports. See
[verification report](album-platforms-20261106.md) for the bounded live sample.

Provider references: [MusicBrainz API](https://musicbrainz.org/doc/MusicBrainz_API),
[release URL relationships](https://musicbrainz.org/relationships/release-url),
[album.link](https://album.link/),
[Apple Search API](https://performance-partners.apple.com/search-api).

Live verification: AOTY robots.txt and an album page returned HTTP 403 from
outside the sandbox. Live AOTY image extraction remains unverified. Parser,
priority, cache, cooldown and fallback tests use controlled fixtures; browser
checks do not establish real catalogue coverage. Direct AOTY access must work
before this integration can supply live AOTY covers. No challenge bypass or
unofficial proxy is used. Apple lookup documentation:
https://performance-partners.apple.com/search-api

### Long conversations and memory

`GET /api/chats/{id}` returns the latest 20 messages by default (limit 2–50).
`before` / `after` are exclusive sequence cursors within the authenticated account
and chat; only one direction is accepted. The response includes oldest/newest
sequence, has_older/has_newer, and chat memory. The browser replaces its current
page using «Ранее» / «Позже», keeping DOM/history responses bounded. Sending a new
message returns to the latest page. Existing direct internal `get_chat` calls may
still request the full archive; public reads and the model use bounded pages.

The per-account SQLite DB lazily builds `web_chat_memory` and `web_chat_cards`.
Recent six exchanges are passed verbatim while the input budget permits. Older
messages produce a deterministic, bounded extractive recap: up to six substantive
user excerpts (300 characters each), source message IDs and a processed-message
count. It is deliberately not a complete semantic summary. The latest substantive
request (up to 800 characters) is kept independently. No extra LLM call or charge is
made for summarization. New requests and confirmed filters override historical
excerpts; arbitrary semantic conflicts still depend on model interpretation.

«Память диалога» exposes editable chat-only notes (1,600 characters). Saving notes
does not modify permanent taste or feedback. Reset clears notes/recap/latest request
and excludes pre-reset turns from subsequent dialogue context; the archive, saved
ratings and shown-card exclusions remain. Explicit questions can still retrieve
old cards. Mutations are blocked while a response is pending.

Historical cards are indexed separately and retrieved by supplied ID, exact title,
artist/text matches, or a reference to the beginning of the chat. Up to five matches
and the last selection enter model context. Ambiguity must produce clarification;
this is lexical retrieval, not an embedding search. Account/chat filters apply to
all lookups. Exclusions use the complete card index, not the visible message page.

`MAX_INPUT_TOKENS` (environment default 16384) and `MAX_INPUT_BYTES` (65536) now govern
separate guards. Token sizing is a padded heuristic, not the provider tokenizer;
byte size is a hard transport ceiling. The same estimator checks construction and
submission, including schema overhead. Old pairs, then recap excerpts, then optional
vocabulary are reduced when needed. Message text remains limited to 4,000 characters.
Spend caps remain $0.01/request and $0.10/day; a larger reservation can reduce available
attempts under expensive model settings. Actual usage/cost checks remain enabled.
Changing model/context settings still requires checking provider compatibility.
