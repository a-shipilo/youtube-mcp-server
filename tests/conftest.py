import json
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from youtube_mcp_server.approval import ApprovalGate
from youtube_mcp_server.client import YouTubeClient
from youtube_mcp_server.server import create_server

MY_CHANNEL = "UCme"
VIEWER = "UCviewer"

Handler = Callable[[dict[str, str], Any], Any]


class FakeYouTube:
    """In-process stand-in for Google: OAuth token endpoint, YouTube Data API and YouTube Analytics API."""

    def __init__(self) -> None:
        self.handlers: dict[tuple[str, str], Handler] = {}
        self.calls: list[tuple[str, str, dict[str, str], Any]] = []
        self.token_requests: list[dict[str, str]] = []
        self.token_reply: tuple[int, dict[str, Any]] = (200, {"access_token": "ya29.test", "expires_in": 3599})
        self.on("GET", "channels", {"items": [channel()]})

    def on(self, method: str, resource: str, result: Any = None, *, handler: Handler | None = None) -> None:
        """Answer ``method resource`` (e.g. ``GET commentThreads``) with ``result`` or ``handler(params, body)``."""
        self.handlers[(method, resource)] = handler or (lambda _params, _body: result)

    def called(self, method: str, resource: str) -> list[tuple[dict[str, str], Any]]:
        return [(params, body) for m, r, params, body in self.calls if (m, r) == (method, resource)]

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.host == "oauth2.googleapis.com":
                self.token_requests.append(dict(parse_qsl(request.content.decode())))
                status, body = self.token_reply
                return httpx.Response(status, json=body)
            resource = request.url.path.removeprefix("/youtube/v3/").removeprefix("/v2/")
            params = dict(request.url.params)
            body = json.loads(request.content) if request.content else None
            self.calls.append((request.method, resource, params, body))
            handler = self.handlers.get((request.method, resource))
            if handler is None:
                return httpx.Response(404, json={"error": {"code": 404, "message": f"no fake {resource}"}})
            result = handler(params, body)
            return result if isinstance(result, httpx.Response) else httpx.Response(200, json=result)

        return httpx.MockTransport(handle)


def channel(**overrides: Any) -> dict[str, Any]:
    return {
        "id": MY_CHANNEL,
        "snippet": {"title": "Мой канал", "customUrl": "@me"},
        "statistics": {"subscriberCount": "1200", "viewCount": "50000", "videoCount": "12"},
        "contentDetails": {"relatedPlaylists": {"uploads": "UUme"}},
        **overrides,
    }


def comment(cid: str, published: str, *, author: str = "Зритель", channel_id: str = VIEWER, text: str = "Привет"):
    return {
        "id": cid,
        "snippet": {
            "authorDisplayName": author,
            "authorChannelId": {"value": channel_id},
            "textDisplay": text,
            "likeCount": 0,
            "publishedAt": published,
            "updatedAt": published,
        },
    }


def mine(cid: str, published: str, text: str = "Спасибо!") -> dict[str, Any]:
    return comment(cid, published, author="Мой канал", channel_id=MY_CHANNEL, text=text)


def thread(top: dict[str, Any], replies: list[dict[str, Any]] = (), *, total: int | None = None, video: str = "V1"):
    return {
        "id": top["id"],
        "snippet": {
            "videoId": video,
            "canReply": True,
            "totalReplyCount": len(replies) if total is None else total,
            "topLevelComment": top,
        },
        **({"replies": {"comments": list(replies)}} if replies else {}),
    }


def video(vid: str, title: str, published: str = "2026-09-01T10:00:00Z", duration: str = "PT12M5S"):
    return {
        "id": vid,
        "snippet": {"title": title, "publishedAt": published},
        "statistics": {"viewCount": "100", "likeCount": "10", "commentCount": "3"},
        "contentDetails": {"duration": duration},
        "status": {"privacyStatus": "public"},
    }


@pytest.fixture
def fake() -> FakeYouTube:
    return FakeYouTube()


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    path = tmp_path / "token.json"
    path.write_text(json.dumps({"refresh_token": "1//refresh", "client_id": "id.apps", "client_secret": "secret"}))
    return path


@pytest.fixture
def client(fake: FakeYouTube, token_file: Path) -> YouTubeClient:
    return YouTubeClient(token_file, transport=fake.transport(), retry_delay=0)


@pytest.fixture
def connect(client: YouTubeClient):
    @asynccontextmanager
    async def _connect(*, mode: str = "auto", elicitation_callback=None):
        server = create_server(client, ApprovalGate(mode))  # type: ignore[arg-type]
        async with create_connected_server_and_client_session(
            server, elicitation_callback=elicitation_callback
        ) as session:
            yield session

    return _connect


def payload(result) -> Any:
    """Structured tool output; non-object return values are wrapped as {"result": ...}."""
    assert not result.isError, result.content[0].text
    data = result.structuredContent
    if isinstance(data, dict) and set(data) == {"result"}:
        return data["result"]
    return data


def error_text(result) -> str:
    assert result.isError
    return result.content[0].text
