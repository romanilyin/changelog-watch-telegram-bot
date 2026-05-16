# Current Iteration

- [x] Добавлены dataclass-модели `ChatRouting`, `RoutingConfig`.
- [x] Реализован загрузчик `load_routing_config` и нормализаторы ID.
- [x] Построение карты `source -> chats` через `build_source_to_chat_map`.
- [x] Подготовлен формат `admin-routing.yaml` (пример в `admin-routing.example.yaml`).
- [x] Интегрировать routing state в runtime для отправки новых релизов.
- [x] Убрать fallback-логику на `TELEGRAM_CHAT_ID` и `SUMMARY_CHAT_IDS` из кода и docs.
- [x] Обновить документацию по деплою, install и переменным окружения.
- [x] Добавить проверку доступа Telegram-бота к чатам из `admin-routing.yaml`.
- [x] Добавить горячую перезагрузку routing state по TTL/сигналу.
- [x] Добавить валидацию дублирующихся `chat_id` при чтении routing config.
- [x] Добавить админские команды `/reload`, `/subscribe`, `/unsubscribe` через Telegram polling.
- [x] Перевести `admin-routing.yaml` в SQLite-рутинг: seed из файла и хранение подписок/админов в `data/posted.sqlite3`.
