# Done

## Previous Runtime Routing Work

- Добавлена разметка и форматирование Telegram-сообщений + префиксы сводки.
- Собран каркас routing в `bot.py` с источником `products.yaml` + state из БД.
- Добавлены вспомогательные нормализации для YAML-конфигураций.
- Подготовлены новые плановые документы для следующего шага работ.
- Реализована runtime-интеграция routing через SQLite:
  - `check_all` грузит маршруты из БД;
  - при первом запуске данные автоматически импортируются из `admin-routing.yaml`;
  - отправка релизов идёт по списку целевых чатов;
  - сводка формируется отдельно для каждого чата с `send_summary`.
- Реализована отложенная доставка per-chat сводок через `summary_queue`:
  - `summary_schedule` поддерживает `immediate`, `daily`, `weekly`;
  - для scheduled сводок хранится состояние `last_summary_sent_at`;
  - алиасы чатов/админов поддерживаются в seed и CLI-командах (`chat_id|alias`).
- Обновлён `.env.example`, `README.md`, `docs/INSTALL_WSL.md`, `docs/ADMIN_DESIGN.md` на SQL-based runtime-routing.
- Реализованы проверки доступности чатов через Telegram API (`getMe`, `getChat`, `getChatMember`).
- Реализован hot-reload routing state:
  - TTL-перезагрузка из SQLite;
  - сигналами `SIGHUP`/`SIGUSR1` + `/reload`.
- Реализована базовая админ-команда по Telegram polling:
  - `/reload`
  - `/subscribe <source_id> [chat_id|alias]`
  - `/unsubscribe <source_id> [chat_id|alias]`
- Переведена routing-логика в SQLite с автоимпортом `admin-routing.yaml` как seed (`DB_PATH`).

## Current Planning Baseline

- Подтверждено решение: SQLite является runtime source-of-truth для Telegram-managed settings.
- Подтверждено решение: YAML остается seed/backup/import/export слоем.
- Подтверждено ограничение: provider/model keys и AI model config управляются только локально, не из Telegram.

## Iteration 1: Settings Export/Import

- Добавлен CLI `--export-settings <path>` для экспорта SQLite routing/settings в публичный YAML shape.
- Добавлен CLI `--import-settings <path>` с validation against source ids from configured products config.
- `--replace` теперь применим к `--import-settings` и явно заменяет routing/settings tables.
- Export/import ограничен routing/settings tables; runtime/history tables `posted_items`, `deliveries`, `summary_queue`, `ai_summaries`, `source_state` не экспортируются.
- Restore выполняется транзакционно после валидации YAML.

## Iteration 2: Runtime Sources In SQLite

- Добавлена SQLite-таблица `runtime_sources` для runtime source definitions с YAML config blob и timestamps.
- `products.yaml` используется как seed для первого запуска и продолжает задавать `poll_minutes`.
- Runtime, validation, scheduler и admin-command paths читают source ids/configs из SQLite после seed.
- `--export-settings`/`--import-settings` теперь включают runtime sources вместе с routing settings.
- `--import-settings --replace` заменяет source/routing settings транзакционно и не удаляет history/runtime tables `posted_items`, `deliveries`, `summary_queue`, `ai_summaries`, `source_state`.
- Следующая итерация: Telegram read/list commands.
