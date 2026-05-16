Лучше **не пытаться отправлять Markdown как есть**. Telegram не умеет настоящие `#`, `##`, `###` как заголовки. Он поддерживает только набор форматирования: bold/italic/underline/strike/spoiler, blockquote, inline links, code/pre и т.п.; для текста `sendMessage` лимит — **4096 символов после парсинга entities**. ([Core Telegram][1])

Я бы делал так: **Markdown → нормализованная модель changelog → Telegram HTML**.

## Что выбрать: HTML или MarkdownV2

Для твоего случая я бы выбрал **`parse_mode: "HTML"`**, а не MarkdownV2.

Почему:

MarkdownV2 в Telegram очень капризный: нужно экранировать `_ * [ ] ( ) ~ ` > # + - = | { } . !`почти везде. :contentReference[oaicite:1]{index=1}  
HTML проще контролировать: экранируешь только пользовательский текст через`<`, `>`, `&`, `"`, а свои теги вставляешь сам. Telegram официально поддерживает HTML-теги вроде `<b>`, `<i>`, `<u>`, `<s>`, `<a>`, `<code>`, `<pre>`, `<blockquote>`. ([Core Telegram][1])

То есть посты лучше отправлять так:

```ts
await bot.sendMessage(chatId, htmlText, {
  parse_mode: "HTML",
  link_preview_options: { is_disabled: true },
  reply_markup: {
    inline_keyboard: [
      [{ text: "Открыть changelog", url: sourceUrl }]
    ]
  }
});
```

## Рекомендуемый формат поста

Пример для changelog:

```html
<b>🆕 OpenCode — v1.2.3</b>
<i>Обновление от 16 мая 2026</i>

<b>Коротко</b>
• Добавили поддержку нового режима
• Исправили падение при запуске
• Улучшили работу CLI

<b>Added</b>
• Новая команда <code>opencode upgrade</code>
• Поддержка конфигурации через <code>opencode.json</code>

<b>Fixed</b>
• Исправлен баг с путями в Windows
• Исправлена ошибка авторизации

<b>Changed</b>
• Обновлена логика запуска агентов

<a href="https://example.com/changelog">Источник</a>
```

В Telegram это будет выглядеть примерно так:

**🆕 OpenCode — v1.2.3**
*Обновление от 16 мая 2026*

**Коротко**
• Добавили поддержку нового режима
• Исправили падение при запуске
• Улучшили работу CLI

**Added**
• Новая команда `opencode upgrade`
• Поддержка конфигурации через `opencode.json`

**Fixed**
• Исправлен баг с путями в Windows
• Исправлена ошибка авторизации

## Как маппить Markdown в Telegram

Я бы использовал такие правила:

| Markdown            | Telegram HTML                                                                |
| ------------------- | ---------------------------------------------------------------------------- |
| `# Title`           | первая строка: `<b>🆕 Product — Title</b>`                                   |
| `## Added`          | пустая строка + `<b>Added</b>`                                               |
| `### Small section` | `<b>— Small section</b>` или `<i>Small section</i>`                          |
| `- item` / `* item` | `• item`                                                                     |
| вложенный список    | `  ◦ item`                                                                   |
| `**bold**`          | `<b>bold</b>`                                                                |
| `_italic_`          | `<i>italic</i>`                                                              |
| `` `code` ``        | `<code>code</code>`                                                          |
| code block          | `<pre><code>...</code></pre>`                                                |
| `[text](url)`       | `<a href="url">text</a>`                                                     |
| raw URL             | лучше убрать из текста и вынести в inline-кнопку                             |
| table               | не отправлять как таблицу; преобразовать в список `Name: value`              |
| image               | либо игнорировать, либо отправлять отдельным `sendPhoto` с короткой подписью |

## Заголовки

В Telegram нет настоящих H1/H2/H3. Поэтому лучше имитировать их:

```html
<b>🆕 Product Name — v1.0.0</b>
```

Для секций:

```html
<b>Added</b>
```

Для подсекций:

```html
<b>— CLI</b>
```

Не советую делать так:

```text
# Product Name
## Added
### CLI
```

В Telegram это будет выглядеть как обычный текст с символами `#`.

## Эмодзи — умеренно

Для changelog-бота можно сделать стабильные категории:

```text
🆕 New / Added
🔧 Fixed
⚡ Improved
⚠️ Breaking
🔐 Security
🧹 Removed
📦 Dependencies
```

Но не надо ставить эмодзи в каждую строку. Лучше так:

```html
<b>⚠️ Breaking changes</b>
• Изменён формат конфига
• Старое поле <code>apiKey</code> больше не поддерживается
```

## Очень важное: делай digest, а не простую копию

Если changelog большой, Telegram-пост должен быть **сводкой**, а не полным Markdown.

Хорошая схема:

```text
1. Заголовок: продукт + версия/дата
2. 3–5 главных изменений
3. Секции Added / Fixed / Changed / Breaking
4. Кнопка "Открыть changelog"
```

Плохая схема:

```text
Вставить весь markdown-файл целиком
```

