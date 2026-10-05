"""Comment tools: threads with their replies, replying, editing the channel's replies and moderation."""

import re
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from .approval import ApprovalGate
from .client import YouTubeClient, YouTubeError

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
PUBLISH = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)
HIDE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True)

MODERATION_STATUS = {"publish": "published", "hold": "heldForReview", "reject": "rejected"}
MODERATION_VERB = {
    "publish": "Опубликовать (одобрить)",
    "hold": "Отправить на проверку",
    "reject": "Скрыть с видео",
}

VideoId = Annotated[str | None, Field(description="Одно видео; без него — весь канал")]
Show = Annotated[
    Literal["awaiting", "unanswered", "all"],
    Field(
        description=(
            "awaiting — последнее слово в ветке не за каналом (новые комментарии и уточняющие вопросы); "
            "unanswered — канал в ветке ни разу не ответил; all — все ветки"
        )
    ),
]
Since = Annotated[
    str | None, Field(description="Только ветки с активностью начиная с этой даты: YYYY-MM-DD или ISO 8601")
]
Status = Annotated[
    Literal["published", "heldForReview", "likelySpam"],
    Field(description="published — опубликованные; heldForReview и likelySpam — очереди проверки в Студии"),
]
Limit = Annotated[int, Field(ge=1, le=500, description="Сколько веток вернуть максимум")]
ThreadId = Annotated[str, Field(description="thread_id ветки (id комментария верхнего уровня)", min_length=1)]
CommentId = Annotated[str, Field(description="id комментария или ответа самого канала", min_length=1)]
CommentIds = Annotated[list[str], Field(description="id комментариев зрителей", min_length=1, max_length=50)]
ReplyText = Annotated[str, Field(description="Текст ответа, обычный текст", min_length=1, max_length=10000)]
Action = Annotated[
    Literal["publish", "hold", "reject"],
    Field(
        description=(
            "publish — одобрить (например, из heldForReview или likelySpam); hold — отправить на проверку; "
            "reject — скрыть с видео"
        )
    ),
]
BanAuthor = Annotated[bool, Field(description="Только с reject: скрывать и будущие комментарии этих авторов")]


def comment_view(comment: dict[str, Any], my_channel_id: str) -> dict[str, Any]:
    snippet = comment["snippet"]
    view = {
        "id": comment["id"],
        "author": snippet.get("authorDisplayName"),
        "mine": snippet.get("authorChannelId", {}).get("value") == my_channel_id,
        "text": snippet.get("textDisplay"),
        "likes": snippet.get("likeCount", 0),
        "published": snippet.get("publishedAt"),
    }
    if snippet.get("updatedAt") not in (None, snippet.get("publishedAt")):
        view["edited"] = snippet["updatedAt"]
    return view


def thread_view(item: dict[str, Any], replies: list[dict[str, Any]], my_channel_id: str) -> dict[str, Any]:
    snippet = item["snippet"]
    top = comment_view(snippet["topLevelComment"], my_channel_id)
    replies = sorted(replies, key=lambda reply: reply["published"] or "")
    video_id = snippet.get("videoId")
    view: dict[str, Any] = {
        "thread_id": top["id"],
        "video_id": video_id,
        "author": top["author"],
        "text": top["text"],
        "likes": top["likes"],
        "published": top["published"],
    }
    if top["mine"]:
        view["mine"] = True
    if "edited" in top:
        view["edited"] = top["edited"]
    view.update(
        reply_count=snippet.get("totalReplyCount", len(replies)),
        answered=any(reply["mine"] for reply in replies),
        last_word_mine=(replies[-1] if replies else top)["mine"],
        can_reply=snippet.get("canReply", True),
        replies=replies,
        url=comment_url(video_id, top["id"]),
    )
    return view


def comment_url(video_id: str | None, comment_id: str | None) -> str | None:
    if not video_id or not comment_id:
        return None
    return f"https://www.youtube.com/watch?v={video_id}&lc={comment_id}"


def last_activity(thread: dict[str, Any]) -> str:
    return max([thread["published"] or ""] + [reply["published"] or "" for reply in thread["replies"]])


