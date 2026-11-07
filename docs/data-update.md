# Добавление реальных источников, 2026-10-28

Загружены MusicBrainz и небольшой дополнительный набор Wikidata. Позже получены реальные исторические выгрузки RYM/AOTY через GitHub и один ответ публичного AOTY API из кеша. MusicBrainz и исторические выгрузки теперь включены в [единый каталог приложения](unified-catalog.md): 9 903 релиза. Wikidata и диагностический ответ API сохранены отдельно. Личные обучающие оценки не создавались. Ниже описан исходный этап загрузки MusicBrainz/Wikidata.

| Источник | Реально сохранено | Использование |
|---|---|---|
| MusicBrainz | 520 канонических релизов, 409 credited artists, годы 1988–2026 | Каталог приложения, жанры, музыкальные ориентиры, общественные ratings/counts |
| Wikidata | 10 проверенных записей по release-group и artist MBIDs; пять с жанровыми признаками | Дополнительные признаки в отдельном файле и совместном JSONL; без автоматической подмены жанров |
| RYM / AOTY | Исторические выгрузки: 5 000 / 6 703 строки; один реальный cached AOTY API response | [Нормализованные исследовательские наборы](community-data.md); capture time неизвестен, автоматического слияния с каталогом нет |
| ListenBrainz MLHD+ | Не импортирован | Проверенный endpoint вернул серверную ошибку; исторический набор не равен свежей популярности |

В hip-hop scope входят 463 релиза. Покрытие поджанров пересекается: experimental 101, abstract 72, industrial 65, hardcore 92, jazz rap 60, boom bap 68, trap 45, instrumental 57. Все пять любимых исполнителей присутствуют; Ye сопоставлен с Kanye West по точному псевдониму и идентификатору.

У 109 релизов есть средняя общественная оценка и положительное число голосов. У остальных 411 источник явно сообщает ноль голосов; средняя оценка остаётся неизвестной. Никакие search scores, числа изданий или вымышленные значения не использованы вместо рейтингов.

**Известность пока измеряется недостаточно хорошо.** В текущем снимке строгий режим возвращает ноль кандидатов: консервативный percentile не может различить большую группу одинаковых нулей. G0 остаётся `limited` также из-за отсутствия независимой жанровой проверки. При этом обычные close/explore/surprise выдачи с пятью seed artists дают по пять результатов; это техническая проверка, не доказательство музыкальной полезности.

## Файлы

- `data/catalogs/musicbrainz-expanded.json` — нормализованный каталог, `mb-tags-v2`.
- `data/manifests/musicbrainz-expanded.json` — источники, queries, capture timestamps и ограничения.
- `data/catalogs/musicbrainz-expanded.import-report.json` — исходные теги, сопоставления, исключения; 37 неоднозначных `trap` тегов не подтверждают hip-hop.
- `data/catalogs/musicbrainz-expanded.audit.json` — покрытие и незакрытые требования.
- `data/expanded/musicbrainz/raw/` — сохранённые официальные API responses с параметрами и датами.
- `data/wikidata/expanded/dataset.json` — отдельный набор внешних признаков. Только 27 из 520 релизов входили в исходный Wikidata query, 12 совпали по релизу, 10 прошли проверку исполнителей; две неоднозначные записи изолированы. Повторного запроса для остальных 493 не было.
- `data/training/musicbrainz-wikidata-expanded/releases.jsonl` и `manifest.json` — 520 строк совместных метаданных с контрольными суммами и без personal labels.

Исходные лицензии и атрибуция сохранены: MusicBrainz core CC0, supplementary tags/ratings CC BY-NC-SA 3.0; Wikidata structured data CC0. Совместная упаковка не снимает условий MusicBrainz. [MusicBrainz attribution](../data/expanded/README.md), [Wikidata provenance](additional-dataset.md), [проверка RYM/AOTY](rym-aoty-access.md).

## Воспроизведение

```bash
uv run python -m discovery.catalog.collect_musicbrainz --config data/expanded/musicbrainz-config.json --offline
uv run python -m discovery.catalog.audit --manifest data/manifests/musicbrainz-expanded.json --as-of 2026-10-28
uv run python -m discovery.catalog.dataset --catalog data/manifests/musicbrainz-expanded.json --enrichment data/wikidata/expanded/dataset.json --output-dir artifacts/catalog-export
```

Export требует новый выходной каталог. Offline collector использует сохранённые ответы и останавливается при отсутствии записи в кеше. Повторное получение актуальных counts требует нового workspace в копии конфигурации: существующий кеш предназначен для resume, не refresh.

Offline reconstruction выполнена отдельно от опубликованного снимка: 0 сетевых запросов, побайтовое совпадение каталога. SHA-256 файла: `eb5b5bbd44186a6bf80b06e07cd1291bc6b8f629ecb3d59400c145a0ec946bb9`. Canonical model hash: `1d66f9d309e0d760ca601f24e829ebaeeeeb8c11a23c24ffa3f2a74f267602f4`; эти два hash используют разные представления JSON.

На этапе интеграции MusicBrainz/Wikidata: **156 tests passed, 8/8 сценариев, Ruff clean**. Включены проверки сохранения дат кеша, работы offline без сети, атомарной публикации, неоднозначного trap, совместимости snapshot, подтверждения Wikidata features исходными claims и стартового UI со всеми пятью исполнителями. Актуальный общий прогон после добавления HTML-парсеров — в [статусе реализации](implementation-status.md).

Для обучения личному вкусу по-прежнему нужен `export-training` из истории прослушиваний. Совместный metadata JSONL не является входом этого supervised CLI. При использовании старых оценок из исходного каталога `mb-tags-v1` требуется отдельный совместимый запуск или явная миграция; смешивание версий автоматически отвергается.
