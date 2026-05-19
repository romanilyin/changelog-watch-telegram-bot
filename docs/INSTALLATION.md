# Инструкция По Установке И Переносу

Эта инструкция описывает установку `changelog-watch-telegram-bot` на Ubuntu, WSL Ubuntu и Windows 11, включая автозапуск после рестарта системы и перенос настроек на другую машину.

## Что Нужно Подготовить

Общее для всех вариантов:

- Python 3.10+ с `venv`.
- Git или архив с репозиторием.
- Telegram bot token от `@BotFather`.
- Telegram `user_id` админа и `chat_id` целевых чатов.
- Доступ бота к целевым чатам: для группы бот должен быть участником, для канала бот должен быть администратором с правом публикации.
- Опционально: `GITHUB_TOKEN`, если много GitHub sources и нужны повышенные rate limits.
- Опционально: ключи AI providers для AI summaries.

Основные runtime-файлы:

- `.env` - секреты, пути и runtime flags.
- `admin-routing.yaml` - seed-файл admins/chats/groups для первого импорта в SQLite.
- `products.yaml` - seed/backup источников и `poll_minutes`.
- `data/posted.sqlite3` - основное runtime state: posted items, deliveries, digest queue, AI cache, runtime sources и routing.
- `ai-summary-models.local.yaml` - локальный ordered fallback моделей для AI summary.
- `scripts/model-summary-compare.local.yaml` - локальный config для сравнения моделей.
- `data/model-decisions.yaml` - локальные решения для скрытия/повтора моделей при сравнении.

Файлы `.env`, `*.local.yaml`, `admin-routing.yaml` и `data/` не коммитятся в git.

## Telegram Setup

1. Создай бота через `@BotFather` и сохрани token.
2. Добавь бота в нужные группы/каналы.
3. Для канала выдай боту право отправлять сообщения.
4. Узнай свой Telegram `user_id` и `chat_id` целевых чатов.

Самый простой способ узнать id после настройки `.env` и запуска continuous mode:

```text
/id
```

Команда `/id` доступна любому пользователю и возвращает текущие `user_id` и `chat_id`.

Если бот еще не запущен, можно получить update через Telegram API:

```bash
set -a
source .env
set +a
curl "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getUpdates"
```

В ответе найди `message.chat.id` или `channel_post.chat.id`. Для супергрупп и каналов id часто начинается с `-100`.

## Настройка `.env`

Скопируй пример:

```bash
cp .env.example .env
```

Минимальный набор для обычного запуска:

```env
TELEGRAM_BOT_TOKEN=123456789:AAH_REAL_TOKEN
CONFIG_PATH=products.yaml
DB_PATH=data/posted.sqlite3
ROUTING_CONFIG_PATH=admin-routing.yaml
ROUTING_SEED_MODE=once
BOT_INSTANCE_LOCK_PATH=data/changelog-watch-telegram-bot.lock
DISPLAY_TIMEZONE=Europe/Moscow
GITHUB_TOKEN=
```

Важные переменные:

| Переменная | Назначение |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Обязателен для реальной отправки. В `--dry-run` может отсутствовать. |
| `CONFIG_PATH` | Путь к `products.yaml`; CLI `--config` имеет приоритет. |
| `DB_PATH` | Путь к SQLite state; CLI `--db` имеет приоритет. |
| `ROUTING_CONFIG_PATH` | Seed-файл routing state для первого импорта. |
| `ROUTING_SEED_MODE` | `once`, `sync` или `off`; обычно `once`. |
| `BOT_INSTANCE_LOCK_PATH` | Lock от второго экземпляра; лучше repo-local path в `data/`. |
| `DISPLAY_TIMEZONE` | Таймзона дат релизов и digest schedule, например `Europe/Moscow`. |
| `GITHUB_TOKEN` | Опционально, повышает GitHub API rate limits. |
| `ADMIN_IDS` | Только lifecycle/duplicate notifications fallback; Telegram admin-команды берут admins из routing state. |
| `LIFECYCLE_NOTIFICATIONS_ENABLED` | Startup/stop уведомления админам. |
| `DUPLICATE_INSTANCE_NOTIFICATIONS_ENABLED` | Уведомления о конфликте singleton lock. |
| `SUMMARY_QUEUE_MAX_AGE_DAYS` | Опциональный лимит возраста digest queue. |
| `SUMMARY_QUEUE_PRUNE_STALE` | Удалять stale digest queue rows, если включен max age. |

