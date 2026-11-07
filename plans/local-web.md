# Локальный сайт Music Discovery

Решение от 2026-10-29: расширить существующий Python-прототип полноценным локальным
сайтом с личными аккаунтами и диалогами. Основание — утверждённый пользователем план.
Streamlit остаётся диагностическим интерфейсом со своей прежней историей.

## Контракт и ответственность

Координатор отвечает за `src/discovery/web/schemas.py`, экспорт
`docs/web-openapi.json`, интеграционные проверки и запуск. Артефакт OpenAPI создан
до параллельной реализации. Его потребитель — React/TypeScript frontend;
провайдер — FastAPI. Изменения согласуются с координатором, типы frontend
генерируются из артефакта, реальные HTTP-ответы проверяются по его схемам.

Три исполнителя работают в раздельных файлах: backend/auth (аккаунты, сессии,
профили, импорт), frontend (экраны и браузерные тесты), chat/evals (переписка,
действия модели, карточки, оценки, диалоговые тесты). После реализации —
взаимное ревью и общие проверки.

## Границы реализации

- FastAPI обслуживает собранный Vite frontend на `127.0.0.1:8000`.
- Общая SQLite для аккаунтов и серверных сессий; отдельная база каждого
  пользователя хранит предпочтения, решения, оценки и полные чаты.
- Argon2, HttpOnly cookie на семь дней, отзыв сессий, Origin/CSRF, ограничение
  попыток входа. Без почты и внешних сервисов авторизации.
- Явный выбор стартового вкуса; старую историю нельзя автоматически присвоить
  новому аккаунту. Импорт и сброс пароля выполняются локальной CLI-командой.
- LLM предлагает проверяемое действие; существующий рекомендатель и каталог
  определяют релизы и факты. Жанр и строгие ограничения меняются с подтверждением.
- До шести обменов в контексте, ограничение размера, уникальный message_id,
  без автоматического повторного запуска прерванного запроса.
- Общий прежний ledger OpenRouter, максимум $0.01 на сообщение и $0.10/сутки
  для всех аккаунтов вместе. История, оценки и обычный подбор бесплатны.
- Только явные оценки прослушанного становятся обучающими метками.
- Русский адаптивный интерфейс, тёмная тема, клавиатура, источники и
  исторические оценки с исходными шкалами; заглушки для обложек.

## Проверка готовности

Сохранить зелёными 421 исходный тест и восемь сценариев; добавить API-проверки
изоляции двух аккаунтов, авторизации, CSRF, идемпотентности и перезапуска;
диалоговые mock-evals и сквозной браузерный сценарий. Проверить сборку на отсутствие
ключа. Реальный OpenRouter smoke — максимум три сценария, суммарно до $0.01,
с общим ledger. Фактические результаты фиксируются в `docs/local-web.md`.

Публичный хостинг, OAuth, email, воспроизведение и автоматическое переобучение
не входят в эту версию.

## Release presentation

Add shared album artwork and listening actions to chat/collection cards. Derive
Cover Art Archive URLs only from exact MusicBrainz release/release-group links;
show a styled fallback for missing or failed artwork. Keep existing snapshot
contracts unchanged so saved chats also gain the presentation. Separate supplied
streaming links from clearly labeled service search links; never invent album IDs.
Match the existing dark olive/lime workspace and preserve feedback/explanation flows.
Prefer the exact catalogue AOTY page, with verified page identity and AOTY image
URLs. Fall back to Apple artist/title matching and existing CAA links. Persist
access diagnostics/cooldowns separately from immutable recommendation snapshots.
Live AOTY access currently returns HTTP 403; fixture tests do not establish coverage.

## Long conversations

Implement separate byte and estimated-token input guards; preserve the 4,000-character
message contract and existing spend caps. Keep full recent turns, compact older turns
into a durable bounded extractive recap (source message IDs, never fabricated facts),
and provide editable chat-only notes/reset. These notes never write listening feedback
or the permanent taste profile. Latest text and confirmed filters override old excerpts.
Index historical cards per account/chat for title/artist and first-selection retrieval.
Serve cursor-based 20-message pages; the browser displays one bounded page and can
navigate older/newer pages. Internal recommendation exclusions cover all indexed cards,
not just the visible page. Backfill indexes/recaps lazily for existing databases.
Validate long dialogues, corrections, resets, restart, account isolation, old cards,
pagination, Cyrillic input and spend guards without paid model calls.
