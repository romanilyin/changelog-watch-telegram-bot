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
python bot.py --once
python bot.py
```

Переменная `SUMMARY_CHAT_IDS` позволяет отправлять сводный дайджест новых релизов в отдельные чаты. Если не указать, сводка уходит в тот же `TELEGRAM_CHAT_ID`.

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

## Сброс памяти о публикациях

Память хранится в `data/posted.sqlite3`. Для полного сброса можно остановить бота и удалить файл:

```bash
rm data/posted.sqlite3
```
