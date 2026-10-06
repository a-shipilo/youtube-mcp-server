"""Server entry point: configuration from environment variables, the tools and CLI helpers."""

import argparse
import asyncio
import logging
import os
import re
import sys
from pathlib import Path
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from . import __version__
from .analytics import register_analytics_tools
from .approval import APPROVAL_MODES, ApprovalGate
from .auth import authorize
from .client import DEFAULT_DIR, ENV_CREDENTIALS, YouTubeClient, YouTubeError, credentials_from_env, load_token
from .comments import READ, register_comment_tools
from .schema import PlainSchemaFastMCP

INSTRUCTIONS = """\
Инструменты для собственного YouTube-канала пользователя: видео, комментарии, ответы, модерация и аналитика.
Ответы публикуются от имени канала пользователя. reply, edit_reply и moderate сначала показывают пользователю
предпросмотр и выполняются только после его согласия.
Отвечайте на языке комментария. Ответ всегда уходит в ветку (thread_id); чтобы обратиться к участнику ветки,
начните текст с его @имени. Лайкать комментарии и ставить им сердечки YouTube API не умеет.
Квота YouTube API — 10 000 единиц в сутки: чтение стоит около 1 единицы за страницу, каждый ответ, правка
или модерация — 50.
"""

Limit = Annotated[int, Field(ge=1, le=200, description="Сколько последних загрузок вернуть")]
ConfirmationId = Annotated[str, Field(description="confirmation_id из ответа инструмента", min_length=1)]

logger = logging.getLogger(__name__)


def seconds(duration: str | None) -> int | None:
    """ISO 8601 duration as YouTube writes it (PT1H2M5S, P1DT2H) -> seconds."""
    match = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", duration or "")
    if not match:
        return None
    days, hours, minutes, secs = (int(part or 0) for part in match.groups())
    return ((days * 24 + hours) * 60 + minutes) * 60 + secs


def video_view(video: dict[str, Any]) -> dict[str, Any]:
    stats = video.get("statistics", {})
    return {
        "id": video["id"],
        "title": video["snippet"]["title"],
        "published": video["snippet"]["publishedAt"],
        "seconds": seconds(video.get("contentDetails", {}).get("duration")),
        "privacy": video.get("status", {}).get("privacyStatus"),
        "views": int(stats.get("viewCount", 0)),
        "likes": int(stats.get("likeCount", 0)),
        "comments": int(stats.get("commentCount", 0)),
    }


def create_server(client: YouTubeClient, gate: ApprovalGate) -> FastMCP:
    mcp = PlainSchemaFastMCP("youtube", instructions=INSTRUCTIONS, log_level="WARNING")

    @mcp.tool(annotations=READ)
    async def channel() -> dict[str, Any]:
        """Авторизованный канал: id, название, @handle, подписчики, просмотры, число видео."""
        found = await client.channel()
        stats = found.get("statistics", {})
        return {
            "id": found["id"],
            "title": found["snippet"]["title"],
            "handle": found["snippet"].get("customUrl"),
            "subscribers": int(stats.get("subscriberCount", 0)),
            "views": int(stats.get("viewCount", 0)),
            "videos": int(stats.get("videoCount", 0)),
            "url": f"https://www.youtube.com/channel/{found['id']}",
        }

    @mcp.tool(annotations=READ)
    async def videos(limit: Limit = 20) -> dict[str, Any]:
        """Последние загрузки канала, включая Shorts, от новых к старым: название, дата публикации, длительность
        в секундах, доступ (public/unlisted/private), просмотры, лайки и число комментариев."""
        uploads = (await client.channel())["contentDetails"]["relatedPlaylists"]["uploads"]
        ids: list[str] = []
        page_token = None
        while len(ids) < limit:
            page = await client.data(
                "playlistItems", part="contentDetails", playlistId=uploads, maxResults=50, pageToken=page_token
            )
            ids += [item["contentDetails"]["videoId"] for item in page.get("items", [])]
            page_token = page.get("nextPageToken")
            if not page_token:
                break
        found = await client.videos(ids[:limit])
        items = sorted(
            (video_view(found[video_id]) for video_id in ids[:limit] if video_id in found),
            key=lambda video: video["published"],
            reverse=True,
        )
        return {"count": len(items), "videos": items}

    register_comment_tools(mcp, client, gate)
    register_analytics_tools(mcp, client)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True))
    async def confirm_action(confirmation_id: ConfirmationId) -> dict[str, Any]:
        """Выполнить подготовленную операцию. Вызывайте ТОЛЬКО после того, как пользователь увидел
        preview и явно согласился выполнить именно эту операцию."""
        return await gate.confirm(confirmation_id)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))
    def cancel_action(confirmation_id: ConfirmationId) -> dict[str, Any]:
        """Отменить подготовленную операцию, если пользователь отказался."""
        return gate.cancel(confirmation_id)

    return mcp


