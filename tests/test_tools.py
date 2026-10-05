import mcp.types as types

from .conftest import MY_CHANNEL, comment, error_text, mine, payload, thread, video

THREAD_ID = "Ugx1"


def elicitation_answer(approve: bool | None, seen: list[str] | None = None):
    async def callback(context, params: types.ElicitRequestParams):
        if seen is not None:
            seen.append(params.message)
        if approve is None:
            return types.ElicitResult(action="cancel")
        return types.ElicitResult(action="accept", content={"approve": approve})

    return callback


def serve_reply_target(fake) -> None:
    fake.on("GET", "commentThreads", {"items": [thread(comment(THREAD_ID, "2026-10-01T10:00:00Z", text="Где брал?"))]})
    fake.on("GET", "videos", {"items": [video("V1", "Обзор шин")]})
    fake.on("POST", "comments", {"id": "Ugx1.reply", "snippet": {"videoId": "V1"}})


async def test_write_tools_are_marked_as_non_read_only(connect):
    async with connect() as session:
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
    for name in ("channel", "videos", "comments", "comment_thread", "analytics", "analytics_query"):
        assert tools[name].annotations.readOnlyHint is True, name
    for name in ("reply", "edit_reply", "moderate", "confirm_action"):
        assert tools[name].annotations.readOnlyHint is False, name
    assert tools["moderate"].annotations.destructiveHint is True


async def test_optional_parameters_are_plain_and_not_required(connect):
    async with connect() as session:
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
    schema = tools["comments"].inputSchema
    assert "required" not in schema
    assert schema["properties"]["video_id"]["type"] == "string"
    assert "default" not in schema["properties"]["show"]
    assert 'По умолчанию "awaiting"' in schema["properties"]["show"]["description"]


async def test_tools_explain_how_to_authorise(connect, token_file):
    token_file.unlink()
    async with connect() as session:
        text = error_text(await session.call_tool("channel", {}))
    assert "не авторизован" in text
    assert "youtube-mcp-server auth" in text


async def test_channel(connect):
    async with connect() as session:
        result = payload(await session.call_tool("channel", {}))
    assert result == {
        "id": MY_CHANNEL,
        "title": "Мой канал",
        "handle": "@me",
        "subscribers": 1200,
        "views": 50000,
        "videos": 12,
        "url": f"https://www.youtube.com/channel/{MY_CHANNEL}",
    }


async def test_videos_newest_first_with_length_in_seconds(connect, fake):
    fake.on("GET", "playlistItems", {"items": [{"contentDetails": {"videoId": v}} for v in ("V1", "V2")]})
    fake.on(
        "GET",
        "videos",
        {
            "items": [
                video("V1", "Старое", "2026-08-01T10:00:00Z"),
                video("V2", "Новое", "2026-09-20T10:00:00Z", "PT45S"),
            ]
        },
    )
    async with connect() as session:
        result = payload(await session.call_tool("videos", {"limit": 5}))
    assert [v["title"] for v in result["videos"]] == ["Новое", "Старое"]
    assert result["videos"][0]["seconds"] == 45
    assert result["videos"][1]["seconds"] == 725
    assert fake.called("GET", "playlistItems")[0][0]["playlistId"] == "UUme"


def conversation_threads():
    return [
        thread(comment("new", "2026-10-03T10:00:00Z")),
        thread(comment("done", "2026-10-02T10:00:00Z"), [mine("done.1", "2026-10-02T12:00:00Z")]),
        thread(
            comment("followup", "2026-10-01T10:00:00Z"),
            [comment("f.2", "2026-10-01T14:00:00Z", text="А размер?"), mine("f.1", "2026-10-01T12:00:00Z")],
        ),
        thread(mine("own", "2026-09-30T10:00:00Z", text="Закреп: ссылки в описании")),
    ]


