# AI Summary

Бот умеет добавлять короткую строку `<b>Кратко:</b> ...` к instant-сообщениям и, если включено, к digest entries.

## Настройки

```env
AI_SUMMARY_ENABLED=false
AI_SUMMARY_MODELS_CONFIG=ai-summary-models.local.yaml
AI_SUMMARY_API_BASE=https://opencode.ai/zen/v1
AI_SUMMARY_API_KEY=
AI_SUMMARY_MODEL=minimax-m2.5-free
AI_SUMMARY_TARGET_LANGUAGE=ru
AI_SUMMARY_MAX_INPUT_CHARS=6000
AI_SUMMARY_TIMEOUT_SECONDS=30
AI_SUMMARY_MAX_OUTPUT_CHARS=440
AI_SUMMARY_MAX_TOKENS=10000
AI_SUMMARY_DRY_RUN_CALL_API=false
AI_SUMMARY_IN_DIGEST=true
GOOGLE_API_KEY=
OPENROUTER_API_KEY=
OLLAMA_API_KEY=
```

По умолчанию AI summary выключены. Если `AI_SUMMARY_MODELS_CONFIG` задан, бот берёт провайдеры, лимиты и ordered fallback-список моделей из YAML. Если YAML не задан, остаётся legacy-режим через `AI_SUMMARY_API_BASE` + `AI_SUMMARY_MODEL`.

Runtime-конфиг моделей создаётся из example:

```bash
cp ai-summary-models.example.yaml ai-summary-models.local.yaml
```

В `ai-summary-models.local.yaml` можно менять порядок `models`, удалять модели или добавлять новые. Бот пробует модели сверху вниз: первая успешная русскоязычная summary сохраняется в cache и используется в сообщении. Ошибки API, rate limit, пустой ответ, reasoning-only ответ или ответ не на целевом языке переводят генерацию к следующей модели.

Текущий рекомендуемый порядок:

| Приоритет | Модель | Провайдер / маршрут | Роль |
|---:|---|---|---|
| 1 | `google-gemini-2-5-flash-lite` | Google | Primary, если Google-лимиты не мешают. |
| 2 | `ollama-devstral-small-2-24b` | Ollama | Лучший Ollama primary. |
| 3 | `ollama-devstral-2-123b` | Ollama | Fallback с похожим качеством. |
| 4 | `openrouter-openai-gpt-oss-20b-free` | OpenRouter / OpenAI | Внешний fallback. |
| 5 | `ollama-qwen3-coder-next` | Ollama | Fallback для технических changelog. |
| 6 | `ollama-qwen3-vl-235b-instruct` | Ollama | Fallback, если доступен. |
| 7 | `ollama-ministral-3-14b` | Ollama | Более подробный fallback. |
| 8 | `ollama-ministral-3-8b` | Ollama | Максимум деталей, не основной Telegram-режим. |

## Cache

Результат сохраняется в SQLite таблицу `ai_summaries` по ключу:

```text
source_id + item_id + model + target_language
```

Если все модели недоступны или ключи пустые, бот отправляет обычное сообщение без AI summary и продолжает работу.

## Dry-Run

Dry-run может читать существующий cache, но по умолчанию не вызывает API и не записывает новые summaries:

```env
AI_SUMMARY_DRY_RUN_CALL_API=false
```

Если нужно явно протестировать API в dry-run:

```env
AI_SUMMARY_DRY_RUN_CALL_API=true
```

Даже в этом режиме dry-run не сохраняет новые AI summaries в SQLite.

## Digest

AI summaries в digest управляются отдельно:

```env
AI_SUMMARY_IN_DIGEST=true
```

Если поставить `false`, AI summary останутся только в instant posts. Это снижает риск медленной scheduled digest отправки, если AI API отвечает долго.

## Model Comparison

Для сравнения нескольких моделей есть отдельный скрипт:

```bash
cp scripts/model-summary-compare.example.yaml scripts/model-summary-compare.local.yaml
python scripts/compare-model-summaries.py --models-config scripts/model-summary-compare.local.yaml --limit 5
```

Скрипт берёт recent release ids из SQLite (`posted_items`, либо `deliveries` при `chat_id`), восстанавливает release notes через источники из `products.yaml`, вызывает модели из отдельного YAML-конфига и пишет Markdown-таблицу. `*.local.yaml` игнорируется git, поэтому ключи держи там или через `api_key_env`.

Конфиг поддерживает `providers`: модель может указать `provider: opencode-zen` и унаследовать `api_base`, `auth_type`, `api_key_env`/`api_key`, `rpm`, `models_path`, `chat_completions_path` и retry-настройки. Любое из этих полей можно переопределить на уровне конкретной модели. `concurrent_models` по умолчанию равен `1`; его можно переопределить в YAML или через `--concurrent-models`.

Поддержанные типы авторизации: `bearer`, `api-key`, `query-key`, `none`. В example config уже есть провайдеры `opencode-zen`, `google`, `openrouter`, `ollama`. Команда `--list-provider-models` выводит id моделей и помечает `FREE`/`LOCAL`, когда это можно определить из ответа провайдера или имени модели.

Для устойчивого отсеивания моделей, которые не подходят для chat/completions, часто rate-limitятся или временно отваливаются, используется локальный файл решений `data/model-decisions.yaml`. Он не коммитится, потому что это runtime-результат экспериментов. Создать шаблон можно так:

```bash
cp scripts/model-decisions.example.yaml data/model-decisions.yaml
```

Формат:

```yaml
models:
  google:deep-research-preview-04-2026:
    action: skip
    reason: google_interactions_api_only
    note: Requires Interactions API, not chat/completions.
  openrouter:some-model:
    action: retry_later
    reason: rate_limit_observed
```

`action: skip` и `action: retry_later` скрывают модель из списков и UI по умолчанию. Чтобы увидеть скрытые модели, используй `--include-hidden-models`.

Статусы в Markdown-таблице сравнения:

| Статус | Значение |
|---|---|
| `🔵 Active/Success` | Модель вернула пригодный ответ. |
| `⚠️ Rate Limit` | Модель упёрлась в RPM/quota/concurrent/rate-limit; можно повторить позже или снизить concurrency. |
| `🟡 Warning/Caution` | Временная или условная проблема без полного исключения модели: high demand, credits/subscription, empty/reasoning-only. |
| `🔴 Error/Danger` | Остальные ошибки, обычно требующие исключения модели или отдельного адаптера. |

Полезные режимы:

```bash
python scripts/compare-model-summaries.py --list-items --limit 10
python scripts/compare-model-summaries.py --list-provider-models --models-config scripts/model-summary-compare.local.yaml
python scripts/compare-model-summaries.py --refresh-model-lists --models-config scripts/model-summary-compare.local.yaml
python scripts/compare-model-summaries.py --dry-run --models-config scripts/model-summary-compare.local.yaml
python scripts/compare-model-summaries.py --item opencode_changelog:v1.15.5 --model zen-minimax-free
```

Локальная web-админка читает сохранённые model lists из `data/model-lists/`, позволяет выбрать релизы и модели, запускает comparison job в фоне и показывает прогресс, логи и Markdown-результат:

```bash
python scripts/model-summary-admin.py --models-config scripts/model-summary-compare.local.yaml
```

По умолчанию UI доступен на `http://127.0.0.1:8765`. Jobs и результаты пишутся в `data/model-summary-admin/`.