`TELEGRAM_CHAT_ID` является legacy и в routing mode игнорируется.

## Настройка Routing

Скопируй пример:

```bash
cp admin-routing.example.yaml admin-routing.yaml
```

Минимальный пример:

```yaml
admins:
  - id: 185073278
    alias: stinger

source_groups:
  ai:
    - opencode_changelog
    - openchamber_changelog
  unity:
    - unity_ivanmurzak_releases
    - unity_coplay_releases
    - locus_releases

chats:
  - chat_id: 185073278
    alias: stinger
    title: Личная рассылка
    groups:
      - ai
      - unity
    sources: []
    enabled: true
    send_summary: true
    delivery_mode: both
    summary_on_startup: false
    summary_schedule:
      mode: immediate

  - chat_id: -1001234567890
    alias: team
    title: Team releases
    groups:
      - ai
      - unity
    sources: []
    enabled: true
    send_summary: true
    delivery_mode: digest
    summary_on_startup: false
    summary_schedule:
      mode: daily
      time: "08:00"
```

Delivery modes:

| Mode | Поведение |
|---|---|
| `instant` | Отправляет каждый релиз сразу, digest не копит. |
| `digest` | Кладет релизы в queue и отправляет digest по schedule. |
| `both` | Отправляет instant posts и дополнительно digest. |
| `none` | Ничего не отправляет в этот chat. |

Schedule modes:

| Mode | Поведение |
|---|---|
| `none` | Digest выключен. |
| `immediate` | Digest отправляется сразу, когда есть queued entries. |
| `daily` | Digest отправляется каждый день в `time`. |
| `weekly` | Digest отправляется в `weekday` и `time`. |

После первого запуска routing сохраняется в SQLite. Дальше Telegram admin-команды меняют runtime state в `data/posted.sqlite3`.

## AI Summary Setup

AI summaries опциональны. Включение:

```env
AI_SUMMARY_ENABLED=true
AI_SUMMARY_IN_DIGEST=true
AI_SUMMARY_MODELS_CONFIG=ai-summary-models.local.yaml
GOOGLE_API_KEY=...
OPENROUTER_API_KEY=...
OLLAMA_API_KEY=...
```

Создай local config:

```bash
cp ai-summary-models.example.yaml ai-summary-models.local.yaml
```

Если `AI_SUMMARY_MODELS_CONFIG` задан, бот пробует models сверху вниз и использует первый успешный русскоязычный summary. Если config не задан, работает legacy mode через `AI_SUMMARY_API_BASE`, `AI_SUMMARY_MODEL` и `AI_SUMMARY_API_KEY`.

Dry-run по умолчанию не вызывает AI API. Для явной проверки API в dry-run:

```env
AI_SUMMARY_DRY_RUN_CALL_API=true
```

## Проверка Перед Запуском

Команды безопасной проверки:

```bash
python bot.py --validate-config
python bot.py --once --dry-run
python bot.py --self-test-admin-helpers
```

Команда `--dry-run` не отправляет Telegram-сообщения, не берет singleton lock и не пишет в SQLite файл. Если нужно создать/обновить schema без запуска отправки, используй:

```bash
python bot.py --validate-config --migrate-db
```

Первый реальный one-shot запуск:

```bash
python bot.py --once
```

По умолчанию sources с `post_on_first_run: false` seed-ят существующие релизы без отправки старого backlog.

## Ubuntu

Раздел подходит для Ubuntu Server/Desktop с systemd.

### 1. Установить зависимости

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git ca-certificates curl
python3 --version
```

### 2. Создать пользователя и директорию

```bash
sudo adduser --disabled-password --gecos "" changelogbot
sudo mkdir -p /opt/changelog-watch-telegram-bot
sudo chown changelogbot:changelogbot /opt/changelog-watch-telegram-bot
```

### 3. Скачать код

```bash
sudo -iu changelogbot
git clone https://github.com/romanilyin/changelog-watch-telegram-bot.git /opt/changelog-watch-telegram-bot
cd /opt/changelog-watch-telegram-bot
```

Если код переносится архивом, распакуй архив в `/opt/changelog-watch-telegram-bot` и проверь владельца:

```bash
sudo chown -R changelogbot:changelogbot /opt/changelog-watch-telegram-bot
```

### 4. Создать venv и установить Python packages

```bash
cd /opt/changelog-watch-telegram-bot
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 5. Создать локальные config files

