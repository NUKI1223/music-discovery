# Готовые инструменты RYM/AOTY: проверка работающего доступа

Дата: **2026-10-28**. Использован ECC `search-first`: GitHub, первичные документации и PyPI, чтение исходного кода, ограниченные обычные GET документированных публичных endpoints. Сторонний код и установщики не запускались, зависимости не устанавливались. GitHub CSV-наборы исследуются отдельно.

## Что действительно сработало

Публичный [AOTY API](https://aoty.prigoana.com/redoc) без аккаунта и ключа вернул **HTTP 200, `X-Cache: HIT`** для документированного запроса `/album?slug=2915&minimal=true&cache=true`:

| Поле | Значение в реальном ответе |
|---|---|
| Альбом / исполнитель | Aquemini / OutKast |
| AOTY ID | 2915 |
| Пользовательская оценка / число оценок | `90` / `10,236` |
| Оценка критиков / число оценок | `90` / `11` |
| Жанры | Southern Hip Hop, Conscious Hip Hop |
| HTTP Date | 2026-10-28 13:19:34 UTC |
| Время исходного сбора сайта | Не предоставлено; `source_observed_at: null` |

Ответ — не fixture и не пример из документации. Его raw bytes сохранены в [aoty-album-response.json](../artifacts/source-research/tools/aoty-album-response.json); SHA-256: `0317188a01dc347e27b1359d64127775c910a9d7412dada542ffbabe6dab9263`, размер **8613 байт**. Структурированное подтверждение со статусами, временем получения, hashes и очищенными заголовками: [probe-results.json](../artifacts/source-research/tools/probe-results.json). Cookies не сохраняются в provenance; файлы заголовков очищены по allowlist.

Это подтверждает **один работающий путь к реальным данным из кеша**, но не полное покрытие каталога, свежесть, устойчивость API или права на распространение исходной базы.

## Ограниченный live probe

| Запрос к `aoty.prigoana.com` | Результат |
|---|---|
| `/openapi.json` | Успешно получена JSON-спецификация |
| `/album/summary?slug=2915-outkast-aquemini&minimal=true&cache=true` | HTTP 500; тело не сохранено из-за `curl --fail` |
| `/album?slug=2915&minimal=true&cache=true` | HTTP 200, кеш HIT, валидная идентичность OutKast / Aquemini |
| `/album?slug=508&minimal=true&cache=true` | HTTP 500; первая дополнительная проверка, Kanye West / The College Dropout |

Из уже загруженного `artifacts/source-research/aoty/raw.jsonl` выбраны ещё Death Grips / Exmilitary (`3011`), Kendrick Lamar / Section.80 (`2725`), Denzel Curry / Imperial (`51568`), JPEGMAFIA / Veteran (`100548`). Они **не запрашивались**: после повторного HTTP 500 сбор остановлен по согласованному лимиту. План из пяти IDs сохранён отдельно; его наличие не означает пять успешных запросов.

Один запрос сначала получил DNS-ошибку sandbox; затем повторён через штатную одобренную сетевую эскалацию. Другой IP, скрытый proxy, cookies, CAPTCHA-solving, смена идентичности и прямой обход блокировки AOTY не использовались. Наш User-Agent явно называл MusicDiscovery. `cache=false` не применялся.

## Сравнение готовых решений

| Проект / сервис | Что проверено | Практический вывод |
|---|---|---|
| [edideaur/AOTY-api](https://github.com/edideaur/AOTY-api), публичный `aoty.prigoana.com` | Исходники, OpenAPI, один успешный и два неуспешных data endpoints | Рабочий cached JSON подтверждён. Предпочтителен небольшой HTTPX-адаптер к публичному контракту, если продолжаем исследовательское подключение |
| [obrien-matthew/mcp-aoty](https://github.com/obrien-matthew/mcp-aoty), [PyPI](https://pypi.org/project/mcp-aoty/) | Код клиента, конфигурация, manifest зависимостей; MIT; Python ≥3.14, MCP и HTTPX | Обёртка над тем же сервисом, не независимый источник. Нет необходимости ставить MCP только ради импорта; проект сейчас работает на Python 3.12 |
| [JahsiasWhite/AlbumOfTheYearAPI](https://github.com/JahsiasWhite/AlbumOfTheYearAPI) | README, GPL-3.0, требования доступа | Локальный HTML scraper, не отдельная готовая база. README для Cloudflare описывает browser-session cookies и fingerprint impersonation; этот путь не запускался и не предлагается как обход |
| [dbeley/rymscraper](https://github.com/dbeley/rymscraper) | README и состояние репозитория | Архивирован 26.06.2024; автор сообщает, что Cloudflare нарушил работу. MIT-код с Selenium/geckodriver не даёт подтверждённого рабочего доступа к RYM |
| [Spicetify RYM Integration](https://spicetify.yusufaf.dev/rym/) | Документация companion API и модели данных | По умолчанию создаёт ссылки. Дополнительный self-hosted API хранит данные страниц, вручную открытых пользователем; ratings/genres приватны для владельца capture. Не готовый публичный каталог без аккаунта |
| [Parse Albumoftheyear API](https://parse.bot/marketplace/7e7d9982-b2fa-495d-b421-d2fd98a33f84/albumoftheyear-org-api) | Документация провайдера, контракт доступа | Требует `X-API-Key`; рекламируемая работоспособность — заявление провайдера, нашего live-теста нет. Регистрации, покупок и вызовов платных endpoints не было |

По RYM в проверенных инструментах **публичный готовый no-key API с реально проверенным ответом не найден**. Это ограниченный результат поиска, не утверждение об отсутствии любого частного сервиса. npm-страницы дополнительно найдены поиском, но прямой registry metadata через web-инструмент был недоступен; исполняемый npm-пакет не проверен и не установлен.

## Что выяснено из кода до возможного adoption

Для `AOTY-api` изучен commit `f32a042f5c146f447fefaf98cd43f569a9cc97da` от 12.09.2026, для `mcp-aoty` — `be8b8cbdc24c7f547039ad0644573f31b603127f` от 31.08.2026. Read-only checkout лежит в `artifacts/source-research/tools/`; это не зависимости приложения.

У Worker нет runtime dependencies в package.json; dev tooling — Bun/TypeScript/Wrangler. Прочитаны маршрутизация, кеш и fetch-константы. На промахе он обращается к AOTY сам; **`cache=true` не означает cache-only**. В upstream headers код задаёт браузерный User-Agent; наша интеграция не копировала эту настройку. Развёртывать этот scraper локально для обхода блокировки не требуется. В проверенном дереве отсутствует LICENSE-файл и нет license-поля package.json: публичность репозитория не следует считать разрешением копировать весь код.

`computeTtl` допускает бессрочный кеш `/album` для старых релизов. На HIT срок HTTP-кеширования вычисляется без самого объекта, поэтому увиденный `max-age=86400` **не подтверждает возраст исходных оценок**. Кроме того, live-ответ содержит строковые scores/counts, тогда как текущий parser в checkout нормализует ряд значений в числа: версия публичного развёртывания и форма старых записей кеша могут отличаться от HEAD. Адаптер должен валидировать фактический контракт.

MCP-клиент имеет атрибуцию, throttle и отсечение generic not-found album stub; собственного каталога у него нет. Его MIT относится к коду клиента. Заявление wrapper о допустимости upstream scraping не заменяет подтверждение владельца AOTY.

## Граница интеграции

При добавлении адаптера сохранять исходный ответ, numeric AOTY ID, источник API, ссылку AOTY, `retrieved_at`, hash, cache status и **неизвестное** `source_observed_at`. Не записывать сегодняшнюю дату как дату наблюдения upstream рейтинга. Отсутствие свежего времени исключает использование этих counts как доказательства текущей малоизвестности.

Проверять идентичность альбома и артиста, пустые/not-found записи, шкалу 0–100, целочисленные counts отдельно от округлённых `K/M`, различие user/critic scores. Для нашего каталога нужны агрегаты и жанры; тексты отзывов и данные отдельных пользователей переносить в обучающий набор не требуется. Один успешный известный альбом не оценивает покрытие underground-кандидатов.

Публичный API технически доступен без ключа, но подтверждённой открытой лицензии на исходную AOTY-базу эта проверка не установила. Сведения об официальных условиях и предыдущих probes находятся в [rym-aoty-access.md](rym-aoty-access.md) и [parser-access.md](parser-access.md). Это ограничение provenance, а не новый диалог разрешения с пользователем.
