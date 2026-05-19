# Current Iteration

## Iteration 3: Telegram Read/List Commands

Цель: добавить read/list Telegram-команды для просмотра runtime settings без ручного доступа к SQLite/YAML.

## Scope

- Добавить Telegram-команды `/sources`, `/admins`, `/chats`.
- Показывать краткие списки runtime sources, admins и chats из SQLite.
- Сохранить текущий write-flow через существующие команды и CLI.
- Не раскрывать секреты и не читать локальные `.env` значения в Telegram-ответах.

## Out Of Scope

- Pending-flow для source/chat.
- Изменение source definitions из Telegram.
- Изменение provider/model ключей и `ai-summary-models.local.yaml`.
