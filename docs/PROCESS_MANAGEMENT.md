# Process Management

## Предпочтительный Workflow

Для Windows PowerShell 7 + WSL используй скрипты из корня репозитория:

```powershell
./start-changelog-watch-bot.ps1
./stop-changelog-watch-bot.ps1
./restart-changelog-watch-bot.ps1 -CheckOnce
./status-changelog-watch-bot.ps1 -Tail
```

`status-changelog-watch-bot.ps1` — основной поддерживаемый способ посмотреть PID, lock, stale state, systemd unit и последние строки лога.

## Start

`start-changelog-watch-bot.ps1` запускает `python bot.py` внутри WSL из указанного репозитория.

Поведение:

- пишет PID в `data/bot.pid`;
- пишет лог в `data/bot.log`;
- запускает процесс через `nohup`;
- с `-Force` останавливает старый экземпляр перед запуском нового;
- пытается определить и остановить связанный `systemd --user` unit, если старый процесс управляется systemd.

Примеры:

```powershell
./start-changelog-watch-bot.ps1
./start-changelog-watch-bot.ps1 -Force
./start-changelog-watch-bot.ps1 -Once -DryRun
./start-changelog-watch-bot.ps1 -Tail -TailLines 120
```

## Stop

`stop-changelog-watch-bot.ps1` останавливает только процессы `bot.py`, которые относятся к этому репозиторию.

Поведение:

- ищет процессы по `/proc`, `data/bot.pid` и lock-файлу;
- удаляет stale `data/bot.pid`, если процесса уже нет;
- удаляет stale lock, если PID из lock-файла не является bot-процессом этого репозитория;
- при необходимости останавливает matching `systemd --user` service;
- сначала отправляет `SIGTERM`, затем `SIGKILL` после таймаута.

Примеры:

```powershell
./stop-changelog-watch-bot.ps1
./stop-changelog-watch-bot.ps1 -WaitSeconds 15
./stop-changelog-watch-bot.ps1 -SystemdServiceName changelog-watch-bot.service
```

## Restart

`restart-changelog-watch-bot.ps1` делает stop + start.

Безопасный перезапуск с precheck:

```powershell
./restart-changelog-watch-bot.ps1 -CheckOnce
./restart-changelog-watch-bot.ps1 -CheckOnce -Tail
```

`-CheckOnce` сначала запускает внутри WSL:

```bash
python bot.py --once --dry-run
```

Если precheck падает, текущий бот не останавливается. Чтобы всё равно продолжить:

```powershell
./restart-changelog-watch-bot.ps1 -CheckOnce -ForceCheckFailure
./restart-changelog-watch-bot.ps1 -CheckOnce -ForceCheckFailure -Tail
```

Dry-run precheck не отправляет Telegram-сообщения, не отправляет lifecycle notifications и не вызывает AI API, если `AI_SUMMARY_DRY_RUN_CALL_API=true` не задан явно.

## Status

```powershell
./status-changelog-watch-bot.ps1
./status-changelog-watch-bot.ps1 -Tail
./status-changelog-watch-bot.ps1 -Tail -TailLines 120
./status-changelog-watch-bot.ps1 -NotifyAdmins
```

Статус показывает:

- репозиторий, config и DB path;
- lock path и PID владельца lock;
- `data/bot.pid` и его валидность;
- running instances;
- matching `systemd --user` units;
- предупреждение о нескольких экземплярах.

## Singleton Lock

Рекомендуемый `.env`:

```env
BOT_INSTANCE_LOCK_PATH=data/changelog-watch-telegram-bot.lock
```

Относительный `BOT_INSTANCE_LOCK_PATH` всегда считается от корня репозитория, не от текущей рабочей директории. Пример выше всегда означает:

```text
<repo>/data/changelog-watch-telegram-bot.lock
```

Если переменная не задана, бот использует `/tmp/changelog-watch-telegram-bot-<hash>.lock`, где hash зависит от абсолютного пути `bot.py`.

`--dry-run` lock не берёт.

## Lifecycle Notifications

По умолчанию startup/stop уведомления выключены:

```env
LIFECYCLE_NOTIFICATIONS_ENABLED=false
```

Включить можно так:

```env
LIFECYCLE_NOTIFICATIONS_ENABLED=true
```

Адресаты берутся из `ADMIN_IDS`; если `ADMIN_IDS` пуст, бот пробует admins из routing config.

Уведомления о duplicate instance управляются отдельно:

```env
DUPLICATE_INSTANCE_NOTIFICATIONS_ENABLED=true
```

## systemd

Шаблон: `systemd/changelog-watch-bot.service.example`.

Перед использованием обязательно проверь `User`, `WorkingDirectory`, `EnvironmentFile`, `BOT_INSTANCE_LOCK_PATH` и `ExecStart`.

Типовой install:

```bash
sudo cp systemd/changelog-watch-bot.service.example /etc/systemd/system/changelog-watch-bot.service
sudo nano /etc/systemd/system/changelog-watch-bot.service
sudo systemctl daemon-reload
sudo systemctl enable changelog-watch-bot
sudo systemctl restart changelog-watch-bot
sudo journalctl -u changelog-watch-bot -f
```

Если процесс запущен через `systemd --user`, PowerShell stop/start scripts пытаются определить unit по `ExecStart`; при необходимости укажи `-SystemdServiceName` явно.