```bash
cp .env.example .env
cp admin-routing.example.yaml admin-routing.yaml
cp ai-summary-models.example.yaml ai-summary-models.local.yaml
chmod 600 .env admin-routing.yaml ai-summary-models.local.yaml
nano .env
nano admin-routing.yaml
```

### 6. Проверить запуск вручную

```bash
source .venv/bin/activate
python bot.py --validate-config
python bot.py --once --dry-run
python bot.py --once
```

Для continuous mode без systemd:

```bash
python bot.py
```

### 7. Автозапуск Через systemd

Выйди из пользователя `changelogbot`, если еще находишься в его shell:

```bash
exit
```

Создай service из шаблона:

```bash
sudo cp /opt/changelog-watch-telegram-bot/systemd/changelog-watch-bot.service.example /etc/systemd/system/changelog-watch-bot.service
sudo nano /etc/systemd/system/changelog-watch-bot.service
```

Пример готового service:

```ini
[Unit]
Description=Changelog Watch Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=changelogbot
WorkingDirectory=/opt/changelog-watch-telegram-bot
EnvironmentFile=/opt/changelog-watch-telegram-bot/.env
Environment="BOT_INSTANCE_LOCK_PATH=/opt/changelog-watch-telegram-bot/data/changelog-watch-telegram-bot.lock"
ExecStart=/opt/changelog-watch-telegram-bot/.venv/bin/python /opt/changelog-watch-telegram-bot/bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Включи автозапуск и запусти:

```bash
sudo systemctl daemon-reload
sudo systemctl enable changelog-watch-bot
sudo systemctl start changelog-watch-bot
sudo systemctl status changelog-watch-bot --no-pager
```

Логи:

```bash
sudo journalctl -u changelog-watch-bot -f
```

Управление:

```bash
sudo systemctl restart changelog-watch-bot
sudo systemctl stop changelog-watch-bot
sudo systemctl disable changelog-watch-bot
```

После перезагрузки Ubuntu сервис стартует автоматически.

### 8. Обновление на Ubuntu

```bash
sudo -iu changelogbot
cd /opt/changelog-watch-telegram-bot
git pull --ff-only
source .venv/bin/activate
pip install -r requirements.txt
python bot.py --validate-config
exit
sudo systemctl restart changelog-watch-bot
```

## WSL Ubuntu

Раздел подходит для Ubuntu внутри WSL на Windows 11. Runtime все равно Linux-based, поэтому команды внутри WSL почти такие же, как на Ubuntu.

### 1. Установить WSL Ubuntu

В Windows PowerShell от обычного пользователя:

```powershell
wsl --install -d Ubuntu
wsl --set-default-version 2
```

Перезагрузи Windows, если installer попросит.

### 2. Включить systemd в WSL

Внутри Ubuntu:

```bash
sudo tee /etc/wsl.conf >/dev/null <<'EOF'
[boot]
systemd=true
EOF
```

В Windows PowerShell:

```powershell
wsl --shutdown
wsl -d Ubuntu
```

Проверь внутри WSL:

```bash
systemctl --version
```

### 3. Установить бота внутри Linux filesystem

Рекомендуется держать runtime в `~/bots` или `/opt`, а не в `/mnt/c`, чтобы SQLite и venv работали быстрее и надежнее.

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git ca-certificates curl
mkdir -p ~/bots
git clone https://github.com/romanilyin/changelog-watch-telegram-bot.git ~/bots/changelog-watch-telegram-bot
cd ~/bots/changelog-watch-telegram-bot
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
cp admin-routing.example.yaml admin-routing.yaml
cp ai-summary-models.example.yaml ai-summary-models.local.yaml
chmod 600 .env admin-routing.yaml ai-summary-models.local.yaml
nano .env
nano admin-routing.yaml
python bot.py --validate-config
python bot.py --once --dry-run
```

