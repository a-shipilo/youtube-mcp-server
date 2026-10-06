import json
import stat

import httpx
import pytest

from youtube_mcp_server.client import YouTubeClient, YouTubeError, auth_hint, explain, save_token

from .conftest import FakeYouTube


def error(status: int, reason: str, message: str = "boom", *, details: bool = False) -> httpx.Response:
    item = {"reason": reason}
    body = {"code": status, "message": message, ("details" if details else "errors"): [item]}
    return httpx.Response(status, json={"error": body})


async def test_access_token_is_refreshed_once_and_reused(client, fake):
    await client.data("channels", part="id", mine="true")
    await client.data("channels", part="id", mine="true")
    assert len(fake.token_requests) == 1
    assert fake.token_requests[0] == {
        "client_id": "id.apps",
        "client_secret": "secret",
        "refresh_token": "1//refresh",
        "grant_type": "refresh_token",
    }


async def test_expired_access_token_is_renewed_and_the_request_repeated(client, fake):
    answers = iter([httpx.Response(401, json={"error": {"code": 401, "message": "expired"}}), {"items": []}])
    fake.on("GET", "videos", handler=lambda _p, _b: next(answers))
    assert await client.data("videos", id="V1") == {"items": []}
    assert len(fake.token_requests) == 2


async def test_revoked_refresh_token_asks_to_authorise_again(client, fake, token_file):
    fake.token_reply = (400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."})
    with pytest.raises(YouTubeError) as caught:
        await client.channel()
    message = str(caught.value)
    assert "7 дней" in message
    assert f"YOUTUBE_MCP_DIR={token_file.parent} uvx" in message


async def test_quota_error_is_explained(client, fake):
    fake.on(
        "GET", "commentThreads", handler=lambda _p, _b: error(403, "quotaExceeded", "The request cannot be completed")
    )
    with pytest.raises(YouTubeError) as caught:
        await client.data("commentThreads", part="snippet")
    assert "квота" in str(caught.value)
    assert caught.value.reason == "quotaExceeded"


def test_disabled_api_is_explained_from_error_details(tmp_path):
    response = error(403, "SERVICE_DISABLED", "YouTube Data API v3 has not been used", details=True)
    assert "APIs & Services" in str(explain(403, response.json(), auth_hint(tmp_path)))


def test_missing_scope_asks_to_authorise_again(tmp_path):
    response = error(403, "insufficientPermissions", "Insufficient Permission")
    assert "youtube-mcp-server auth" in str(explain(403, response.json(), auth_hint(tmp_path)))


async def test_reads_are_retried_after_server_errors(client, fake):
    answers = iter([httpx.Response(503), httpx.Response(500), {"items": [1]}])
    fake.on("GET", "videos", handler=lambda _p, _b: next(answers))
    assert await client.data("videos", id="V1") == {"items": [1]}
    assert len(fake.called("GET", "videos")) == 3


async def test_writes_are_never_retried(client, fake):
    fake.on("POST", "comments", handler=lambda _p, _b: httpx.Response(503))
    with pytest.raises(YouTubeError):
        await client.data("comments", method="POST", part="snippet", body={"snippet": {}})
    assert len(fake.called("POST", "comments")) == 1


async def test_lost_answer_to_a_write_warns_it_may_have_happened(fake, token_file):
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        raise httpx.ReadTimeout("timed out", request=request)

    async with YouTubeClient(token_file, transport=httpx.MockTransport(handle), retry_delay=0) as client:
        with pytest.raises(YouTubeError) as caught:
            await client.data("comments", method="POST", part="snippet", body={})
    assert "могла выполниться" in str(caught.value)


async def test_channel_without_youtube_channel(client, fake: FakeYouTube):
    fake.on("GET", "channels", {"items": []})
    with pytest.raises(YouTubeError, match="нет YouTube-канала"):
        await client.channel()


async def test_videos_are_requested_in_batches_of_fifty(client, fake):
    fake.on("GET", "videos", handler=lambda params, _b: {"items": [{"id": v} for v in params["id"].split(",")]})
    found = await client.videos([f"V{i}" for i in range(120)] + ["V0", None])
    assert len(found) == 120
    assert [len(params["id"].split(",")) for params, _ in fake.called("GET", "videos")] == [50, 50, 20]


def test_saved_token_is_private(tmp_path):
    path = tmp_path / "nested" / "token.json"
    save_token(path, {"refresh_token": "x"})
    assert json.loads(path.read_text()) == {"refresh_token": "x"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


ENV_ACCESS = {"client_id": "env-id", "client_secret": "env-secret", "refresh_token": "1//env"}


async def test_access_from_the_environment_replaces_token_json(fake, tmp_path):
    async with YouTubeClient(tmp_path / "token.json", credentials=ENV_ACCESS, transport=fake.transport()) as client:
        await client.data("channels", part="id", mine="true")
    assert fake.token_requests[0] == {**ENV_ACCESS, "grant_type": "refresh_token"}


async def test_incomplete_access_in_the_environment_names_what_is_missing(fake, tmp_path):
    partial = {**ENV_ACCESS, "refresh_token": ""}
    async with YouTubeClient(tmp_path / "token.json", credentials=partial, transport=fake.transport()) as client:
        with pytest.raises(YouTubeError) as caught:
            await client.data("channels", part="id", mine="true")
    assert "YOUTUBE_REFRESH_TOKEN" in str(caught.value)
    assert not fake.token_requests


async def test_revoked_access_from_the_environment_points_to_the_plugin_settings(fake, tmp_path):
    fake.token_reply = (400, {"error": "invalid_grant"})
    async with YouTubeClient(tmp_path / "token.json", credentials=ENV_ACCESS, transport=fake.transport()) as client:
        with pytest.raises(YouTubeError) as caught:
            await client.data("channels", part="id", mine="true")
    assert "настройках плагина" in str(caught.value)