async def test_comments_awaiting_shows_new_comments_and_follow_ups(connect, fake):
    fake.on("GET", "commentThreads", {"items": conversation_threads()})
    fake.on("GET", "videos", {"items": [video("V1", "Обзор шин")]})
    async with connect() as session:
        awaiting = payload(await session.call_tool("comments", {}))
        unanswered = payload(await session.call_tool("comments", {"show": "unanswered"}))
        everything = payload(await session.call_tool("comments", {"show": "all"}))
    assert [t["thread_id"] for t in awaiting["threads"]] == ["new", "followup"]
    assert [t["thread_id"] for t in unanswered["threads"]] == ["new"]
    assert len(everything["threads"]) == 4
    followup = awaiting["threads"][1]
    assert [r["id"] for r in followup["replies"]] == ["f.1", "f.2"]
    assert followup["answered"] is True and followup["last_word_mine"] is False
    assert followup["video_title"] == "Обзор шин"
    assert followup["url"] == "https://www.youtube.com/watch?v=V1&lc=followup"
    params = fake.called("GET", "commentThreads")[0][0]
    assert params["allThreadsRelatedToChannelId"] == MY_CHANNEL
    assert params["moderationStatus"] == "published"


async def test_comments_for_one_video_from_the_review_queue(connect, fake):
    fake.on("GET", "commentThreads", {"items": []})
    async with connect() as session:
        payload(await session.call_tool("comments", {"video_id": "V9", "status": "heldForReview"}))
    params = fake.called("GET", "commentThreads")[0][0]
    assert params["videoId"] == "V9"
    assert "allThreadsRelatedToChannelId" not in params
    assert params["moderationStatus"] == "heldForReview"


async def test_comments_fetch_replies_the_thread_list_left_out(connect, fake):
    shown = [comment(f"r{i}", f"2026-10-01T1{i}:00:00Z") for i in range(5)]
    hidden = [*shown, mine("r5", "2026-10-01T16:00:00Z"), comment("r6", "2026-10-01T17:00:00Z", text="Ещё вопрос")]
    fake.on("GET", "commentThreads", {"items": [thread(comment("t", "2026-10-01T09:00:00Z"), shown, total=7)]})
    fake.on("GET", "comments", {"items": hidden})
    fake.on("GET", "videos", {"items": []})
    async with connect() as session:
        result = payload(await session.call_tool("comments", {}))
    (found,) = result["threads"]
    assert len(found["replies"]) == 7
    assert found["answered"] is True and found["last_word_mine"] is False
    assert fake.called("GET", "comments")[0][0]["parentId"] == "t"


async def test_comments_since_stops_at_a_page_without_recent_activity(connect, fake):
    pages = {
        None: {"items": [thread(comment("fresh", "2026-10-04T10:00:00Z"))], "nextPageToken": "p2"},
        "p2": {"items": [thread(comment("old", "2026-09-01T10:00:00Z"))], "nextPageToken": "p3"},
    }
    fake.on("GET", "commentThreads", handler=lambda params, _body: pages[params.get("pageToken")])
    fake.on("GET", "videos", {"items": []})
    async with connect() as session:
        result = payload(await session.call_tool("comments", {"since": "2026-10-01"}))
    assert [t["thread_id"] for t in result["threads"]] == ["fresh"]
    assert len(fake.called("GET", "commentThreads")) == 2


async def test_comments_reject_an_unreadable_date(connect):
    async with connect() as session:
        assert "since" in error_text(await session.call_tool("comments", {"since": "вчера"}))


async def test_comment_thread_returns_all_replies(connect, fake):
    fake.on("GET", "commentThreads", {"items": [thread(comment(THREAD_ID, "2026-10-01T10:00:00Z"), total=2)]})
    fake.on("GET", "comments", {"items": [comment("a", "2026-10-01T11:00:00Z"), mine("b", "2026-10-01T12:00:00Z")]})
    fake.on("GET", "videos", {"items": [video("V1", "Обзор шин")]})
    async with connect() as session:
        result = payload(await session.call_tool("comment_thread", {"thread_id": THREAD_ID}))
    assert [r["id"] for r in result["replies"]] == ["a", "b"]
    assert result["video_title"] == "Обзор шин"


