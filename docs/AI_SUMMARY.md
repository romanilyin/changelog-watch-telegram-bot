# AI Summary

Бот умеет добавлять короткую строку `<b>Кратко:</b> ...` к instant-сообщениям и, если включено, к digest entries.

## Настройки

```env
AI_SUMMARY_ENABLED=false
AI_SUMMARY_API_BASE=https://opencode.ai/zen/v1
AI_SUMMARY_API_KEY=
AI_SUMMARY_MODEL=minimax-m2.5-free
AI_SUMMARY_TARGET_LANGUAGE=ru
AI_SUMMARY_MAX_INPUT_CHARS=6000
AI_SUMMARY_TIMEOUT_SECONDS=30
AI_SUMMARY_MAX_OUTPUT_CHARS=220
AI_SUMMARY_MAX_TOKENS=1200
AI_SUMMARY_DRY_RUN_CALL_API=false
AI_SUMMARY_IN_DIGEST=true
```

По умолчанию AI summary выключены.

## Cache

Результат сохраняется в SQLite таблицу `ai_summaries` по ключу:

```text
source_id + item_id + model + target_language
```

Если OpenCode Zen не отвечает или ключ пустой, бот отправляет обычное сообщение без AI summary и продолжает работу.

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

Полезные режимы:

```bash
python scripts/compare-model-summaries.py --list-items --limit 10
python scripts/compare-model-summaries.py --list-provider-models --models-config scripts/model-summary-compare.local.yaml
python scripts/compare-model-summaries.py --dry-run --models-config scripts/model-summary-compare.local.yaml
python scripts/compare-model-summaries.py --item opencode_changelog:v1.15.5 --model zen-minimax-free
```
