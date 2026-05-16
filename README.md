# Changelog Watch Telegram Bot

Бот по расписанию проверяет список источников обновлений и отправляет новые версии в Telegram.

Поддерживаемые типы источников:

- `html_changelog` — HTML-страница changelog, где версии можно найти по regex.
- `markdown_changelog` — `CHANGELOG.md` с заголовками вида `## [1.2.3] - 2026-05-06`.
- `github_releases` — страница GitHub Releases, например `https://github.com/org/repo/releases`.

## Файлы

```text
bot.py
products.yaml
admin-routing.yaml (seed-файл для начальной инициализации routing-хранилища)
requirements.txt
.env.example
docs/INSTALL_WSL.md
docs/ADMIN_DESIGN.md
systemd/changelog-watch-bot.service
```

## Быстрый старт

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env
cp admin-routing.example.yaml admin-routing.yaml
nano admin-routing.yaml
python bot.py --once
python bot.py
```

Основные переменные окружения:

- `DISPLAY_TIMEZONE` — часовой пояс для отображения времени релизов GitHub (по умолчанию `Europe/Amsterdam`).
- `GITHUB_TOKEN` — токен GitHub API для увеличения лимитов запросов.

Перезагрузка маршрутизации в рантайме:

- `ROUTING_RELOAD_TTL_SECONDS=0` — читать routing state из SQLite каждый цикл.
- `ROUTING_RELOAD_TTL_SECONDS=<N>` — кэшировать routing state `N` секунд.
- `kill -HUP <PID>` или `kill -USR1 <PID>` — форсированно перечитать routing state из SQLite и выполнить новый проход.

Рассылка строится по routing state в SQLite (`DB_PATH`).
`ROUTING_CONFIG_PATH` используется только как seed для первого импорта в БД.
Для каждого чата можно указать конкретные источники (`sources`) и группы источников (`groups`).
`delivery_mode` задаёт стратегию доставки новых релизов для чата:
- `instant` — только немедленная публикация новых релизов.
- `digest` — только сводка.
- `both` — и сразу, и в сводку.
- `none` — пропустить все новые релизы.

Значение по умолчанию:
- если `send_summary: false`, то `delivery_mode` = `instant`;
- если `send_summary: true`, то `delivery_mode` = `both`.

Сводка отправляется только в чаты с режимами `digest` или `both`.
Ниже пример seed-конфига для `admin-routing.yaml`:

```yaml
admins:
  - id: 12345678
    alias: maintainer

source_groups:
  core:
    - opencode_changelog
    - openchamber_changelog

chats:
  - chat_id: -1001234567890
    alias: main
    title: Основной канал
    groups:
      - core
    sources: []
    send_summary: true
    delivery_mode: both
    summary_schedule:
      mode: immediate
  - chat_id: -1009876543210
    alias: backup
    title: Резервный канал
    groups: []
    sources:
      - codenomad_releases
    enabled: true
    send_summary: false
    delivery_mode: digest
    summary_schedule:
      mode: weekly
      time: "20:00"
      weekday: monday
```

Для `summary_schedule` поддерживаются режимы:
- `immediate` (без отсрочки)
- `daily` (по `time`, отправка каждый день после дедлайна)
- `weekly` (по `time` + `weekday`)
- `none` (сводка отключена для данного чата)

### Админ-команды

- `/reload` — перезагрузить конфиг маршрутизации без перезапуска процесса.
- `/subscribe <source_id> [chat_id|alias]` — добавить источник в чат.
- `/unsubscribe <source_id> [chat_id|alias]` — убрать источник из чата.

После подключения администратора все изменения `/subscribe` и `/unsubscribe` пишутся в SQLite.

Если `chat_id` не указан, используется текущий чат.

При первом запуске источники с `post_on_first_run: false` будут только запомнены в SQLite, без отправки старых версий в чат.

## Добавление нового источника

Добавь новый блок в `products.yaml`:

```yaml
  - id: my_product_releases
    product: My Product
    type: github_releases
    url: https://github.com/org/repo/releases
    include_prereleases: false
    post_on_first_run: false
    max_body_chars: 2500
```

Или Markdown changelog:

```yaml
  - id: my_product_changelog
    product: My Product
    type: markdown_changelog
    url: https://raw.githubusercontent.com/org/repo/main/CHANGELOG.md
    source_url: https://github.com/org/repo/blob/main/CHANGELOG.md
    skip_unreleased: true
    post_on_first_run: false
    max_body_chars: 2500
```

## Проверка без отправки сообщений

```bash
python bot.py --once --dry-run
```

В dry-run отдельными строками логируются и обычные сообщения, и сводка (`[summary] DRY RUN would post aggregate`), без вызовов Telegram API.

## Сброс состояния

Состояние публикаций и routing-хранилище лежат в `data/posted.sqlite3`. Для полного сброса (публикаций и подписок) можно остановить бота и удалить файл:

```bash
rm data/posted.sqlite3
```
