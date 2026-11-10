# ML, bandit и последовательный RL

Команды выполняются из корня проекта после `uv sync --locked`. Они не вызывают OpenRouter и не требуют API-ключа. Механика работает локально; реальные личные оценки пока не накоплены. Обученная и проверенная модель вкуса пользователя отсутствует. Тесты на искусственных данных не доказывают пользу рекомендаций.

## 1. Данные и baseline

```bash
uv run discovery --db runtime/discovery.sqlite3 recommend \
  --catalog data/demo/catalog.json --genre hip-hop --mode explore
```

`data/demo/catalog.json` — вымышленные релизы для проверки приложения. Для этого каталога приложение использует отдельную БД `runtime/discovery-demo.sqlite3`. Demo-оценки не смешиваются с реальной историей.

Baseline работает без ML: жанровые фильтры → признаки и сходство → режим новизны → разнообразие исполнителей. Отсутствие числа голосов не означает редкость. `--obscurity require` может вернуть пустой или короткий результат при недостаточных данных. Известность оценивается относительно конкретного источника и локальной сравнительной группы.

В UI можно выбрать реальный каталог, указать любимых исполнителей и оценивать прослушанное. `known` сообщает знакомство, `save_for_later` — интерес; `listened_mixed` означает «прослушал, впечатления смешанные» и снимает прежнюю метку релиза; только `listened_like` и `listened_dislike` становятся обучающими метками, но не те, где пользователь отметил, что знал релиз до рекомендации (`familiar_before_recommendation=true`, `known` или отдельное событие `familiarity`): модель предсказывает, понравится ли новое. Знакомство до рекомендации спрашивается отдельно. Выбор нового жанра без собственных данных использует baseline, а не автоматически переносит hip-hop предпочтения.

## 2. Экспорт и supervised learning

Проверка поведения без данных:

```bash
uv run python -m discovery.training.train --config configs/supervised.json
```

Ожидаемый статус — `insufficient_data`, `artifact: null`. Это нормальный результат, не ошибка установки.

Перед обучением зафиксируй конфигурацию с версией taxonomy именно того каталога, на котором получены оценки. Базовый `configs/supervised.json` настроен на реальный расширенный MusicBrainz pilot с `mb-tags-v2`; у demo версия `demo-v1`. Следующая команда создаёт конфиг из выбранного каталога; для demo замени путь на `data/demo/catalog.json`:

```bash
uv run python - <<'PY'
import json
from pathlib import Path

config = json.loads(Path("configs/supervised.json").read_text())
catalog = json.loads(Path("data/catalogs/musicbrainz-expanded.json").read_text())
config["taxonomy_version"] = catalog["taxonomy_version"]
Path("artifacts").mkdir(exist_ok=True)
Path("artifacts/supervised-run.json").write_text(json.dumps(config, indent=2))
PY
```

Training export сохраняет `taxonomy_version` и `feature_version` каждой записи. Несовместимость хотя бы одной записи с config останавливает обучение с ошибкой; записи не отбрасываются молча. Для другой версии нужен отдельный matching dataset/config или явная миграция признаков. Matching version также проверяется при serving; формальная замена строки версии в готовом artifact не является миграцией.

После реальных прослушиваний:

```bash
uv run discovery --db runtime/discovery.sqlite3 export-training \
  --output artifacts/training-records.json
uv run python -m discovery.training.train \
  --config artifacts/supervised-run.json \
  --data artifacts/training-records.json \
  --output artifacts/training-report.json
```

Экспорт не перезаписывает существующий файл: для нового снимка выбери новое имя. Для демонстрационных оценок явно указывай `--db runtime/discovery-demo.sqlite3`; результат такого обучения имеет статус `fixture_only`. При нестандартном `DISCOVERY_DB` используй фактический путь БД интерфейса.

Первый ориентир — 60 явных оценок в жанре, включая минимум 15 оценок меньшего класса. Это технический порог, не гарантия точности. Данные каждого релиза остаются в одной части временного split; `available_at` ограничивает доступность отложенных меток. Векторизация и масштабирование обучаются только на train. Признаки берутся из сохранённого решения, а не из сегодняшнего профиля.