def utc_stamp(value: str) -> str:
    """'2026-10-01' or any ISO 8601 time -> '2026-10-01T00:00:00Z', comparable with publishedAt strings."""
    value = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value + "T00:00:00Z"
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise YouTubeError(f"since: не понимаю дату {value!r}, нужен формат YYYY-MM-DD или ISO 8601") from None
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def shorten(text: str | None, limit: int = 300) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def all_replies(client: YouTubeClient, parent_id: str, my_channel_id: str) -> list[dict[str, Any]]:
    replies: list[dict[str, Any]] = []
    page_token = None
    while True:
        page = await client.data(
            "comments", part="snippet", parentId=parent_id, maxResults=100, textFormat="plainText", pageToken=page_token
        )
        replies += [comment_view(comment, my_channel_id) for comment in page.get("items", [])]
        page_token = page.get("nextPageToken")
        if not page_token:
            return replies


async def load_thread(client: YouTubeClient, item: dict[str, Any], my_channel_id: str) -> dict[str, Any]:
    # commentThreads returns only some of the replies; the rest are needed to know who spoke last.
    replies = [comment_view(comment, my_channel_id) for comment in item.get("replies", {}).get("comments", [])]
    if item["snippet"].get("totalReplyCount", 0) > len(replies):
        replies = await all_replies(client, item["snippet"]["topLevelComment"]["id"], my_channel_id)
    return thread_view(item, replies, my_channel_id)


async def video_titles(client: YouTubeClient, video_ids: list[str | None]) -> dict[str, str]:
    return {video_id: video["snippet"]["title"] for video_id, video in (await client.videos(video_ids)).items()}