### 4. Автозапуск Внутри WSL Через systemd

Можно использовать system-wide service, как на Ubuntu. Если repo лежит в `~/bots`, замени пути на абсолютные:

```bash
readlink -f ~/bots/changelog-watch-telegram-bot
```

Создай service:

```bash
sudo cp ~/bots/changelog-watch-telegram-bot/systemd/changelog-watch-bot.service.example /etc/systemd/system/changelog-watch-bot.service
sudo nano /etc/systemd/system/changelog-watch-bot.service
```

Пример для пользователя `roman`:

```ini
[Unit]
Description=Changelog Watch Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=roman
WorkingDirectory=/home/roman/bots/changelog-watch-telegram-bot
EnvironmentFile=/home/roman/bots/changelog-watch-telegram-bot/.env
Environment="BOT_INSTANCE_LOCK_PATH=/home/roman/bots/changelog-watch-telegram-bot/data/changelog-watch-telegram-bot.lock"
ExecStart=/home/roman/bots/changelog-watch-telegram-bot/.venv/bin/python /home/roman/bots/changelog-watch-telegram-bot/bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Включи service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable changelog-watch-bot
sudo systemctl start changelog-watch-bot
sudo systemctl status changelog-watch-bot --no-pager
```

### 5. Автозапуск После Рестарта Windows

`systemctl enable` стартует service при запуске WSL distro. Но Windows не всегда запускает WSL distro сам после reboot. Добавь Windows Scheduled Task, который поднимает WSL при logon.

В Windows PowerShell:

```powershell
$Action = New-ScheduledTaskAction -Execute "wsl.exe" -Argument '-d Ubuntu --exec /bin/true'
$Trigger = New-ScheduledTaskTrigger -AtLogOn
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName "Start WSL Ubuntu for Changelog Bot" -Action $Action -Trigger $Trigger -Settings $Settings -Description "Boot WSL so systemd starts changelog-watch-bot"
```

Проверка после logon:

```powershell
wsl -d Ubuntu -- systemctl status changelog-watch-bot --no-pager
```

Если не хочешь использовать systemd в WSL, можно создать Scheduled Task, который сразу запускает bot через `nohup`:

```powershell
$Command = 'cd ~/bots/changelog-watch-telegram-bot && mkdir -p data && nohup ./.venv/bin/python bot.py >> data/bot.log 2>&1 < /dev/null &'
$Action = New-ScheduledTaskAction -Execute "wsl.exe" -Argument "-d Ubuntu -- bash -lc `"$Command`""
$Trigger = New-ScheduledTaskTrigger -AtLogOn
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName "Changelog Watch Bot WSL" -Action $Action -Trigger $Trigger -Settings $Settings
```

Вариант с systemd предпочтительнее, потому что service сам рестартует бот при падении.

## Windows 11

Нативный запуск через Windows Python сейчас не поддерживается: `bot.py` использует POSIX `fcntl` для singleton lock и Linux process semantics. На Windows 11 запускай бота через WSL Ubuntu.

Этот раздел описывает Windows 11 как host: код может лежать в Windows path, а процесс выполняется внутри WSL через PowerShell scripts из репозитория.

### 1. Установить компоненты

В Windows PowerShell:

```powershell
wsl --install -d Ubuntu
winget install --id Microsoft.PowerShell --source winget
winget install --id Git.Git --source winget
```

Открой PowerShell 7 (`pwsh`) и клонируй repo в Windows filesystem:

```powershell
mkdir C:\Bots
cd C:\Bots
git clone https://github.com/romanilyin/changelog-watch-telegram-bot.git
cd C:\Bots\changelog-watch-telegram-bot
```

### 2. Установить Linux Dependencies В Этом Repo Через WSL

PowerShell scripts ожидают, что в WSL внутри repo есть `.venv/bin/python`.

```powershell
wsl -d Ubuntu -- bash -lc "cd /mnt/c/Bots/changelog-watch-telegram-bot && sudo apt update && sudo apt install -y python3 python3-venv python3-pip git ca-certificates curl && python3 -m venv .venv && . .venv/bin/activate && pip install --upgrade pip && pip install -r requirements.txt"
```

### 3. Создать Локальные Configs

В PowerShell:

```powershell
Copy-Item .env.example .env
Copy-Item admin-routing.example.yaml admin-routing.yaml
Copy-Item ai-summary-models.example.yaml ai-summary-models.local.yaml
notepad .env
notepad admin-routing.yaml
```

Проверка из PowerShell:

```powershell
wsl -d Ubuntu -- bash -lc "cd /mnt/c/Bots/changelog-watch-telegram-bot && ./.venv/bin/python bot.py --validate-config"
wsl -d Ubuntu -- bash -lc "cd /mnt/c/Bots/changelog-watch-telegram-bot && ./.venv/bin/python bot.py --once --dry-run"
```

### 4. Запуск Через PowerShell Scripts

Из PowerShell 7 в корне repo:

```powershell
./start-changelog-watch-bot.ps1
./status-changelog-watch-bot.ps1 -Tail
./restart-changelog-watch-bot.ps1 -CheckOnce
./stop-changelog-watch-bot.ps1
```

Полезные варианты:

```powershell
./start-changelog-watch-bot.ps1 -Force -Tail
./restart-changelog-watch-bot.ps1 -CheckOnce -Tail
./restart-changelog-watch-bot.ps1 -CheckOnce -ForceCheckFailure
./status-changelog-watch-bot.ps1 -Tail -TailLines 120
```

Скрипты пишут PID в `data/bot.pid`, log в `data/bot.log`, останавливают только bot process из текущего repo и умеют чистить stale lock.

### 5. Автозапуск После Рестарта Windows 11

Создай Scheduled Task at logon, который запускает PowerShell wrapper.

В PowerShell 7:

```powershell
$Repo = "C:\Bots\changelog-watch-telegram-bot"
$Script = Join-Path $Repo "start-changelog-watch-bot.ps1"
$Action = New-ScheduledTaskAction -Execute "pwsh.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Script`" -Force" -WorkingDirectory $Repo
$Trigger = New-ScheduledTaskTrigger -AtLogOn
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName "Changelog Watch Telegram Bot" -Action $Action -Trigger $Trigger -Settings $Settings -Description "Start changelog-watch-telegram-bot in WSL"
```

Проверка task:

```powershell
Start-ScheduledTask -TaskName "Changelog Watch Telegram Bot"
Get-ScheduledTaskInfo -TaskName "Changelog Watch Telegram Bot"
./status-changelog-watch-bot.ps1 -Tail
```

Удалить task:

```powershell
Unregister-ScheduledTask -TaskName "Changelog Watch Telegram Bot" -Confirm:$false
```

Если repo лежит внутри Linux filesystem (`~/bots/...`), используй WSL section и Scheduled Task с `wsl.exe`, а не PowerShell wrapper path из Windows.

## Перенос На Другую Машину

Есть два способа: полный перенос SQLite state или перенос только routing/settings.

### Вариант A: Полный Перенос State

Этот вариант сохраняет память о публикациях, delivery statuses, digest queue, AI cache, runtime sources и routing. Он предпочтителен для production migration.

На старой машине останови бота:

```bash
sudo systemctl stop changelog-watch-bot
```

Если бот запущен через PowerShell wrapper:

```powershell
./stop-changelog-watch-bot.ps1
```

Скопируй на новую машину:

```text
.env
admin-routing.yaml
products.yaml
data/posted.sqlite3
ai-summary-models.local.yaml
scripts/model-summary-compare.local.yaml
data/model-decisions.yaml
```

Файлы `ai-summary-models.local.yaml`, `scripts/model-summary-compare.local.yaml` и `data/model-decisions.yaml` нужны только если используются AI summaries или model comparison.

После копирования на новой машине:

```bash
chmod 600 .env admin-routing.yaml ai-summary-models.local.yaml 2>/dev/null || true
python bot.py --validate-config
python bot.py --once --dry-run
```

Если `data/posted.sqlite3` перенесен, бот не должен повторно отправлять уже известные релизы.

### Вариант B: Перенос Только Settings

Этот вариант переносит runtime sources/routing/admin/chats/subscriptions, но не переносит posted history, deliveries, digest queue и AI cache.

На старой машине:

```bash
python bot.py --export-settings data/settings-backup.yaml
```

Скопируй на новую машину:

```text
.env
products.yaml
admin-routing.yaml
data/settings-backup.yaml
ai-summary-models.local.yaml
scripts/model-summary-compare.local.yaml
data/model-decisions.yaml
```

На новой машине:

```bash
python bot.py --validate-config --migrate-db
python bot.py --import-settings data/settings-backup.yaml --replace
python bot.py --validate-config
python bot.py --once --dry-run
```

Без `data/posted.sqlite3` бот будет считать историю публикаций новой. При `post_on_first_run: false` существующие релизы обычно будут silently seeded, но меняя `source.id` можно спровоцировать повторную отправку.

## Что Проверить После Переноса

Проверь env и paths:

```bash
python bot.py --validate-config
python bot.py --once --dry-run
```

Проверь Telegram runtime state после запуска continuous mode:

```text
/status
/chats
/sources
/subscriptions
```

Проверь systemd:

```bash
systemctl status changelog-watch-bot --no-pager
journalctl -u changelog-watch-bot -n 100 --no-pager
```

Для Windows PowerShell wrapper:

```powershell
./status-changelog-watch-bot.ps1 -Tail
```

## Model Comparison Setup

Сравнение моделей не требуется для работы Telegram bot, но полезно для выбора AI summary модели.

Создай local config:

```bash
cp scripts/model-summary-compare.example.yaml scripts/model-summary-compare.local.yaml
```

Полезные команды:

```bash
python scripts/compare-model-summaries.py --list-items --limit 10
python scripts/compare-model-summaries.py --list-provider-models --models-config scripts/model-summary-compare.local.yaml
python scripts/compare-model-summaries.py --refresh-model-lists --models-config scripts/model-summary-compare.local.yaml
python scripts/compare-model-summaries.py --dry-run --models-config scripts/model-summary-compare.local.yaml
python scripts/compare-model-summaries.py --models-config scripts/model-summary-compare.local.yaml --limit 5
```

Локальный web UI:

```bash
python scripts/model-summary-admin.py --models-config scripts/model-summary-compare.local.yaml
```

Открой `http://127.0.0.1:8765`.

