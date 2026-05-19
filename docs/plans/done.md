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

## Iteration 3: Telegram Read/List Commands

- Расширен существующий HTTP long-polling admin listener без `python-telegram-bot`.
- Добавлены read/list команды из SQLite runtime state: `/admins`, `/chats` (`/contacts`), `/sources` (`/projects`), `/source <source_id>` (`/info <source_id>`).
- Добавлена команда `/id`, доступная любому пользователю для setup.
- `/help` обновлен с read aliases и существующими `/reload`, `/subscribe`, `/unsubscribe`.
- Telegram-ответы экранируют runtime/user values для HTML parse mode и режутся на короткие chunks.

## Iteration 4: Telegram Admin/Chat Management

- Добавлена SQLite-таблица `pending_chats` для заявок из `/requestchat` и `/addme` с chat/requester metadata.
- Расширен существующий HTTP long-polling listener без новых Telegram dependencies.
- Добавлены admin commands для approve/reject/add/remove/enable/disable chats, chat alias/title/delivery и add/remove admins.
- Mutating commands пишут SQLite и запрашивают hot reload routing state где нужно.
- `/removechat` удаляет routing row/subscriptions без ручного удаления history/runtime delivery tables.

## Iteration 5: Telegram Source Management With Staging

- Добавлена SQLite-таблица `pending_sources` для staging-заявок на source changes; export/import settings её не включает.
- Расширен существующий HTTP long-polling admin listener без новых dependencies/frameworks.
- Добавлены admin commands `/testsource`, `/addrepo`, `/addsource`, `/pendingsources`, `/confirmsource`, `/rejectsource`, `/enablesource`, `/disablesource`, `/removesource`.
- Validation перед staging использует существующий `parse_source` path и требует хотя бы одну entry.
- Apply/toggle/remove пишут SQLite и запрашивают reload; `/removesource` блокируется при ссылках из source groups или direct chat subscriptions.
- Telegram source commands не принимают provider/model/API keys.
- Следующая итерация: subscription aliases and operational commands.

## Iteration 6: Subscription Aliases And Operational Commands

- Добавлены aliases `/link` и `/unlink` для совместимости со старым GitLab bot UX.
- Добавлены convenience commands `/subscribe_here`, `/unsubscribe_here`, `/subscriptions [chat_id|alias]`.
- Добавлена safe operational команда `/status` с runtime counts, `poll_minutes` и безопасной меткой DB path.
- Mutating subscription commands пишут SQLite и запрашивают routing reload.
- `/help`, `README.md` и `docs/CONFIG.md` обновлены минимально.

## Iteration 7: Hardening And Final Docs

- Проверены admin command formatting helpers на HTML-safe runtime values.
- Закрыт edge case с raw `pending_sources.preview_text` в `/pendingsources`.
- Добавлен optional CLI `--self-test-admin-helpers` с in-memory проверками без network calls и записи в DB-файл.
- `README.md` и `docs/CONFIG.md` дополнены кратким admin runbook: setup, backup/restore, добавление чата, добавление источника, подписка и status checks.
- `docs/plans/current-iteration.md` переведён в post-MVP/backlog pointer.
