# Global Roadmap

1. Увести маршрутизацию из `TELEGRAM_CHAT_ID` в файл `admin-routing.yaml` без fallback.
2. Реализовать per-chat summary на основе `send_summary`.
3. Дать админам базовые Telegram-команды (`/reload`, `/subscribe`, `/unsubscribe`) для управления routing state (`admin-routing.yaml` остается seed-файлом).
4. Перенести subscriptions в SQLite: `admin-routing.yaml` = seed, runtime state в `DB_PATH`.
5. Добавить web-интерфейс и audit-логи для изменения маршрутизации.