async def test_reply_without_elicitation_waits_for_confirm_action(connect, fake):
    serve_reply_target(fake)
    async with connect() as session:
        pending = payload(await session.call_tool("reply", {"thread_id": THREAD_ID, "text": "На Озоне"}))
        assert pending["status"] == "confirmation_required"
        assert "Зритель" in pending["preview"] and "«Обзор шин»" in pending["preview"]
        assert "«Где брал?»" in pending["preview"] and pending["preview"].endswith("На Озоне")
        assert fake.called("POST", "comments") == []

        done = payload(await session.call_tool("confirm_action", {"confirmation_id": pending["confirmation_id"]}))
    assert done["status"] == "done"
    assert done["result"] == {"id": "Ugx1.reply", "url": "https://www.youtube.com/watch?v=V1&lc=Ugx1.reply"}
    ((params, body),) = fake.called("POST", "comments")
    assert params["part"] == "snippet"
    assert body == {"snippet": {"parentId": THREAD_ID, "textOriginal": "На Озоне"}}


async def test_reply_with_elicitation_posts_after_the_user_agrees(connect, fake):
    serve_reply_target(fake)
    seen: list[str] = []
    async with connect(elicitation_callback=elicitation_answer(True, seen)) as session:
        result = payload(await session.call_tool("reply", {"thread_id": THREAD_ID, "text": "На Озоне"}))
    assert result["status"] == "done"
    assert "Текст ответа:\nНа Озоне" in seen[0]
    assert len(fake.called("POST", "comments")) == 1


async def test_reply_rejected_in_the_dialog_is_not_posted(connect, fake):
    serve_reply_target(fake)
    async with connect(elicitation_callback=elicitation_answer(False)) as session:
        result = payload(await session.call_tool("reply", {"thread_id": THREAD_ID, "text": "На Озоне"}))
    assert result["status"] == "rejected"
    assert fake.called("POST", "comments") == []


async def test_cancelled_reply_cannot_be_confirmed(connect, fake):
    serve_reply_target(fake)
    async with connect() as session:
        pending = payload(await session.call_tool("reply", {"thread_id": THREAD_ID, "text": "На Озоне"}))
        cancelled = payload(await session.call_tool("cancel_action", {"confirmation_id": pending["confirmation_id"]}))
        late = await session.call_tool("confirm_action", {"confirmation_id": pending["confirmation_id"]})
    assert cancelled["status"] == "cancelled"
    assert "не найдена" in error_text(late)
    assert fake.called("POST", "comments") == []


async def test_reply_to_a_missing_thread(connect, fake):
    fake.on("GET", "commentThreads", {"items": []})
    async with connect() as session:
        text = error_text(await session.call_tool("reply", {"thread_id": "gone", "text": "Привет"}))
    assert "Ветка gone не найдена" in text


async def test_edit_reply_shows_old_and_new_text(connect, fake):
    fake.on("GET", "comments", {"items": [mine("my.1", "2026-10-01T12:00:00Z", text="Спасиба")]})
    fake.on("PUT", "comments", {"id": "my.1", "snippet": {"textDisplay": "Спасибо"}})
    async with connect() as session:
        pending = payload(await session.call_tool("edit_reply", {"comment_id": "my.1", "text": "Спасибо"}))
        assert "Было:\nСпасиба" in pending["preview"] and "Станет:\nСпасибо" in pending["preview"]
        done = payload(await session.call_tool("confirm_action", {"confirmation_id": pending["confirmation_id"]}))
    assert done["result"] == {"id": "my.1", "text": "Спасибо"}
    assert fake.called("PUT", "comments")[0][1] == {"id": "my.1", "snippet": {"textOriginal": "Спасибо"}}


async def test_edit_reply_refuses_someone_elses_comment(connect, fake):
    fake.on("GET", "comments", {"items": [comment("their", "2026-10-01T12:00:00Z")]})
    async with connect() as session:
        text = error_text(await session.call_tool("edit_reply", {"comment_id": "their", "text": "Исправил"}))
    assert "только комментарии самого канала" in text


