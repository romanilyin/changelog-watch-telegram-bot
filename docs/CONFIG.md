# Configuration

## `.env`

Минимально нужно:

```env
TELEGRAM_BOT_TOKEN=123456789:AAH_REPLACE_ME
ROUTING_CONFIG_PATH=admin-routing.yaml
CONFIG_PATH=products.yaml
DB_PATH=data/posted.sqlite3
BOT_INSTANCE_LOCK_PATH=data/changelog-watch-telegram-bot.lock
ROUTING_SEED_MODE=once
```

`TELEGRAM_BOT_TOKEN` нужен только для реальных отправок. Для `--dry-run` он может отсутствовать.

Рекомендуемые локальные добавления:

```env
ROUTING_CONFIG_PATH=admin-routing.yaml
ROUTING_SEED_MODE=once
BOT_INSTANCE_LOCK_PATH=data/changelog-watch-telegram-bot.lock
LIFECYCLE_NOTIFICATIONS_ENABLED=false
DUPLICATE_INSTANCE_NOTIFICATIONS_ENABLED=true
AI_SUMMARY_DRY_RUN_CALL_API=false
AI_SUMMARY_IN_DIGEST=true
DISPLAY_TIMEZONE=Europe/Moscow
```

`TELEGRAM_CHAT_ID=...` — legacy; его лучше удалить из `.env` или оставить как неиспользуемую старую переменную.

## Legacy `TELEGRAM_CHAT_ID`

`TELEGRAM_CHAT_ID` больше не управляет рассылкой. Routing mode его игнорирует и пишет warning:

```text
TELEGRAM_CHAT_ID is legacy and ignored by routing mode. Add this chat_id to admin-routing.yaml.
```

Чтобы отправлять в чат, добавь его в `admin-routing.yaml`.

## Routing Seed

`ROUTING_CONFIG_PATH` указывает YAML seed-файл, который импортируется в SQLite routing tables.

`ROUTING_SEED_MODE`:

- `once` — default; импортировать seed только когда routing DB пустая.
- `sync` — импортировать seed при каждом reload/cycle.
- `off` — не импортировать seed автоматически.

Рекомендуемый режим: `once`. Тогда изменения `/subscribe` и `/unsubscribe`, записанные в SQLite, не перетираются следующим reload. В режиме `sync` seed может снова добавить или перезаписать то, что менялось в runtime.

Если `data/posted.sqlite3` уже существует и `ROUTING_SEED_MODE=once`, изменения в `admin-routing.yaml` не перепишут routing DB автоматически. Чтобы применить seed без удаления истории публикаций, используй:

```bash
python bot.py --import-routing --replace
```

Если `ROUTING_CONFIG_PATH` не задан и routing DB пустая, запуск падает с понятной ошибкой:

```text
ROUTING_CONFIG_PATH is not set and routing DB is empty.
Copy admin-routing.example.yaml to admin-routing.yaml or set ROUTING_CONFIG_PATH.
```

## Routing YAML

Пример:

```yaml
admins:
  - id: 12345678
    alias: maintainer

source_groups:
  all:
    - opencode_changelog
    - openchamber_changelog

chats:
  - chat_id: -1001234567890
    alias: main
    title: Основной канал
    groups:
      - all
    sources: []
    enabled: true
    send_summary: false
    delivery_mode: instant
    summary_on_startup: false
    summary_schedule:
      mode: none
```

Поля чата:

- `chat_id` — Telegram chat/channel ID.
- `alias` — короткое имя для admin commands.
- `groups` — группы источников из `source_groups`.
- `sources` — дополнительные конкретные source ids.
- `enabled` — отключает чат без удаления.
- `delivery_mode` — `instant`, `digest`, `both`, `none`.
- `summary_schedule` — `none`, `immediate`, `daily`, `weekly`.
- `summary_on_startup` — default `false`; не отправлять daily/weekly digest сразу после рестарта только потому, что время уже прошло.

`summary_schedule.mode: immediate` отправляет digest сразу только когда он явно указан.

## Personal Chat Example

Для личных уведомлений без digest summaries:

```yaml
admins:
  - id: 185073278
    alias: roman

source_groups:
  all:
    - opencode_changelog
    - openchamber_changelog
    - codenomad_releases
    - unity_ivanmurzak_releases
    - unity_coplay_releases
    - locus_releases

chats:
  - chat_id: 185073278
    alias: main
    title: Личные уведомления
    groups:
      - all
    sources: []
    enabled: true
    send_summary: false
    delivery_mode: instant
    summary_on_startup: false
    summary_schedule:
      mode: none
```

Важно:

