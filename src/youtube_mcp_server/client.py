"""YouTube Data API v3 and YouTube Analytics API v2 over httpx, authorised with a stored OAuth refresh token."""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp.exceptions import ToolError

DATA_URL = "https://www.googleapis.com/youtube/v3"
ANALYTICS_URL = "https://youtubeanalytics.googleapis.com/v2"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = (
    "https://www.googleapis.com/auth/youtube.force-ssl",  # read and write comments
    "https://www.googleapis.com/auth/yt-analytics.readonly",
)
DEFAULT_DIR = Path("~/.config/youtube-mcp-server")
AUTH_COMMAND = "uvx --from git+https://github.com/a-shipilo/youtube-mcp-server youtube-mcp-server auth"
# The same access as token.json, given as environment variables (the Claude Code plugin passes its settings this way)
ENV_CREDENTIALS = {
    "client_id": "YOUTUBE_CLIENT_ID",
    "client_secret": "YOUTUBE_CLIENT_SECRET",
    "refresh_token": "YOUTUBE_REFRESH_TOKEN",
}
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

REASONS = {
    "quotaExceeded": (
        "Закончилась дневная квота YouTube API (10 000 единиц). Она обновляется в полночь по тихоокеанскому времени."
    ),
    "accessNotConfigured": (
        "API не включён в проекте Google Cloud: включите YouTube Data API v3 и YouTube Analytics API "
        "в APIs & Services → Library."
    ),
    "commentsDisabled": "Комментарии к этому видео отключены.",
    "commentNotFound": "Комментарий не найден: его удалили, скрыли или id неверный.",
    "videoNotFound": "Видео не найдено.",
    "operationNotSupported": "YouTube не поддерживает эту операцию для этого комментария.",
}
REASONS["SERVICE_DISABLED"] = REASONS["accessNotConfigured"]
SCOPE_REASONS = frozenset({"insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"})


