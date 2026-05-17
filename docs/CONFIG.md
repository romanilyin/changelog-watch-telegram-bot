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

Опционально можно не отправлять старые queued items:

```env
SUMMARY_QUEUE_MAX_AGE_DAYS=14
```

Если переменная пустая, фильтр выключен. Старые элементы не отправляются, skipped count пишется в лог.

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

Проверяется:

- `products.yaml` загружается;
- source ids уникальны;
- source types поддерживаются;
- GitHub URLs разбираются в owner/repo;
- routing YAML загружается, если задан `ROUTING_CONFIG_PATH`;
- routing source references существуют;
- summary schedule values валидны;
- SQLite DB открывается и мигрирует.

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
