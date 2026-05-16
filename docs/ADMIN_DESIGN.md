# Проект будущей админки для Changelog Watch Bot

## Что взять из текущих твоих ботов

Из `remove-join-messages-telegram-bot` стоит забрать идею простого allowlist/pending-flow:

- список админов;
- список разрешённых чатов;
- запрос от пользователя или чата на добавление;
- команды, которые удобно копировать из сообщений бота.

Из `gitlab-telegram-bot` стоит забрать модель подписок:

- `contacts` / алиасы получателей;
- сущность проекта/источника;
- команды `/link` и `/unlink`, где админ связывает источник с конкретным получателем;
- `/projects`, `/contacts`, `/info`;
- локализацию можно оставить на потом, но структуру команд лучше сразу не ломать.

## Почему лучше перейти с JSON на SQLite

Для этого бота SQLite уже используется под `posted_items`. Поэтому будущую админку лучше хранить там же, а не плодить JSON-файлы.

Главные причины:

- атомарные изменения подписок и источников;
- проще не потерять данные при падении процесса;
- проще мигрировать на сервер;
- проще сделать web/admin UI позже;
- меньше риска рассинхрона между `products.yaml`, списком чатов и posted-state.

## Предлагаемая схема SQLite

```sql
CREATE TABLE admins (
    user_id INTEGER PRIMARY KEY,
    alias TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE chats (
    chat_id INTEGER PRIMARY KEY,
    title TEXT,
    username TEXT,
    type TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE sources (
    source_id TEXT PRIMARY KEY,
    product TEXT NOT NULL,
    type TEXT NOT NULL,
    url TEXT NOT NULL,
    source_url TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    include_prereleases INTEGER NOT NULL DEFAULT 1,
    skip_unreleased INTEGER NOT NULL DEFAULT 1,
    max_body_chars INTEGER NOT NULL DEFAULT 2500,
    post_on_first_run INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE chat_subscriptions (
    chat_id INTEGER NOT NULL,
    source_id TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, source_id),
    FOREIGN KEY (chat_id) REFERENCES chats(chat_id),
    FOREIGN KEY (source_id) REFERENCES sources(source_id)
);

CREATE TABLE pending_chats (
    chat_id INTEGER PRIMARY KEY,
    title TEXT,
    username TEXT,
    requested_by_user_id INTEGER,
    requested_by_name TEXT,
    created_at TEXT NOT NULL
);
```

`posted_items` и `source_state` из текущего MVP можно оставить как есть.

## Режимы работы

### MVP сейчас

- Один `TELEGRAM_CHAT_ID` из `.env`.
- Список источников из `products.yaml`.
- Нет интерактивной админки.
- Хорошо для WSL-теста и первого запуска на сервере.

### Следующий этап

- Бот запускается через polling и слушает команды.
- Рассылка идет не в один чат, а по таблице `chat_subscriptions`.
- `products.yaml` остается seed-файлом: при старте можно импортировать источники в SQLite.
- Изменения из админки пишутся в SQLite.

## Команды для админа

Минимальный набор:

```text
/start
/help
/sources
/source <source_id>
/addsource <source_id> <type> <url> | <product name>
/removesource <source_id>
/enablesource <source_id>
/disablesource <source_id>
/checknow <source_id>
/testsource <source_id>
/chats
/addchat
/removechat <chat_id>
/subscribe <source_id> <chat_id>
/unsubscribe <source_id> <chat_id>
/subscribe_here <source_id>
/unsubscribe_here <source_id>
```

Удобные алиасы:

```text
/products -> /sources
/addrepo -> /addsource
/link -> /subscribe
/unlink -> /unsubscribe
/info -> /source или /subscriptions
```

Так ты сохранишь привычную модель из `gitlab-telegram-bot`.

## Команды для обычного пользователя / чата

```text
/requestchat
/addme
/my_subscriptions
/unsubscribe_here <source_id>
```

Для текущей задачи `addme` необязателен, потому что changelog обычно рассылается в чаты/каналы, а не в лички. Но если хочешь персональные подписки, модель `contacts` из GitLab-бота пригодится.

## Inline-кнопки

Вместо копирования команд можно добавить inline-кнопки:

- `➕ Подписать этот чат`;
- `⏸ Выключить источник`;
- `▶️ Включить источник`;
- `🧪 Тест`;
- `🔁 Проверить сейчас`;
- `🗑 Удалить`.

Для WSL/первой серверной версии команды проще. Inline-кнопки лучше добавить после стабилизации модели данных.

## UX добавления источника

Лучше сделать пошагово, а не одной длинной командой:

```text
/addsource
```

Бот спрашивает:

1. Название продукта.
2. URL.
3. Тип источника: GitHub Releases / Markdown changelog / HTML changelog.
4. Постить pre-release? Да/нет.
5. Постить старые записи при первом запуске? Да/нет.
6. В какие чаты подписать.

Но для CLI-style админки можно оставить короткую команду:

```text
/addsource codenomad github_releases https://github.com/NeuralNomadsAI/CodeNomad/releases | CodeNomad
```

## Проверка источника перед добавлением

Перед сохранением `/addsource` должен:

1. скачать источник;
2. распарсить последние 1-3 записи;
3. показать админу preview;
4. попросить подтвердить сохранение.

Это особенно важно для `html_changelog`, потому что HTML-страницы чаще ломают парсинг.

## Миграция от текущего MVP

1. Оставить `products.yaml` как seed.
2. Добавить команду:

```text
/import_yaml
```

3. Она импортирует источники из `products.yaml` в таблицу `sources`.
4. После этого `products.yaml` можно использовать только для первичной установки или резервной правки руками.

## Рекомендованный стек для админки

Для Telegram-only админки:

- `python-telegram-bot` для команд, inline-кнопок и polling;
- текущий `httpx` оставить для скачивания changelog/release API;
- `APScheduler` оставить для расписания;
- SQLite оставить как основное хранилище.

Для web-админки позже:

- FastAPI;
- SQLite/Postgres;
- простая HTML-админка через Jinja или отдельный frontend;
- авторизация через пароль/Telegram login widget уже на отдельном этапе.

## Что не делать сразу

- Не заводить Nginx/webhook только ради changelog-бота. Для проверки в WSL и простого сервера polling проще и надежнее.
- Не хранить runtime-состояние в нескольких JSON рядом с SQLite.
- Не делать web-админку до того, как устаканятся команды и схема данных.
