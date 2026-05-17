# Установка и проверка в Windows + WSL Ubuntu

## 1. Подготовить Ubuntu в WSL

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git ca-certificates curl unzip
```

Проверь Python:

```bash
python3 --version
```

Нужен Python 3.10+; на актуальной Ubuntu в WSL обычно всё ок.

## 2. Распаковать бота

Если архив лежит в Windows, например в `Downloads`, его можно скопировать из WSL так:

```bash
mkdir -p ~/bots
cd ~/bots
cp /mnt/c/Users/$USER/Downloads/changelog-watch-telegram-bot.zip . 2>/dev/null || true
unzip changelog-watch-telegram-bot.zip
cd changelog-watch-telegram-bot
```

Если `$USER` в WSL не совпадает с именем Windows-пользователя, подставь путь руками:

```bash
cp /mnt/c/Users/roman/Downloads/changelog-watch-telegram-bot.zip .
```

## 3. Создать виртуальное окружение

```bash
cd ~/bots/changelog-watch-telegram-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 4. Настроить `.env`

```bash
cp .env.example .env
nano .env
cp admin-routing.example.yaml admin-routing.yaml
nano admin-routing.yaml
```

Заполни:

```env
TELEGRAM_BOT_TOKEN=токен_от_BotFather
DB_PATH=data/posted.sqlite3
ROUTING_CONFIG_PATH=admin-routing.yaml
ROUTING_SEED_MODE=once
BOT_INSTANCE_LOCK_PATH=data/changelog-watch-telegram-bot.lock
ROUTING_RELOAD_TTL_SECONDS=0
ADMIN_POLL_TIMEOUT=25
ADMIN_COMMAND_POLL_SECONDS=2
LIFECYCLE_NOTIFICATIONS_ENABLED=false
DUPLICATE_INSTANCE_NOTIFICATIONS_ENABLED=true
DISPLAY_TIMEZONE=Europe/Amsterdam
GITHUB_TOKEN=ghp_xxx
```

`TELEGRAM_CHAT_ID` — legacy и routing mode его игнорирует. Нужный chat id указывай в `admin-routing.yaml`.

### Как узнать `chat_id` для `admin-routing.yaml`

Вариант для проверки:

1. Добавь бота в нужный чат или канал.
2. Напиши любое сообщение в этот чат.
3. Выполни:

```bash
source .venv/bin/activate
set -a
source .env
set +a
curl "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getUpdates"
```

В ответе найди `chat.id`. Для супергруппы/канала ID обычно начинается с `-100`.

Если это канал, бот должен быть администратором канала с правом публикации сообщений.

## 5. Первая проверка

```bash
source .venv/bin/activate
python bot.py --once
```

По умолчанию старые версии не отправятся, потому что в `products.yaml` стоит `post_on_first_run: false`. Бот только создаст `data/posted.sqlite3` и запомнит уже существующие версии.

Для проверки парсинга без Telegram:

```bash
python bot.py --once --dry-run
```

Для проверки конфигов без network calls:

```bash
python bot.py --validate-config
```

## 6. Запуск в терминале WSL

```bash
source .venv/bin/activate
python bot.py
```

При запуске в фоне через постоянный процесс доступны команды `/reload`, `/subscribe`, `/unsubscribe`.
Они доступны админам из routing state (seed берётся из `admin-routing.yaml#admins` при первом импорте).

Оставь окно WSL открытым. Бот будет проверять источники раз в 30 минут.

Можно запускать через скрипты управления из `data/`, которые берут виртуальное окружение и корень проекта автоматически:

```bash
./data/start-changelog-watch-bot.sh          # запуск в консоли
./data/stop-changelog-watch-bot.sh           # корректная остановка
./data/restart-changelog-watch-bot.sh        # перезапуск
```

Если запуск идёт из Windows PowerShell 7, удобны скрипты-обертки в корне репозитория:

```powershell
pwsh ./start-changelog-watch-bot.ps1 -Once -DryRun
pwsh ./restart-changelog-watch-bot.ps1 -CheckOnce
pwsh ./stop-changelog-watch-bot.ps1
pwsh ./stop-changelog-watch-bot.ps1  # автоопределение systemd-юнита по bot.py
pwsh ./stop-changelog-watch-bot.ps1 -SystemdServiceName changelog-watch-bot.service
pwsh ./status-changelog-watch-bot.ps1 -Tail
```

Аргумент `--once` и `--dry-run` также пробрасывается через `start`/`restart`. Подробно: `docs/PROCESS_MANAGEMENT.md`.

## 7. Удобный запуск через `tmux`, если нужен долгий тест

```bash
sudo apt install -y tmux
cd ~/bots/changelog-watch-telegram-bot
source .venv/bin/activate
tmux new -s changelog-bot
python bot.py
```

Отсоединиться от сессии: `Ctrl+B`, потом `D`.

Вернуться:

```bash
tmux attach -t changelog-bot
```

## 8. Systemd в WSL, опционально

В свежем WSL systemd обычно можно включить. Проверь:

```bash
systemctl --version
```

Если systemd работает, можно поставить сервис. В `systemd/changelog-watch-bot.service.example` уже есть типовой `ExecStart` и готовый `BOT_INSTANCE_LOCK_PATH` (проверь путь под свой корень):

```bash
sudo cp systemd/changelog-watch-bot.service.example /etc/systemd/system/changelog-watch-bot.service
sudo nano /etc/systemd/system/changelog-watch-bot.service
```

Поменяй `User`, `WorkingDirectory` и пути под свой WSL-пользователь.
Если хочешь хранить lock вне `/tmp`, задай `BOT_INSTANCE_LOCK_PATH` в `.env` или оставь значение из шаблона.

Затем:

```bash
sudo systemctl daemon-reload
sudo systemctl enable changelog-watch-bot
sudo systemctl start changelog-watch-bot
sudo journalctl -u changelog-watch-bot -f
```

### Диагностика `BOT_INSTANCE_LOCK_PATH`

Если в логе попался конфликт `single-instance lock is already held`, проверьте:

```bash
ps -eo pid,args | grep '/changelog-watch-telegram-bot/bot.py' | grep -v grep
```

Если старый процесс уже завершен, а конфликт продолжает появляться, лучше сначала запустить `pwsh ./stop-changelog-watch-bot.ps1`: он удаляет stale `data/bot.pid` и stale lock. Вручную удалить lock-файл можно так:

```bash
source .env
if [ -n "${BOT_INSTANCE_LOCK_PATH:-}" ]; then
  case "$BOT_INSTANCE_LOCK_PATH" in
    /*) rm -f "$BOT_INSTANCE_LOCK_PATH" ;;
    *) rm -f "$(pwd)/$BOT_INSTANCE_LOCK_PATH" ;;
  esac
fi

# если переменная не задавалась в .env
rm -f /tmp/changelog-watch-telegram-bot-*.lock
```

`--dry-run` блокировку не использует, поэтому для проверки конфликтов запускай обычный `python bot.py --once`.

Подробные настройки см. в `docs/CONFIG.md` и `docs/PROCESS_MANAGEMENT.md`.

## 9. Перенос на рабочий сервер позже

На сервере достаточно перенести:

```text
bot.py
products.yaml
requirements.txt
.env
data/posted.sqlite3
```

Если перенести `data/posted.sqlite3`, бот не переопубликует уже запомненные версии.
