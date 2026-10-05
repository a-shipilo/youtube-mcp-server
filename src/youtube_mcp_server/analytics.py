"""Analytics tools: ready-made YouTube Analytics reports and a raw reports.query."""

import datetime as dt
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from .client import YouTubeClient, YouTubeError
from .comments import READ

PRESETS: dict[str, dict[str, Any]] = {
    "overview": {
        "metrics": "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,likes,comments,shares,"
        "subscribersGained,subscribersLost",
    },
    "daily": {
        "metrics": "views,estimatedMinutesWatched,averageViewDuration,subscribersGained",
        "dimensions": "day",
        "sort": "day",
    },
    "top_videos": {
        "metrics": "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,likes,comments,"
        "subscribersGained",
        "dimensions": "video",
        "sort": "-views",
        "maxResults": 25,
    },
    "traffic_sources": {
        "metrics": "views,estimatedMinutesWatched",
        "dimensions": "insightTrafficSourceType",
        "sort": "-views",
    },
    "search_terms": {
        "metrics": "views,estimatedMinutesWatched",
        "dimensions": "insightTrafficSourceDetail",
        "filters": "insightTrafficSourceType==YT_SEARCH",
        "sort": "-views",
        "maxResults": 25,
    },
    "retention": {
        "metrics": "audienceWatchRatio,relativeRetentionPerformance",
        "dimensions": "elapsedVideoTimeRatio",
    },
    "geography": {
        "metrics": "views,estimatedMinutesWatched,averageViewDuration",
        "dimensions": "country",
        "sort": "-views",
        "maxResults": 25,
    },
    "devices": {"metrics": "views,estimatedMinutesWatched", "dimensions": "deviceType", "sort": "-views"},
    "demographics": {"metrics": "viewerPercentage", "dimensions": "ageGroup,gender"},
}

ReportName = Annotated[
    Literal[
        "overview",
        "daily",
        "top_videos",
        "traffic_sources",
        "search_terms",
        "retention",
        "geography",
        "devices",
        "demographics",
    ],
    Field(
        description=(
            "overview — итоги за период; daily — по дням; top_videos — лучшие видео периода; "
            "traffic_sources — откуда приходят просмотры; search_terms — поисковые запросы YouTube, по которым "
            "находят видео; retention — кривая удержания (нужен video_id); geography — страны; devices — "
            "устройства; demographics — возраст и пол"
        )
    ),
]
VideoId = Annotated[str | None, Field(description="Одно видео; без него — весь канал")]
StartDate = Annotated[
    str | None, Field(description="YYYY-MM-DD; по умолчанию дата публикации видео или 28 дней назад для канала")
]
EndDate = Annotated[str | None, Field(description="YYYY-MM-DD включительно; по умолчанию сегодня")]
Metrics = Annotated[str, Field(description="Метрики через запятую, например views,likes", min_length=1)]
Dimensions = Annotated[str | None, Field(description="Измерения через запятую, например day или video")]
Filters = Annotated[str | None, Field(description="Фильтры, например video==ID;country==RU")]
Sort = Annotated[str | None, Field(description="Сортировка, например -views")]
MaxResults = Annotated[int | None, Field(ge=1, le=200, description="Сколько строк вернуть")]


def report_query(report: str, video_id: str | None) -> dict[str, Any]:
    if report == "retention" and not video_id:
        raise YouTubeError("Отчёт retention строится по одному видео: укажите video_id.")
    if report == "top_videos" and video_id:
        raise YouTubeError("top_videos — отчёт по всему каналу, video_id для него не нужен.")
    query = dict(PRESETS[report])
    if video_id:
        query["filters"] = ";".join(filter(None, [query.get("filters"), f"video=={video_id}"]))
    return query


def check_date(name: str, value: str) -> str:
    try:
        return dt.date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        raise YouTubeError(f"{name}: нужна дата в формате YYYY-MM-DD, а не {value!r}") from None


async def run_report(
    client: YouTubeClient,
    query: dict[str, Any],
    *,
    video_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    end = check_date("end_date", end_date) if end_date else dt.date.today().isoformat()
    if start_date:
        start = check_date("start_date", start_date)
    elif video_id:
        video = (await client.videos([video_id])).get(video_id)
        if video is None:
            raise YouTubeError(f"Видео {video_id} не найдено.")
        start = video["snippet"]["publishedAt"][:10]
    else:
        start = (dt.date.fromisoformat(end) - dt.timedelta(days=27)).isoformat()
    result = await client.report(ids="channel==MINE", startDate=start, endDate=end, **query)
    columns = [header["name"] for header in result.get("columnHeaders", [])]
    rows = [dict(zip(columns, row, strict=False)) for row in result.get("rows") or []]
    if "video" in columns and rows:
        videos = await client.videos(row["video"] for row in rows)
        for row in rows:
            row["title"] = videos.get(row["video"], {}).get("snippet", {}).get("title")
    return {"start": start, "end": end, "rows": rows}


def register_analytics_tools(mcp: FastMCP, client: YouTubeClient) -> None:
    @mcp.tool(annotations=READ)
    async def analytics(
        report: ReportName, video_id: VideoId = None, start_date: StartDate = None, end_date: EndDate = None
    ) -> dict[str, Any]:
        """Готовый отчёт YouTube Analytics по каналу или одному видео. Данные отстают на 2–3 дня."""
        return await run_report(
            client, report_query(report, video_id), video_id=video_id, start_date=start_date, end_date=end_date
        )

    @mcp.tool(annotations=READ)
    async def analytics_query(
        metrics: Metrics,
        dimensions: Dimensions = None,
        filters: Filters = None,
        sort: Sort = None,
        max_results: MaxResults = None,
        start_date: StartDate = None,
        end_date: EndDate = None,
    ) -> dict[str, Any]:
        """Произвольный запрос reports.query к YouTube Analytics API v2 по каналу — для того, чего нет
        в готовых отчётах analytics."""
        query = {
            "metrics": metrics,
            "dimensions": dimensions,
            "filters": filters,
            "sort": sort,
            "maxResults": max_results,
        }
        return await run_report(
            client, {key: value for key, value in query.items() if value}, start_date=start_date, end_date=end_date
        )
