# youtube-mcp-server

[![CI](https://github.com/a-shipilo/youtube-mcp-server/actions/workflows/ci.yml/badge.svg)](https://github.com/a-shipilo/youtube-mcp-server/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

MCP-сервер для **вашего собственного YouTube-канала**: Claude читает комментарии, готовит ответы,
модерирует и смотрит аналитику. Ответы и модерация выполняются только после вашего подтверждения.
Сервер запускается через `uvx` прямо из GitHub, устанавливать ничего не нужно.

*English: an open-source MCP server for your own YouTube channel: read comment threads, reply and moderate
(every write needs your confirmation) and query YouTube Analytics. Run it with
`uvx --from git+https://github.com/a-shipilo/youtube-mcp-server youtube-mcp-server`.*

## Возможности

| Инструмент | Что делает | Квота |
|---|---|---|
| `channel` | канал: название, @handle, подписчики, просмотры, число видео | 1 |
| `videos` | последние загрузки (включая Shorts): длительность, доступ, просмотры, лайки, комментарии | 2+ |
| `comments` | ветки комментариев к видео или всему каналу, с ответами и названием видео | 1 за страницу |
| `comment_thread` | одна ветка со всеми ответами | 1–2 |
| `reply` | ответ в ветку от имени канала, **с подтверждением** | 50 |
| `edit_reply` | правка своего ответа, **с подтверждением** | 50 |
| `moderate` | одобрить, отправить на проверку или скрыть комментарии, по желанию заблокировать авторов, **с подтверждением** | 50 |
| `analytics` | готовые отчёты: итоги, по дням, топ видео, источники трафика, поисковые запросы, удержание, страны, устройства, демография | 1 |
| `analytics_query` | произвольный `reports.query` к YouTube Analytics API | 1 |
| `confirm_action`, `cancel_action` | подтвердить или отменить подготовленную операцию | — |

- `comments` по умолчанию показывает ветки, где **последнее слово не за каналом**: новые комментарии
  и уточняющие вопросы в старых ветках. `show=unanswered` — только ветки без ответа канала, `show=all` — все.
  `status=heldForReview` или `likelySpam` открывает очереди проверки из Студии.
- Отвечать можно только в ветку (на комментарий верхнего уровня). Чтобы обратиться к участнику ветки,
  начните ответ с его @имени.
- Лайкать комментарии и ставить им сердечки YouTube API не позволяет.
- Квота YouTube API — 10 000 единиц в сутки, то есть около 200 ответов. Чтение почти бесплатное.
- Данные аналитики отстают от текущей даты на 2–3 дня.

### Подтверждение ответов и модерации

`reply`, `edit_reply` и `moderate` ничего не публикуют сразу: сначала вы видите предпросмотр
(чей комментарий, к какому видео, текст ответа или список комментариев).

- В клиентах с поддержкой elicitation (Claude Code) появляется диалог подтверждения.
- В остальных (Claude Desktop) инструмент возвращает предпросмотр и одноразовый `confirmation_id`,
  Claude показывает предпросмотр в чате и вызывает `confirm_action` только после вашего согласия.
  Неподтверждённая операция истекает через 15 минут.

Режим задаёт `YOUTUBE_CONFIRM_MODE`: `auto` (по умолчанию: диалог, если клиент умеет, иначе `confirmation_id`),
`elicitation` или `token`.

## Установка

Нужен установленный [uv](https://docs.astral.sh/uv/getting-started/installation/)
(`brew install uv` на macOS).

### 1. Создайте OAuth-клиент в Google Cloud

1. Откройте [console.cloud.google.com](https://console.cloud.google.com) и создайте проект.
2. **APIs & Services → Library**: включите **YouTube Data API v3** и **YouTube Analytics API**.
3. **Google Auth Platform → Get started**: название приложения, ваша почта, тип **External**.
4. **Audience → Publish app**. В статусе *Testing* Google отзывает доступ через 7 дней.
   Для личного использования проверка приложения не нужна: при входе Google просто предупредит,
   что приложение не проверено.
5. **Clients → Create client**, тип **Desktop app**. Скачайте JSON и положите его в папку сервера:
   `~/.config/youtube-mcp-server/` (или в свою, см. `YOUTUBE_MCP_DIR`).

### 2. Разрешите доступ к каналу

```bash
uvx --from git+https://github.com/a-shipilo/youtube-mcp-server youtube-mcp-server auth
```

Откроется браузер. Выберите аккаунт канала (если канал на бренд-аккаунте, выберите именно его),
на экране «Google hasn't verified this app» нажмите **Advanced → Go to …** и разрешите доступ.
Команда сохранит `token.json` рядом с JSON клиента и покажет канал.

Проверить доступ позже:

```bash
uvx --from git+https://github.com/a-shipilo/youtube-mcp-server youtube-mcp-server check
```

### 3. Подключите сервер

#### Claude Code

```bash
claude mcp add youtube -s user -- uvx --from git+https://github.com/a-shipilo/youtube-mcp-server@v0.1.0 youtube-mcp-server
```

С папкой не по умолчанию добавьте `-e YOUTUBE_MCP_DIR=/путь/к/папке` перед `--`.

#### Claude Desktop

1. Откройте **Settings → Developer → Edit Config**. Откроется файл `claude_desktop_config.json`.
2. Добавьте сервер в `mcpServers`:

```json
{
  "mcpServers": {
    "youtube": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/a-shipilo/youtube-mcp-server@v0.1.0",
        "youtube-mcp-server"
      ]
    }
  }
}
```

3. Полностью перезапустите Claude Desktop.

`@v0.1.0` фиксирует версию. Чтобы всегда брать последнюю версию из `main`, уберите `@v0.1.0`.
Для обновления добавьте в `args` перед `--from` флаг `--refresh`.

Если в логах `spawn uvx ENOENT`, укажите полный путь к uvx (узнать его: `which uvx`),
например `"command": "/opt/homebrew/bin/uvx"`.

## Настройки

| Переменная | По умолчанию | Описание |
|---|---|---|
| `YOUTUBE_MCP_DIR` | `~/.config/youtube-mcp-server` | папка с JSON OAuth-клиента и `token.json` |
| `YOUTUBE_CONFIRM_MODE` | `auto` | как подтверждаются ответы и модерация: `auto`, `elicitation`, `token` |

Без авторизации сервер всё равно стартует, а инструменты вернут понятную ошибку с командой `auth`.

## Команды

| Команда | Что делает |
|---|---|
| `youtube-mcp-server` | запускает MCP-сервер (stdio) |
| `youtube-mcp-server auth [--client-secret PATH]` | разрешает доступ к каналу в браузере и сохраняет его |
| `youtube-mcp-server check` | показывает канал, к которому есть доступ |
| `youtube-mcp-server --version` | версия |

## Privacy

*Конфиденциальность.* Сервер работает на вашем компьютере, у него нет своего бэкенда, аналитики или телеметрии.

- **Куда уходят запросы.** Только в Google: YouTube Data API, YouTube Analytics API и OAuth
  (`googleapis.com`, `youtubeanalytics.googleapis.com`, `oauth2.googleapis.com`). Никаким третьим
  сторонам сервер данные канала не передаёт.
- **Где хранится доступ.** Refresh token лежит только локально, в `token.json` в `YOUTUBE_MCP_DIR`,
  с правами `600` (папка — `700`). Access token живёт в памяти процесса и на диск не пишется.
  Сервер не логирует комментарии, ответы и статистику.
- **Что видит MCP-клиент.** Результаты инструментов (комментарии, статистика) получает клиент, который
  их вызвал, например Claude, и они попадают в ваш разговор с моделью по правилам этого клиента.
- **Права.** Сервер запрашивает `youtube.force-ssl` (чтение и запись комментариев канала)
  и `yt-analytics.readonly`. Публикация и модерация выполняются только после вашего подтверждения.
- **Как отозвать доступ.** Удалите `token.json` и отзовите доступ приложения на
  [myaccount.google.com/permissions](https://myaccount.google.com/permissions).

*English: the server runs locally and talks only to Google APIs; it has no backend or telemetry. The refresh
token is stored only on your machine (`token.json`, mode 600). Tool results go to the MCP client that called
the tool. Revoke access at myaccount.google.com/permissions.*

## Разработка

```bash
git clone https://github.com/a-shipilo/youtube-mcp-server.git
cd youtube-mcp-server
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

Локальная отладка в [MCP Inspector](https://github.com/modelcontextprotocol/inspector):

```bash
npx @modelcontextprotocol/inspector uv run youtube-mcp-server
```

## Лицензия

[MIT](LICENSE)
