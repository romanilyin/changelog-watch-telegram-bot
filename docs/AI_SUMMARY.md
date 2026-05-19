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