Почему: длинные посты плохо читаются, ломаются на лимите 4096 символов, а code blocks и таблицы на телефоне выглядят плохо.

## Что делать с длинными changelog

Я бы ввёл лимиты:

```ts
const MAX_MESSAGE_LENGTH = 3900; // запас до 4096
const MAX_ITEMS_PER_SECTION = 7;
const MAX_ITEM_LENGTH = 250;
const MAX_SECTIONS = 5;
```

Если пост длиннее:

```html
<b>🆕 OpenCode — v1.2.3</b>

<b>Коротко</b>
• ...
• ...
• ...

<i>Показаны главные изменения. Полный список — по кнопке ниже.</i>
```

И кнопка:

```ts
reply_markup: {
  inline_keyboard: [
    [{ text: "Полный changelog", url: sourceUrl }]
  ]
}
```

## Как обрабатывать разные источники

Для каждого источника лучше хранить настройки оформления:

```ts
type SourceConfig = {
  id: string;
  title: string;
  url: string;
  productEmoji?: string;
  format: "changelog-md" | "github-release" | "web-md";
  sectionAliases?: Record<string, string>;
};
```

Например:

```ts
const sectionAliases = {
  "added": "🆕 Added",
  "new": "🆕 Added",
  "features": "🆕 Added",
  "fixed": "🔧 Fixed",
  "bug fixes": "🔧 Fixed",
  "changed": "⚡ Changed",
  "improvements": "⚡ Improved",
  "breaking": "⚠️ Breaking changes",
  "security": "🔐 Security",
};
```

Тогда бот будет приводить разные changelog-файлы к одному стилю.

## Минимальный HTML escape

Любой текст из Markdown надо экранировать перед вставкой в HTML:

```ts
function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
```

Важно: экранируй **только текст**, а не весь готовый HTML. То есть так:

```ts
const title = escapeHtml(parsed.title);

const html = `<b>🆕 ${title}</b>`;
```

А не так:

```ts
const html = escapeHtml(`<b>🆕 ${title}</b>`);
```

## Хороший итоговый шаблон

Я бы сделал базовый шаблон таким:

```html
<b>🆕 {product} — {versionOrTitle}</b>
<i>{date}</i>

<b>Коротко</b>
• {summaryItem1}
• {summaryItem2}
• {summaryItem3}

<b>{sectionTitle1}</b>
• {item}
• {item}

<b>{sectionTitle2}</b>
• {item}
• {item}

<i>Полный список изменений — по кнопке ниже.</i>
```

Для GitHub release / changelog лучше в конце не пихать длинную ссылку текстом, а делать inline-кнопку. Ссылку в тексте оставляй только если пост уходит в канал без кнопок или нужна явная видимость URL.

## Мой рекомендуемый вариант для твоего бота

Сделай три уровня форматирования:

**Compact** — только заголовок, 3–5 главных пунктов, кнопка.
**Normal** — заголовок, short summary, секции Added/Fixed/Changed, кнопка.
**Full** — почти весь changelog, но с лимитом и обрезкой.

По умолчанию для чатов лучше короткая версия. Для очень шумных продуктов — короткая (compact), для личного тестового чата можно включить полную (full).

Главное правило: **Telegram-пост должен быть не Markdown-рендером, а читабельной карточкой обновления**. Тогда даже большие changelog будут нормально восприниматься в чате.

## Краткая и полная версии: конкретные шаблоны

Ниже два рабочих шаблона, которые хорошо смотрятся в Telegram при `parse_mode: "HTML"`.

### Краткая версия (compact)

```html
<b>🆕 {product} — {versionOrTitle}</b>
<i>{date}</i>

<b>Коротко</b>
• {summaryItem1}
• {summaryItem2}
• {summaryItem3}

<i>Полный список изменений — по кнопке ниже.</i>
```

Что получается:

- **🆕 App — v2.4.0**
- _Обновление от 16 мая 2026_
- **Коротко**
- • Исправлена ошибка на Windows
- • Добавлена новая опция авторизации
- • Повышена производительность
- _Полный список изменений — по кнопке ниже._

### Полная версия (full)

```html
<b>🆕 {product} — {versionOrTitle}</b>
<i>{date}</i>

<b>Коротко</b>
• {summaryItem1}
• {summaryItem2}
• {summaryItem3}

<b>Added</b>
• {item}
• {item}

<b>Fixed</b>
• {item}
• {item}

<b>Changed</b>
• {item}

<i>Полный список изменений — по кнопке ниже.</i>
```

Как упростить рендер:

- Если секций меньше — просто не показывай пустые блоки.
- Ограничь общий текст лимитом `MAX_MESSAGE_LENGTH = 3900` с запасом.
- Для длинного списка секций лучше обрезай до `MAX_ITEMS_PER_SECTION` и добавляй итоговую фразу о полном источнике.

### Рекомендация по использованию

- Короткая: лучше для каналов и сводок.
- Полная: для теста и редких крупных релизов, где важны детали.
- Для обоих вариантов ставь кнопку `Открыть changelog`, чтобы не тащить весь список в сообщение.

[1]: https://core.telegram.org/bots/api "Telegram Bot API"