Локальные decisions:

```bash
cp scripts/model-decisions.example.yaml data/model-decisions.yaml
```

`action: skip` и `action: retry_later` скрывают модели из lists/UI по умолчанию. Чтобы увидеть скрытые модели, добавь `--include-hidden-models`.

## Troubleshooting

Если бот не отправляет сообщения:

```bash
python bot.py --once --dry-run
python bot.py --validate-config
```

Проверь:

- `TELEGRAM_BOT_TOKEN` задан и не содержит пробелов.
- Бот добавлен в чат или канал.
- Для канала бот имеет право публиковать сообщения.
- Chat id в routing state совпадает с реальным chat id.
- Source enabled и chat enabled.
- Delivery mode не `none`.
- В `data/posted.sqlite3` источник уже мог быть seeded, поэтому старые релизы не отправляются повторно.

Если видишь `single-instance lock is already held`:

```bash
ps -eo pid,args | grep 'bot.py' | grep -v grep
```

Если старого процесса нет, удали stale lock:

```bash
source .env
rm -f data/changelog-watch-telegram-bot.lock
rm -f /tmp/changelog-watch-telegram-bot-*.lock
```

Если используется PowerShell wrapper, лучше сначала выполнить:

```powershell
./stop-changelog-watch-bot.ps1
./status-changelog-watch-bot.ps1
```

Если AI summary не появляется:

- Проверь `AI_SUMMARY_ENABLED=true`.
- Проверь `AI_SUMMARY_MODELS_CONFIG` и наличие local YAML.
- Проверь provider API keys в `.env`.
- Проверь logs на `missing API key`, `rate-limited`, `empty content`, `non-ru summary`.
- Для dry-run API calls включи `AI_SUMMARY_DRY_RUN_CALL_API=true`.

## Безопасность

Не коммить и не отправляй в публичные места:

- `.env`
- `admin-routing.yaml`, если в нем реальные chat ids.
- `ai-summary-models.local.yaml`
- `scripts/model-summary-compare.local.yaml`
- `data/posted.sqlite3`
- `data/model-decisions.yaml`, если там локальные эксперименты.

Перед передачей архива другой машине проверь, что token и API keys уходят только туда, где они действительно нужны.