- `TELEGRAM_CHAT_ID` legacy и routing mode его игнорирует.
- `admin-routing.yaml` игнорируется git через `.gitignore`, поэтому локальные chat ids не попадут в репозиторий.
- При `ROUTING_SEED_MODE=once` существующая routing DB не переписывается изменениями YAML; используй `python bot.py --import-routing --replace`, временно `ROUTING_SEED_MODE=sync` или полный сброс DB.

## Summaries

Digest отправляется только для `delivery_mode: digest` или `delivery_mode: both`, если `summary_schedule.mode` не `none`.

Для личного чата без сводок используй:

```yaml
delivery_mode: instant
summary_on_startup: false
summary_schedule:
  mode: none
```

Очередь digest лежит в таблице `summary_queue`. Если случайно накопилась ненужная очередь, после остановки бота можно очистить её вручную:

```sql
DELETE FROM summary_queue;
```

Или безопаснее через CLI без network calls:

```bash
python bot.py --clear-summary-queue
python bot.py --clear-summary-queue --chat-id 185073278
```

Команда удаляет только строки из `summary_queue`; `posted_items`, `deliveries`, `source_state` и `ai_summaries` не трогаются.

Опционально можно не отправлять старые queued items:

```env
SUMMARY_QUEUE_MAX_AGE_DAYS=14
```

Если переменная пустая, фильтр выключен. Старые элементы не отправляются, skipped count пишется в лог.

По умолчанию stale rows остаются в `summary_queue`, чтобы не удалять данные молча. Если нужно удалять skipped stale rows автоматически:

```env
SUMMARY_QUEUE_MAX_AGE_DAYS=14
SUMMARY_QUEUE_PRUNE_STALE=true
```

При `SUMMARY_QUEUE_PRUNE_STALE=false` stale rows остаются до ручной очистки.

## Products

`products.yaml` должен содержать top-level `sources` list и опциональный `poll_minutes`.

Пример GitHub Releases:

```yaml
sources:
  - id: my_product_releases
    product: My Product
    type: github_releases
    url: https://github.com/org/repo/releases
    include_prereleases: false
    post_on_first_run: false
    max_body_chars: 2500
```

Пример Markdown changelog:

```yaml
sources:
  - id: my_product_changelog
    product: My Product
    type: markdown_changelog
    url: https://raw.githubusercontent.com/org/repo/main/CHANGELOG.md
    source_url: https://github.com/org/repo/blob/main/CHANGELOG.md
    skip_unreleased: true
    post_on_first_run: false
    max_body_chars: 2500
```

`source.id` — ключ дедупликации в SQLite. Если изменить id, старые entries будут выглядеть как новые.

## Validation

Проверка без network calls:

```bash
python bot.py --validate-config
```

По умолчанию validation использует in-memory копию SQLite и не меняет DB-файл. Если нужно явно открыть и мигрировать реальную БД:

```bash
python bot.py --validate-config --migrate-db
```

Проверяется:

- `products.yaml` загружается;
- source ids уникальны;
- source types поддерживаются;
- GitHub URLs разбираются в owner/repo;
- routing YAML загружается, если задан `ROUTING_CONFIG_PATH`;
- routing source references существуют;
- summary schedule values валидны;
- SQLite DB открывается и проверяется; миграции реальной БД выполняются только с `--migrate-db`.

## Routing Import

Применить `admin-routing.yaml` к существующей SQLite DB без удаления истории публикаций:

```bash
python bot.py --import-routing --replace
```

Команда:

- загружает `products.yaml`;
- валидирует `ROUTING_CONFIG_PATH` против source ids;
- заменяет routing tables из seed;
- не удаляет `posted_items`, `source_state`, `deliveries`, `summary_queue`, `ai_summaries`.

Если нужно одновременно убрать накопленную digest-очередь:

```bash
python bot.py --import-routing --replace --clear-summary-queue
```

## Dry-Run

```bash
python bot.py --once --dry-run
```

Dry-run:

- не отправляет Telegram-сообщения;
- не берёт singleton-lock;
- не отправляет lifecycle notifications;
- не вызывает AI API по умолчанию;
- читает существующий SQLite через in-memory копию и не меняет DB-файл;
- если DB-файла нет, создаёт schema только in-memory.

Ручная проверка no-write guarantee:

```bash
./check-dry-run-no-write.sh
```

## Admin Commands

В continuous mode доступны команды админам из routing state:

- `/reload` — перечитать routing state.
- `/subscribe <source_id> [chat_id|alias]` — добавить источник в чат.
- `/unsubscribe <source_id> [chat_id|alias]` — убрать источник из чата.

Изменения пишутся в SQLite и сохраняются при рестарте. Они не перетираются seed-файлом в `ROUTING_SEED_MODE=once`.
