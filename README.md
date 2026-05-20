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
python bot.py --self-test-admin-helpers
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
- `ai-summary-models.local.yaml` — локальный ordered provider/model fallback для AI summary.
- `.env` — runtime-настройки.
- `data/posted.sqlite3` — runtime sources, routing, состояние публикаций, очереди digest и AI cache.
- `icon_watcher.png` — иконка/branding для бота.
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
- `/requestchat [alias]`, `/addme [alias]` — создать pending-заявку на добавление текущего чата, доступно любому пользователю.
- `/admins`, `/chats` (`/contacts`), `/routing [chat_id|alias]`, `/deliveries [chat_id|alias]`, `/schedule [chat_id|alias]`, `/sources` (`/projects`), `/source <source_id>` (`/info <source_id>`), `/channels`, `/channel <channel>` — просмотр runtime settings из SQLite.
- `/pending`, `/approvechat`, `/rejectchat`, `/addchat_here`, `/removechat`, `/enablechat`, `/disablechat`, `/setchatenabled`, `/addadmin`, `/removeadmin`, `/chatadmins`, `/addchatadmin`, `/removechatadmin`, `/setchatalias`, `/setchattitle`, `/setchatdelivery`, `/setchatschedule`, `/setstartupsummary` — admin chat/admin management в SQLite.
- `/testsource`, `/addrepo`, `/addsource`, `/pendingsources`, `/confirmsource`, `/rejectsource`, `/enablesource`, `/disablesource`, `/setsourceprivate`, `/removesource` — safe source management через staging/approval; source validation использует текущий parser/network path.
- `/reload`, `/status`, `/subscriptions [chat_id|alias]` — operational read-команды для admins.
- `/subscribe` (`/link`), `/unsubscribe` (`/unlink`), `/subscribe_here`, `/unsubscribe_here`, `/channel_subscribe`, `/channel_unsubscribe` — admin-команды подписок с routing reload.

`chat_admins` внутри chat могут добавлять только новые публичные sources, управлять подписками и менять `enabled`/`delivery_mode`/`summary_schedule` только своих чатов. Sources с `private: true` видят и редактируют только full admins.

## Admin Runbook

1. Setup: скопируй примеры без секретов, заполни локальные значения и проверь конфиг.

```bash
cp .env.example .env
cp admin-routing.example.yaml admin-routing.yaml
python bot.py --validate-config
```

2. Backup/restore runtime settings: экспортируй SQLite routing/source settings в YAML и восстанавливай через `--import-settings --replace`.

```bash
python bot.py --export-settings data/settings-backup.yaml
python bot.py --import-settings data/settings-backup.yaml --replace
```

3. Add a chat: в нужном Telegram-чате выполни `/requestchat alias`, затем admin подтверждает `/approvechat <chat_id> alias`; для private chat можно использовать `/addme alias`.

4. Add a source: admin проверяет и staging-ит источник через `/addrepo <owner/repo|github_url> [source_id] [product name...]` или `/addsource <source_id> <type> <url> | <product name>`, затем применяет `/confirmsource <token>`.

5. Subscribe: добавь источник в чат через `/subscribe <source_id> [chat_id|alias]`, `/link <source_id> <chat_id|alias>` или `/subscribe_here <source_id>`.

6. Check status: используй `/status`, `/routing [chat_id|alias]`, `/deliveries [chat_id|alias]`, `/schedule [chat_id|alias]`, `/subscriptions [chat_id|alias]`, `/chats`, `/sources` и `/source <source_id>`; локально можно запустить `python bot.py --self-test-admin-helpers`.

## Документация

- `docs/INSTALLATION.md` — подробная установка на Ubuntu, WSL Ubuntu и Windows 11, автозапуск и перенос настроек.
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