`training-report.json` содержит статус, размеры train/validation/test, per-genre log loss/Brier и constant-baseline сравнение. Если обучение возможно, `artifact` содержит коэффициенты и preprocessing metadata в JSON, без pickle. Значение `validated_genre_ids` после обучения всегда пустое. Результат на реальных метках называется `experimental`, пока нет отдельного решения о продвижении.

Для повторяемого опыта добавь фиксированный ISO timestamp `cutoff` в копию JSON-конфига. Для нового жанра добавляются данные в тот же pipeline, затем общая модель переобучается на всей доступной train-истории. Увеличение общей выборки не заменяет проверку каждого жанра. Старые жанры проверяются на регрессии; предыдущий artifact сохраняется для отката.

## 3. Продвижение и serving

Обучение автоматически не включает персональную модель. Отдельная функция `promote_artifact(artifact, evidence, previous_artifact=...)` проверяет:

- `is_fixture=false` у artifact и evidence;
- точное соответствие `artifact_hash` и `dataset_hash`;
- `protocol="frozen_temporal_holdout"`, явное `decision="promote"`;
- для каждого продвигаемого жанра: `holdout_snapshot_id`, минимум 20 отложенных меток и по 5 каждого класса, корректные `n/positive/negative`, ноль `genre_violations`;
- `model_log_loss < baseline_log_loss` и `model_brier <= baseline_brier`;
- охват всех ранее подтверждённых жанров, если передан `previous_artifact`;
- если в evidence указаны `release_ids` отложенной выборки, они не пересекаются с `training_release_ids` модели.

`artifact_hash` — идентичность модели и не включает `validated_genre_ids`/`promotion_evidence` (evidence ссылается на этот хеш). Поэтому `promote_artifact` добавляет `promotion_seal` — хеш от `artifact_hash`, `validated_genre_ids` и evidence. При загрузке для serving seal обязателен, а `validated_genre_ids` должны совпадать с жанрами evidence; иначе используется baseline. Seal защищает от правки области или отчёта после продвижения, но не от того, кто пересобрал весь файл; это целостность, а не подпись. Ранее продвинутые артефакты без seal работают как baseline, пока не продвинуты заново.

Продвинутая модель не заменяет формулу: `score = baseline_score + MODEL_WEIGHT·(p − 0.5)`, `MODEL_WEIGHT = 0.3`. При `p = 0.5` порядок совпадает с baseline; качество, сходство, штрафы жанра и целевая новизна режима («Неожиданнее») сохраняются. В компонентах остаются `model_probability` и `baseline_score`. Ранжировщик — `baseline-v7-artist-peak-popularity`.

Evidence — отдельный отчёт оценки, а не ручная подмена `validated_genre_ids`. Сам по себе этот gate подтверждает только указанное offline-сравнение. Пользу после реальных прослушиваний проверяют отдельно с учётом неполной обратной связи. Текущий training report нельзя без такой проверки объявить доказательством улучшения относительно полноценной рекомендательной системы.

После подготовки настоящего отчёта можно сохранить новый artifact через Python API:

```python
import json
from pathlib import Path
from discovery.training import load_artifact, promote_artifact

artifact = load_artifact("artifacts/training-report.json")
evidence = json.loads(Path("artifacts/promotion-evidence.json").read_text())
promoted = promote_artifact(artifact, evidence)
Path("artifacts/model-approved.json").write_text(json.dumps(promoted, indent=2))
```

Файл `promotion-evidence.json` не поставляется с вымышленными результатами. Этот пример применим только после получения требуемых данных и отдельной оценки. Для обновления уже подтверждённой модели передай её в `previous_artifact`, чтобы проверить сохранение качества прежних жанров.

Опциональное подключение:

```bash
uv run discovery --db runtime/discovery.sqlite3 recommend \
  --catalog data/catalogs/musicbrainz-expanded.json --genre hip-hop \
  --model artifacts/model-approved.json
```

