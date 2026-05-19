# Current Iteration

## Iteration 2: Runtime Sources In SQLite

Цель: перенести source definitions из `products.yaml` в SQLite runtime state, сохранив YAML как seed/backup/import/export слой.

## Scope

- Спроектировать SQLite tables для runtime sources.
- Добавить seed/import path из `products.yaml` без потери history state.
- Обновить validation так, чтобы source ids читались из runtime DB.
- Сохранить YAML backup/restore сценарий для settings.

## Out Of Scope

- Telegram-команды `/sources`, `/admins`, `/chats`.
- Pending-flow для source/chat.
- Изменение provider/model ключей и `ai-summary-models.local.yaml`.
