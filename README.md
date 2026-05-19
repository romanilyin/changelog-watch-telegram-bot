# Changelog Watch Telegram Bot

Бот по расписанию проверяет источники обновлений и отправляет новые версии в Telegram по routing-правилам из SQLite.

Поддерживаемые источники:

- `html_changelog` — HTML changelog с версиями по regex.
- `markdown_changelog` — `CHANGELOG.md` с заголовками `## [1.2.3] - 2026-05-06`.
- `github_releases` — GitHub Releases, например `https://github.com/org/repo/releases`.

## Быстрый Старт

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
cp admin-routing.example.yaml admin-routing.yaml
nano .env
nano admin-routing.yaml
python bot.py --validate-config
python bot.py --once --dry-run
python bot.py --once
python bot.py
./scripts/check-local.sh
```

`--dry-run` не отправляет Telegram-сообщения, не берёт singleton-lock и по умолчанию не вызывает AI API. Для существующей БД dry-run работает с in-memory копией и не меняет файл SQLite.

## Основные Команды

```bash
python bot.py --validate-config
python bot.py --validate-config --migrate-db
python bot.py --import-routing --replace
python bot.py --export-settings data/settings-backup.yaml
python bot.py --import-settings data/settings-backup.yaml --replace
python bot.py --clear-summary-queue --chat-id 185073278
python bot.py --once --dry-run
python bot.py --once
python bot.py
```

Windows PowerShell 7 + WSL:

```powershell
./start-changelog-watch-bot.ps1
./start-changelog-watch-bot.ps1 -Force -Tail
./restart-changelog-watch-bot.ps1 -CheckOnce
./restart-changelog-watch-bot.ps1 -CheckOnce -ForceCheckFailure
./stop-changelog-watch-bot.ps1
./status-changelog-watch-bot.ps1 -Tail
```

Предпочтительный статус-инструмент: `status-changelog-watch-bot.ps1`.

## Важные Файлы

- `bot.py` — основной бот и CLI.
- `products.yaml` — seed/backup список источников и `poll_minutes`.
- `admin-routing.yaml` — seed-файл routing-настроек.
- `.env` — runtime-настройки.
- `data/posted.sqlite3` — runtime sources, routing, состояние публикаций, очереди digest и AI cache.
- `check-dry-run-no-write.sh` — ручная проверка, что dry-run не меняет SQLite.

## Настройки

Ключевые переменные из `.env.example`:

- `ROUTING_CONFIG_PATH=admin-routing.yaml` — seed-файл для первого импорта routing state.
- `ROUTING_SEED_MODE=once` — `once`, `sync` или `off`.
- `BOT_INSTANCE_LOCK_PATH=data/changelog-watch-telegram-bot.lock` — repo-local lock; относительный путь считается от корня репозитория.
- `LIFECYCLE_NOTIFICATIONS_ENABLED=false` — startup/stop уведомления админам.
- `DUPLICATE_INSTANCE_NOTIFICATIONS_ENABLED=true` — уведомления о конфликте инстансов.
- `TELEGRAM_CHAT_ID` — legacy, routing mode его игнорирует.

Для личных уведомлений без digest добавь свой `chat_id` в `admin-routing.yaml`; пример есть в `docs/CONFIG.md#personal-chat-example`.

Подробно: `docs/CONFIG.md`.

## Telegram Admin Commands

В continuous mode доступны runtime-команды:

- `/id` — показать текущие `user_id` и `chat_id`, доступно любому пользователю.
- `/admins`, `/chats` (`/contacts`), `/sources` (`/projects`), `/source <source_id>` (`/info <source_id>`) — короткий просмотр runtime settings из SQLite, только для admins.
- `/reload`, `/subscribe <source_id> [chat_id|alias]`, `/unsubscribe <source_id> [chat_id|alias]` — существующие admin write/reload команды.

## Документация

- `docs/INSTALL_WSL.md` — полный setup в Windows + WSL.
- `docs/PROCESS_MANAGEMENT.md` — start/stop/restart/status, singleton-lock, systemd.
- `docs/CONFIG.md` — `.env`, `products.yaml`, routing, summaries, dry-run.
- `docs/AI_SUMMARY.md` — OpenCode Zen / AI one-line summaries.
- `docs/ADMIN_DESIGN.md` — админ-команды и routing-дизайн.

## Сброс Состояния

Для полного сброса публикаций, routing state, digest-очереди и AI cache останови бота и удали БД:

```bash
rm data/posted.sqlite3
```

Для удаления только случайно накопленной digest-очереди:

```bash
python bot.py --clear-summary-queue
python bot.py --clear-summary-queue --chat-id 185073278
```