async def test_moderate_reject_and_ban(connect, fake):
    fake.on("GET", "comments", {"items": [comment("spam1", "2026-10-01T12:00:00Z", text="Заработок в сети")]})
    fake.on("POST", "comments/setModerationStatus", {})
    async with connect() as session:
        pending = payload(
            await session.call_tool(
                "moderate", {"comment_ids": ["spam1", "held2"], "action": "reject", "ban_author": True}
            )
        )
        assert "Скрыть с видео, комментариев: 2" in pending["preview"]
        assert "«Заработок в сети»" in pending["preview"] and "held2 (текст недоступен)" in pending["preview"]
        assert "заблокированы" in pending["preview"]
        done = payload(await session.call_tool("confirm_action", {"confirmation_id": pending["confirmation_id"]}))
    assert done["result"] == {"comments": 2, "status": "rejected", "banned": True}
    ((params, _),) = fake.called("POST", "comments/setModerationStatus")
    assert params == {"id": "spam1,held2", "moderationStatus": "rejected", "banAuthor": "true"}


async def test_moderate_ban_needs_reject(connect, fake):
    async with connect() as session:
        result = await session.call_tool("moderate", {"comment_ids": ["c1"], "action": "publish", "ban_author": True})
    assert "только вместе с action=reject" in error_text(result)
    assert fake.called("POST", "comments/setModerationStatus") == []


async def test_analytics_retention_needs_a_video(connect):
    async with connect() as session:
        assert "укажите video_id" in error_text(await session.call_tool("analytics", {"report": "retention"}))


async def test_analytics_for_a_video_starts_at_its_publish_date(connect, fake):
    fake.on("GET", "videos", {"items": [video("V1", "Обзор шин", "2026-09-15T18:00:00Z")]})
    fake.on(
        "GET",
        "reports",
        {
            "columnHeaders": [{"name": "elapsedVideoTimeRatio"}, {"name": "audienceWatchRatio"}],
            "rows": [[0.01, 1.0], [0.5, 0.42]],
        },
    )
    async with connect() as session:
        result = payload(
            await session.call_tool("analytics", {"report": "retention", "video_id": "V1", "end_date": "2026-10-01"})
        )
    assert result == {
        "start": "2026-09-15",
        "end": "2026-10-01",
        "rows": [
            {"elapsedVideoTimeRatio": 0.01, "audienceWatchRatio": 1.0},
            {"elapsedVideoTimeRatio": 0.5, "audienceWatchRatio": 0.42},
        ],
    }
    params = fake.called("GET", "reports")[0][0]
    assert params["ids"] == "channel==MINE"
    assert params["filters"] == "video==V1"
    assert params["dimensions"] == "elapsedVideoTimeRatio"


async def test_analytics_top_videos_get_titles(connect, fake):
    fake.on("GET", "reports", {"columnHeaders": [{"name": "video"}, {"name": "views"}], "rows": [["V1", 900]]})
    fake.on("GET", "videos", {"items": [video("V1", "Обзор шин")]})
    async with connect() as session:
        result = payload(
            await session.call_tool(
                "analytics", {"report": "top_videos", "start_date": "2026-09-01", "end_date": "2026-09-28"}
            )
        )
    assert result["rows"] == [{"video": "V1", "views": 900, "title": "Обзор шин"}]
    params = fake.called("GET", "reports")[0][0]
    assert (params["startDate"], params["endDate"], params["sort"]) == ("2026-09-01", "2026-09-28", "-views")


async def test_analytics_query_defaults_to_the_last_28_days(connect, fake):
    fake.on("GET", "reports", {"columnHeaders": [{"name": "day"}, {"name": "views"}], "rows": []})
    async with connect() as session:
        result = payload(
            await session.call_tool(
                "analytics_query", {"metrics": "views", "dimensions": "day", "end_date": "2026-10-28"}
            )
        )
    assert (result["start"], result["end"], result["rows"]) == ("2026-10-01", "2026-10-28", [])
    params = fake.called("GET", "reports")[0][0]
    assert params["metrics"] == "views" and params["dimensions"] == "day"
    assert "filters" not in params and "maxResults" not in params