class YouTubeError(ToolError):
    """An API or authorisation failure whose message is meant for the user (ToolError keeps it visible)."""

    def __init__(self, message: str, *, status: int | None = None, reason: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason


def auth_hint(directory: Path) -> str:
    prefix = "" if directory == DEFAULT_DIR.expanduser() else f"YOUTUBE_MCP_DIR={directory} "
    return f"Пройдите авторизацию: {prefix}{AUTH_COMMAND}"


def env_hint() -> str:
    return (
        f"Пройдите авторизацию заново ({AUTH_COMMAND}), покажите новые значения командой "
        "youtube-mcp-server credentials и обновите их в настройках плагина."
    )


def credentials_from_env() -> dict[str, str] | None:
    """Access from YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET and YOUTUBE_REFRESH_TOKEN; None when none is set."""
    values = {key: os.environ.get(var, "").strip() for key, var in ENV_CREDENTIALS.items()}
    return values if any(values.values()) else None


def load_token(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        raise YouTubeError(f"Сервер ещё не авторизован: нет файла {path}. {auth_hint(path.parent)}") from None
    except (OSError, ValueError) as exc:
        raise YouTubeError(f"Не удалось прочитать {path}: {exc}. {auth_hint(path.parent)}") from None
    missing = [key for key in ("refresh_token", "client_id", "client_secret") if not data.get(key)]
    if missing:
        raise YouTubeError(f"В {path} нет {', '.join(missing)}. {auth_hint(path.parent)}")
    return data


def save_token(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as file:
        json.dump(data, file, indent=2)
    path.chmod(0o600)


def explain(status: int, body: Any, reauth: str) -> YouTubeError:
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return YouTubeError(f"YouTube API ответил HTTP {status}", status=status)
    message = error.get("message") or f"HTTP {status}"
    reasons = [
        item.get("reason") for item in error.get("errors", []) + error.get("details", []) if isinstance(item, dict)
    ]
    reasons = [reason for reason in reasons if reason]
    reason = next((r for r in reasons if r in REASONS or r in SCOPE_REASONS), reasons[0] if reasons else None)
    hint = f"У сохранённого доступа не хватает прав. {reauth}" if reason in SCOPE_REASONS else REASONS.get(reason or "")
    text = f"{hint} (YouTube: {message})" if hint else f"YouTube API: {message}"
    return YouTubeError(text, status=status, reason=reason)


def _json(response: httpx.Response) -> Any:
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError:
        return {}


class YouTubeClient:
    def __init__(
        self,
        token_file: Path,
        *,
        credentials: dict[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        retries: int = 2,
        retry_delay: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.token_file = token_file
        self.credentials = credentials  # from the environment; token_file is used without them
        self.retries = retries
        self.retry_delay = retry_delay
        self._clock = clock
        self._http = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(30.0))
        self._lock = asyncio.Lock()
        self._access_token: str | None = None
        self._expires_at = 0.0
        self._channel: dict[str, Any] | None = None

    async def __aenter__(self) -> YouTubeClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _bearer(self, *, renew: bool = False) -> str:
        async with self._lock:
            if renew or self._access_token is None or self._clock() >= self._expires_at:
                await self._refresh()
            assert self._access_token is not None
            return self._access_token

    def _reauth(self) -> str:
        return env_hint() if self.credentials is not None else auth_hint(self.token_file.parent)

    def _token(self) -> dict[str, Any]:
        if self.credentials is None:
            return load_token(self.token_file)
        missing = [ENV_CREDENTIALS[key] for key, value in self.credentials.items() if not value]
        if missing:
            raise YouTubeError(f"Не задано: {', '.join(missing)}. Нужны все три значения доступа. {env_hint()}")
        return self.credentials

    async def _refresh(self) -> None:
        token = self._token()
        try:
            response = await self._http.post(
                token.get("token_uri") or TOKEN_URL,
                data={
                    "client_id": token["client_id"],
                    "client_secret": token["client_secret"],
                    "refresh_token": token["refresh_token"],
                    "grant_type": "refresh_token",
                },
            )
        except httpx.TransportError as exc:
            raise YouTubeError(f"Нет связи с Google OAuth: {exc}") from exc
        body = _json(response)
        if response.is_error or not isinstance(body, dict) or "access_token" not in body:
            error = body.get("error") if isinstance(body, dict) else None
            if error == "invalid_grant":
                raise YouTubeError(
                    "Google отклонил сохранённый доступ: его отозвали или он истёк (у приложения в статусе "
                    f"Testing доступ живёт 7 дней). {self._reauth()}"
                )
            detail = (body.get("error_description") or error) if isinstance(body, dict) else None
            raise YouTubeError(f"Google не выдал токен доступа: {detail or f'HTTP {response.status_code}'}")
        self._access_token = body["access_token"]
        self._expires_at = self._clock() + max(int(body.get("expires_in", 3600)) - 60, 0)

    async def request(
        self, method: str, url: str, *, params: dict[str, Any] | None = None, body: Any = None
    ) -> dict[str, Any]:
        """Call the API. Only GET is retried: a write whose answer was lost may already have happened."""
        params = {key: value for key, value in (params or {}).items() if value is not None}
        attempts = 1 + (self.retries if method == "GET" else 0)
        attempt, renewed = 1, False
        while True:
            headers = {"Authorization": f"Bearer {await self._bearer()}"}
            try:
                response = await self._http.request(method, url, params=params, json=body, headers=headers)
            except httpx.TransportError as exc:
                if attempt < attempts:
                    await asyncio.sleep(self.retry_delay * attempt)
                    attempt += 1
                    continue
                if method == "GET" or isinstance(exc, httpx.ConnectError):
                    raise YouTubeError(f"Нет связи с YouTube API: {exc}") from exc
                raise YouTubeError(
                    f"Ответ YouTube API потерян ({exc}): операция могла выполниться. "
                    "Проверьте результат, прежде чем повторять."
                ) from exc
            if response.status_code == 401 and not renewed:
                renewed = True
                await self._bearer(renew=True)
                continue
            if response.status_code in RETRY_STATUSES and attempt < attempts:
                await asyncio.sleep(self.retry_delay * attempt)
                attempt += 1
                continue
            data = _json(response)
            if response.is_error:
                raise explain(response.status_code, data, self._reauth())
            return data if isinstance(data, dict) else {}

    async def data(self, resource: str, *, method: str = "GET", body: Any = None, **params: Any) -> dict[str, Any]:
        """YouTube Data API v3: ``resource`` is e.g. ``commentThreads`` or ``comments/setModerationStatus``."""
        return await self.request(method, f"{DATA_URL}/{resource}", params=params, body=body)

    async def report(self, **params: Any) -> dict[str, Any]:
        """YouTube Analytics API v2 reports.query."""
        return await self.request("GET", f"{ANALYTICS_URL}/reports", params=params)

    async def channel(self) -> dict[str, Any]:
        """The authorised channel (cached for the server's lifetime)."""
        if self._channel is None:
            result = await self.data("channels", part="snippet,statistics,contentDetails", mine="true")
            if not result.get("items"):
                raise YouTubeError(
                    "У авторизованного аккаунта Google нет YouTube-канала. Пройдите авторизацию заново и "
                    f"выберите аккаунт канала. {self._reauth()}"
                )
            self._channel = result["items"][0]
        return self._channel

    async def videos(self, ids: Iterable[str | None]) -> dict[str, dict[str, Any]]:
        """Video resources by id, 50 per request (1 quota unit each)."""
        unique = list(dict.fromkeys(video_id for video_id in ids if video_id))
        found: dict[str, dict[str, Any]] = {}
        for start in range(0, len(unique), 50):
            result = await self.data(
                "videos", part="snippet,statistics,contentDetails,status", id=",".join(unique[start : start + 50])
            )
            found.update({video["id"]: video for video in result.get("items", [])})
        return found