def register_comment_tools(mcp: FastMCP, client: YouTubeClient, gate: ApprovalGate) -> None:
    @mcp.tool(annotations=READ)
    async def comments(
        video_id: VideoId = None,
        show: Show = "awaiting",
        since: Since = None,
        status: Status = "published",
        limit: Limit = 50,
    ) -> dict[str, Any]:
        """Ветки комментариев к видео канала, от новых к старым, с ответами и названием видео.

        mine=true помечает сообщения самого канала. Ответить в ветку — инструментом reply по её thread_id.
        """
        my_id = (await client.channel())["id"]
        params: dict[str, Any] = {
            "part": "snippet,replies",
            "maxResults": 100,
            "order": "time",
            "textFormat": "plainText",
            "moderationStatus": status,
        }
        if video_id:
            params["videoId"] = video_id
        else:
            params["allThreadsRelatedToChannelId"] = my_id
        cutoff = utc_stamp(since) if since else None
        threads: list[dict[str, Any]] = []
        page_token = None
        while len(threads) < limit:
            page = await client.data("commentThreads", pageToken=page_token, **params)
            fresh_on_page = False
            for item in page.get("items", []):
                thread = await load_thread(client, item, my_id)
                if cutoff and last_activity(thread) < cutoff:
                    continue
                fresh_on_page = True
                if show == "unanswered" and (thread["answered"] or thread.get("mine")):
                    continue
                if show == "awaiting" and thread["last_word_mine"]:
                    continue
                threads.append(thread)
                if len(threads) == limit:
                    break
            page_token = page.get("nextPageToken")
            # Threads come newest first: a page with nothing recent means the rest is older still.
            if not page_token or (cutoff and not fresh_on_page):
                break
        titles = await video_titles(client, [thread["video_id"] for thread in threads])
        for thread in threads:
            thread["video_title"] = titles.get(thread["video_id"])
        result: dict[str, Any] = {"count": len(threads), "threads": threads}
        if len(threads) == limit:
            result["limit_reached"] = True
        return result

    @mcp.tool(annotations=READ)
    async def comment_thread(thread_id: ThreadId) -> dict[str, Any]:
        """Одна ветка комментариев со всеми ответами."""
        my_id = (await client.channel())["id"]
        page = await client.data("commentThreads", part="snippet,replies", id=thread_id, textFormat="plainText")
        if not page.get("items"):
            raise YouTubeError(thread_not_found(thread_id))
        thread = await load_thread(client, page["items"][0], my_id)
        thread["video_title"] = (await video_titles(client, [thread["video_id"]])).get(thread["video_id"])
        return thread

    @mcp.tool(annotations=PUBLISH)
    async def reply(thread_id: ThreadId, text: ReplyText, ctx: Context) -> dict[str, Any]:
        """Ответить в ветку комментариев от имени канала (50 единиц квоты).

        Перед публикацией пользователь видит предпросмотр и подтверждает его.
        """
        page = await client.data("commentThreads", part="snippet", id=thread_id, textFormat="plainText")
        if not page.get("items"):
            raise YouTubeError(thread_not_found(thread_id))
        snippet = page["items"][0]["snippet"]
        top = snippet["topLevelComment"]["snippet"]
        video_id = snippet.get("videoId")
        title = (await video_titles(client, [video_id])).get(video_id or "", "без названия")
        summary = (
            f"Ответ от имени канала на комментарий {top.get('authorDisplayName')} к видео «{title}»:\n"
            f"«{shorten(top.get('textDisplay'))}»\n\nТекст ответа:\n{text}"
        )

        async def run() -> dict[str, Any]:
            created = await client.data(
                "comments",
                method="POST",
                part="snippet",
                body={"snippet": {"parentId": thread_id, "textOriginal": text}},
            )
            return {"id": created.get("id"), "url": comment_url(video_id, created.get("id"))}

        return await gate.request(ctx, summary, run)

    @mcp.tool(annotations=PUBLISH)
    async def edit_reply(comment_id: CommentId, text: ReplyText, ctx: Context) -> dict[str, Any]:
        """Заменить текст комментария или ответа самого канала (50 единиц квоты).

        Перед изменением пользователь видит старый и новый текст и подтверждает замену.
        """
        my_id = (await client.channel())["id"]
        page = await client.data("comments", part="snippet", id=comment_id, textFormat="plainText")
        if not page.get("items"):
            raise YouTubeError(f"Комментарий {comment_id} не найден: его удалили или id неверный.")
        current = comment_view(page["items"][0], my_id)
        if not current["mine"]:
            raise YouTubeError("Изменять можно только комментарии самого канала.")
        summary = f"Изменение ответа канала {comment_id}.\n\nБыло:\n{current['text']}\n\nСтанет:\n{text}"

        async def run() -> dict[str, Any]:
            updated = await client.data(
                "comments", method="PUT", part="snippet", body={"id": comment_id, "snippet": {"textOriginal": text}}
            )
            return {"id": updated.get("id", comment_id), "text": updated.get("snippet", {}).get("textDisplay", text)}

        return await gate.request(ctx, summary, run)

    @mcp.tool(annotations=HIDE)
    async def moderate(
        comment_ids: CommentIds, action: Action, ctx: Context, ban_author: BanAuthor = False
    ) -> dict[str, Any]:
        """Сменить статус модерации комментариев зрителей (50 единиц квоты за вызов).

        Перед изменением пользователь видит список комментариев и действие и подтверждает его.
        """
        if ban_author and action != "reject":
            raise YouTubeError("ban_author работает только вместе с action=reject.")
        page = await client.data("comments", part="snippet", id=",".join(comment_ids), textFormat="plainText")
        found = {comment["id"]: comment["snippet"] for comment in page.get("items", [])}
        lines = [
            f"• {found[cid].get('authorDisplayName')}: «{shorten(found[cid].get('textDisplay'), 200)}»"
            if cid in found
            else f"• {cid} (текст недоступен)"
            for cid in comment_ids
        ]
        summary = f"{MODERATION_VERB[action]}, комментариев: {len(comment_ids)}\n" + "\n".join(lines)
        if ban_author:
            summary += "\n\nАвторы будут заблокированы: их новые комментарии на канале тоже будут скрываться."
        status = MODERATION_STATUS[action]

        async def run() -> dict[str, Any]:
            await client.data(
                "comments/setModerationStatus",
                method="POST",
                id=",".join(comment_ids),
                moderationStatus=status,
                banAuthor="true" if ban_author else None,
            )
            return {"comments": len(comment_ids), "status": status, "banned": ban_author}

        return await gate.request(ctx, summary, run)


def thread_not_found(thread_id: str) -> str:
    return (
        f"Ветка {thread_id} не найдена: её удалили, она ждёт проверки или это id ответа, "
        "а не комментария верхнего уровня."
    )