def config_dir() -> Path:
    return Path(os.environ.get("YOUTUBE_MCP_DIR", "").strip() or DEFAULT_DIR).expanduser()


def build_from_env() -> FastMCP:
    mode = os.environ.get("YOUTUBE_CONFIRM_MODE", "auto").strip().lower() or "auto"
    if mode not in APPROVAL_MODES:
        raise SystemExit(f"YOUTUBE_CONFIRM_MODE: допустимые значения — {', '.join(APPROVAL_MODES)}")
    # Without a token the server still starts: tools then explain how to authorise.
    client = YouTubeClient(config_dir() / "token.json", credentials=credentials_from_env())
    return create_server(client, ApprovalGate(mode))  # type: ignore[arg-type]


async def show_channel() -> int:
    async with YouTubeClient(config_dir() / "token.json", credentials=credentials_from_env()) as client:
        try:
            found = await client.channel()
        except YouTubeError as exc:
            print(exc, file=sys.stderr)
            return 1
    stats = found.get("statistics", {})
    print(f"Канал: {found['snippet']['title']} ({found['snippet'].get('customUrl') or found['id']})")
    print(
        f"Подписчики: {stats.get('subscriberCount', '—')}, видео: {stats.get('videoCount', '—')}, "
        f"просмотры: {stats.get('viewCount', '—')}"
    )
    return 0


def run_auth(client_secret: Path | None) -> int:
    directory = config_dir()
    try:
        token = authorize(directory, client_secret)
    except YouTubeError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"Доступ сохранён в {token}")
    print("Значения для настроек плагина Claude Code покажет: youtube-mcp-server credentials")
    return asyncio.run(show_channel())


def show_credentials() -> int:
    """Print the saved access as environment variables: for the plugin's settings or a password manager."""
    try:
        token = load_token(config_dir() / "token.json")
    except YouTubeError as exc:
        print(exc, file=sys.stderr)
        return 1
    for key, var in ENV_CREDENTIALS.items():
        print(f"{var}={token[key]}")
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="youtube-mcp-server",
        description="MCP-сервер для вашего YouTube-канала: комментарии, ответы, модерация и аналитика. Без команды "
        "запускает сервер (stdio). Доступ к каналу хранится в YOUTUBE_MCP_DIR (по умолчанию "
        f"{DEFAULT_DIR}); подтверждение записи настраивает YOUTUBE_CONFIRM_MODE.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", title="команды")
    commands.add_parser("serve", help="запустить MCP-сервер (по умолчанию)")
    auth = commands.add_parser("auth", help="разрешить доступ к каналу в браузере и сохранить его в YOUTUBE_MCP_DIR")
    auth.add_argument(
        "--client-secret",
        type=Path,
        help="JSON OAuth-клиента Google типа Desktop app (по умолчанию client_secret*.json из YOUTUBE_MCP_DIR)",
    )
    commands.add_parser("check", help="показать канал, к которому есть доступ")
    commands.add_parser(
        "credentials",
        help="вывести сохранённый доступ как YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET и YOUTUBE_REFRESH_TOKEN "
        "(для настроек плагина или менеджера паролей)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.command == "auth":
        raise SystemExit(run_auth(args.client_secret))
    if args.command == "check":
        raise SystemExit(asyncio.run(show_channel()))
    if args.command == "credentials":
        raise SystemExit(show_credentials())
    build_from_env().run("stdio")


__all__ = ["create_server", "build_from_env", "main"]
