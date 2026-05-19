# Current Iteration

## Iteration 1: Settings Export/Import

Цель: сделать переносимость runtime-настроек SQLite, чтобы Telegram-admin mode не привязывал настройки к одной машине.

## Scope

- Добавить CLI `--export-settings <path>`.
- Добавить CLI `--import-settings <path>`.
- Добавить `--replace` для import settings с явной заменой routing/settings таблиц.
- Экспортировать только настройки:
  - admins;
  - source groups;
  - chats;
  - chat groups;
  - chat source subscriptions.
- Не экспортировать runtime/history state:
  - `posted_items`;
  - `deliveries`;
  - `summary_queue`;
  - `ai_summaries`;
  - `source_state`.
- Добавить validation path: импорт должен сначала прочитать YAML, сверить `source_id` с доступными source ids и только потом писать в SQLite.
- Обновить docs с backup/restore сценариями.

## Acceptance Checks

- `python bot.py --validate-config`
- `python bot.py --export-settings data/settings-export.test.yaml`
- `python bot.py --import-settings data/settings-export.test.yaml --replace --db data/settings-import.test.sqlite3`
- `python bot.py --validate-config --db data/settings-import.test.sqlite3 --migrate-db`
- Проверить git diff и удалить временные test artifacts перед коммитом.

## Out Of Scope

- Перенос источников в SQLite.
- Telegram-команды `/sources`, `/admins`, `/chats`.
- Pending-flow для source/chat.
- Изменение provider/model ключей и `ai-summary-models.local.yaml`.
