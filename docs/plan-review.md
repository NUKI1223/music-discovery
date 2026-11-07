# Независимое ревью плана

2026-10-28. Ниже первоначальное ревью версии 0.2 и дополнительная проверка многожанрового scope версии 0.3 [плана](../plans/implementation.md).

Работали три реальных subagents Codex параллельно; координатор собрал план и внёс исправления. Ни один implementation step не выполнялся. Ревью спецификации не является тестированием приложения или подтверждением ML-качества.

| Агент | Направление | Первый проход | Повторная проверка |
|---|---|---|---|
| architecture_data (Euler) | Архитектура и источники | 2 major | Оба закрыты; новых critical не обнаружено |
| ml_rl_design (Bacon) | ML, новизна и RL | 4 major | Все закрыты; новых critical/major в изменённых разделах нет |
| harness_evals (Singer) | Harness, evals и приёмка | 4 major | Все закрыты; новых critical не обнаружено |

Замечание по familiarity совпало у двух агентов: всего исправлены 9 уникальных существенных проблем.

1. Строгая малоизвестность теперь имеет отдельный hard filter, cutoff, cohort size и свежесть.
2. G0 проверяет coverage по направлениям и полный snapshot, а не только маленький sample.
3. Cold start определяет weak artist anchors, confidence и fallback без тегов.
4. Музыкальная новизна отделена от exploration epsilon; состав первых четырёх позиций согласован с пятым слотом.
5. Sampling выполняется один раз; агент не выбирает лучшую из нескольких случайных выдач, OPE ограничена фиксированным prefix/pool.
6. Familiarity до рекомендации записывается явно и не переписывается после прослушивания.
7. Engineering readiness отделена от evidence readiness; ожидание пользовательских оценок не блокирует техническую приёмку MVP.
8. HTTP attempts включают retries/repair, неизвестный расход после timeout сохраняет резерв.
9. Claims проверяются как field/operator/value/evidence; существование evidence ID не считается доказательством произвольного текста.

Дополнительно: feedback available_at, delayed-return counterexample в RL, статистика already-known в отчёте, typed errors и idempotency закреплены в плане.

Архитектурный выбор: агент Data предложил FastAPI/React. Координатор выбрал Streamlit/SQLite для локального одного пользователя; после ревью Data согласился, что это не создаёт blocker при модульном ядре и idempotent UI. Отдельный web stack остаётся возможным последующим изменением.

Открытые зависимости: получение разрешённого каталога и показателя известности, реальные личные оценки, выбор модели/бюджета и live-проверка OpenRouter. Они не закрыты ревью.

Применённые навыки ECC: blueprint, parallel-execution-optimizer, recsys-pipeline-architect, api-design, ml-adoption-playbook, mle-workflow, agent-harness-construction, eval-harness и релевантные разделы security-review. plan-orchestrate/dev-team были прочитаны для выбора процесса, но не запускались как workflow: использован прямой native multi-agent dispatch.

## v0.3 — Многожанровая модель, hip-hop как первый пилот

Пользователь уточнил исходное требование: модель предназначена для разных жанров, хип-хоп — только первый этап обучения. Прежнее ограничение продукта исправлено, рабочее название изменено на Music Discovery. Путь проекта сохранён для совместимости существующих ссылок.

Data/Architecture и ML/RL агенты параллельно предложили требования и повторно проверили итоговый v0.3. Оба вернули PASS без major/critical в проверенной области.

Проверено: динамический genre registry и taxonomy, crossover identity, один genre scope на запрос MVP, предпочтения в контексте жанра, rarity cohorts внутри scope, общая genre-conditioned модель, artifact trained/validated scopes, baseline нового жанра, полное переобучение на истории нескольких жанров и per-genre regression/evidence gates. Второй жанр включён в contract fixtures и acceptance уже первой версии. Parser/UI не закрепляют hip-hop в коде.

Это целевое ревью изменения scope, а не повторный полный аудит harness. Функциональность приложения по-прежнему не реализована; реальная полезность модели вне первого пилота не заявляется.
