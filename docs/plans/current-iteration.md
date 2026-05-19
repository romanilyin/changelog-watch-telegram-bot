# Current Iteration

## Iteration 5: Telegram Source Management With Staging

Цель: добавить безопасное управление runtime source definitions из Telegram через staging/approval flow.

## Scope

- Staging-заявки на добавление/изменение runtime sources.
- Просмотр diff/validation результата перед применением.
- Apply/reject staged source changes из admin commands.
- Сохранить SQLite как runtime source-of-truth.

## Out Of Scope

- Изменение provider/model ключей и `ai-summary-models.local.yaml`.
- Полный UI wizard вместо коротких Telegram commands.
