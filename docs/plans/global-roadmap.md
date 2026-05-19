# Global Roadmap

## Принцип Хранения

SQLite становится основным runtime source-of-truth для настроек, которые меняются из Telegram:

- админы и их alias;
- чаты/получатели и их alias;
- группы источников;
- источники changelog/release;
- подписки `chat -> source/group`;
- pending-заявки и staging добавления источников;
- audit-log административных действий.

YAML-конфиги остаются seed/backup/import/export слоем:

- `products.yaml` - первичный seed источников и человекочитаемый пример;
- `admin-routing.yaml` - первичный seed админов, чатов, групп и подписок;
- export/import YAML - перенос runtime-настроек между инстансами без истории публикаций.

Локально и вне Telegram остаются только секреты и AI-модельные настройки:

- `.env`;
- API-ключи провайдеров;
- `ai-summary-models.local.yaml`;
- порядок моделей, лимиты и provider/model fallback.

## Итерации

1. Planning docs and safety baseline.
   Зафиксировать roadmap/current/done/backlog, чтобы последующие итерации были коммитами с проверяемой целью.
2. Settings export/import.
   Добавить YAML export/import для runtime routing settings из SQLite без `posted_items`, `deliveries`, `summary_queue`, `ai_summaries`, `source_state`.
3. Runtime sources in SQLite.
   Добавить таблицы источников, импорт seed из `products.yaml`, загрузку источников из SQLite и сохранение совместимости CLI validation/dry-run.
4. Telegram read/list commands.
   Добавить `/help`, `/id`, `/admins`, `/chats`/`/contacts`, `/sources`/`/projects`, `/source`/`/info`.
5. Telegram admin/chat management.
   Добавить `/addadmin`, `/removeadmin`, `/requestchat`, `/addme`, `/pending`, `/approvechat`, `/rejectchat`, `/addchat_here`, `/removechat`, `/enablechat`, `/disablechat`.
6. Telegram source management with staging.
   Добавить `/testsource`, `/addrepo`, `/addsource`, `/confirmsource`, `/enablesource`, `/disablesource`, `/removesource`; перед включением source обязательно network-check и preview.
7. Subscription aliases and operational commands.
   Закрепить `/link`/`/unlink` как alias для `/subscribe`/`/unsubscribe`, добавить `/checknow` при необходимости, обновить UX help.
8. Hardening and docs.
   Добавить audit-log, edge-case tests/checks, документацию сценариев миграции/backup/restore и финальные smoke checks.

## Правила Безопасности

- Не записывать provider/model keys через Telegram.
- Не сохранять непроверенный source в active runtime state.
- Перед добавлением чата проверять Telegram access через `getChat`/`getChatMember`.
- Перед добавлением source проверять парсинг и показывать preview последних 1-3 entries.
- Импорт настроек должен иметь dry-run/validation path и режим `--replace` только явно.
