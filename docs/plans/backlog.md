# Backlog

Здесь лежат задачи без жесткой привязки к текущей итерации.

## Telegram UX

- Добавить reply keyboard или inline keyboard после стабилизации command-only UX.
- Добавить пагинацию длинных списков `/sources`, `/chats`, `/pending`.
- Добавить `/checknow <source_id|all>` после стабилизации source management.
- Добавить `/audit [limit]` для просмотра последних admin-действий.
- Добавить `/status` с кратким runtime состоянием: interval, enabled chats, enabled sources, pending counts.

## Safety And Ops

- Добавить audit-log для всех mutating Telegram-команд.
- Добавить dry-run import report без записи в SQLite.
- Добавить backup rotation для settings export.
- Добавить защиту от удаления последнего admin.
- Добавить защиту от удаления source, на который есть активные подписки, без явного `--force`/подтверждения.
- Добавить лимиты на размер Telegram-ответов и автоматическое разбиение сообщений.

## Data Model

- Перенести `products.yaml` sources в SQLite runtime table.
- Разделить source groups и explicit source subscriptions в admin UI.
- Хранить source validation preview в pending table с TTL.
- Хранить chat metadata: title, username, type, last_seen_at.

## Documentation

- Документировать migration path: legacy YAML seed -> SQLite runtime -> export/import YAML.
- Документировать восстановление нового инстанса из `settings.yaml` + `.env` + AI model local config.
- Документировать Telegram command reference.

## Later

- Web-admin UI поверх SQLite.
- Import/export через Telegram document upload, если появится безопасный validation/confirmation flow.
- Локализация команд после стабилизации структуры.