Для UI используется `DISCOVERY_MODEL_PATH`. Отсутствующий или повреждённый optional файл обрабатывает `load_optional_artifact(path)`: возвращает `(None, warning)`, baseline остаётся доступным. Валидный файл также должен пройти проверки genre scope, taxonomy/feature versions, real provenance и promotion evidence. При несовместимости `rank` выдаёт предупреждение и использует baseline.

Прогноз применяется до прежнего selector: жёсткие жанровые ограничения, известные релизы, условия редкости и diversity продолжают действовать. `feature_values` исключает собственные прогнозы модели из последующего training export.

## 4. Контекстный бандит

```bash
uv run discovery --db runtime/discovery-demo.sqlite3 recommend \
  --catalog data/demo/catalog.json --genre hip-hop --mode surprise --bandit
```

Это опциональный эксперимент. Для работающего bandit нужны профиль выбранного жанра, пять подходящих карточек и исследовательский пул без конфликтов с первыми четырьмя. Профиль можно сохранить через UI. Если условия не выполнены, `policy_details.enabled=false` и указан `reason`; baseline продолжает работать.

Первые четыре карточки фиксированы, меняется только пятая. Сохраняются eligible pool, context/prefix, выбранный релиз, seed и точная вероятность. При `epsilon=0.15` вероятность лучшего кандидата равна `0.85+0.15/M`, остальных — `0.15/M`. Epsilon управляет случайным исследованием, а музыкальная дистанция задаётся режимом новизны независимо.

После минимум 10 явных fifth-slot оценок обоих классов сервис может восстановить экспериментальную логистическую reward-модель из исправленной истории. Это техническая активация learner, не подтверждение его превосходства. Показ без оценки не является отрицательной наградой.

Повтор одного `--request-id` возвращает прежнее сохранённое решение; для нового самостоятельного запроса используй новый ID. LLM получает только deterministic preview и не выбирает лучший результат из нескольких случайных выдач.

`evaluate_ope` доступна как Python-функция в `discovery.experiments.bandit`. Она принимает наблюдения с `policy_details` и `reward=0|1|None`, а также target probabilities по `context_id`. Проверяются support, веса и effective sample size. Другие prefix, genre scope или candidate set требуют другого эксперимента. Missing rewards не считаются нулями; IPS/SNIPS по ответившим не исправляет самоотбор в прослушивание. Отчёт не разрешает автоматическое продвижение.

## 5. Последовательный RL

```bash
uv run python -m discovery.experiments.sequential_rl.evaluate \
  --config configs/rl.json \
  --output artifacts/sequential-rl-report.json
```

`DiscoveryEnvironment` — `gymnasium.Env` с конечными пространствами состояний/действий. Состояние содержит шаг, повторяемость знакомого направления и факт открытия нового; оба действия остаются внутри одного жанра. Исследование может дать немедленный проигрыш и улучшить последующие награды. Есть terminal bonus, поэтому задача проверяет отложенную награду, а не только мгновенную оценку.

Сравниваются Q-learning, myopic greedy, learner с `gamma=0` (contextual-bandit baseline) и случайная политика. Отчёт содержит три конфигурации слушателя, несколько training seeds, отдельные evaluation seeds, held-out listener variant, кривые обучения и разброс результатов. В сценарии без преимущества исследования сложная политика не обязана выигрывать.

Статус всегда `simulator_only`, `real_user_efficacy="not_evaluated"`. Правила симулятора заданы вручную; действия обозначают направления, а не настоящий каталог релизов. Результат не включает RL в пользовательские рекомендации и не доказывает, что музыка понравится.

## 6. Проверки

```bash
uv run pytest -q tests/ranking tests/training tests/bandit tests/sequential_rl
```

Проверяются hard filters, missing/stale popularity, новый жанр, временная доступность меток, дубли crossover-релизов, serving fallback/promotion, точные propensity, отсутствие обновлений от pending feedback, сохранность prefix и `gymnasium.utils.env_checker.check_env`. Это инженерные проверки; личная музыкальная полезность остаётся отдельным экспериментом.
