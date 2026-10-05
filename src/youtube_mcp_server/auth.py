"""One-time browser consent: exchanges the user's OAuth client for a refresh token the server keeps."""

import json
from pathlib import Path

from .client import SCOPES, YouTubeError, save_token


def find_client_secret(directory: Path, explicit: Path | None = None) -> Path:
    if explicit is not None:
        if not explicit.is_file():
            raise YouTubeError(f"Нет файла {explicit}.")
        secret = explicit
    else:
        candidates = sorted(directory.glob("client_secret*.json"))
        if not candidates:
            raise YouTubeError(
                f"В {directory} нет JSON OAuth-клиента (client_secret*.json). Создайте в Google Cloud клиент "
                "типа Desktop app, скачайте JSON и положите его туда или укажите путь через --client-secret."
            )
        if len(candidates) > 1:
            names = ", ".join(path.name for path in candidates)
            raise YouTubeError(
                f"В {directory} несколько OAuth-клиентов ({names}): укажите нужный через --client-secret."
            )
        secret = candidates[0]
    try:
        kind = next(iter(json.loads(secret.read_text())), None)
    except (OSError, ValueError) as exc:
        raise YouTubeError(f"Не удалось прочитать {secret}: {exc}") from None
    if kind != "installed":
        raise YouTubeError(
            f"{secret.name}: нужен OAuth-клиент типа Desktop app, а этот — {kind or 'неизвестного типа'}."
        )
    return secret


def authorize(directory: Path, client_secret: Path | None = None) -> Path:
    """Open the consent page in the browser and save the refresh token to ``directory/token.json``."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(find_client_secret(directory, client_secret)), list(SCOPES))
    credentials = flow.run_local_server(
        port=0,
        prompt="consent",
        access_type="offline",
        authorization_prompt_message="Откройте страницу и разрешите доступ к каналу: {url}",
        success_message="Готово: доступ к каналу сохранён, вкладку можно закрыть.",
    )
    if not credentials.refresh_token:
        raise YouTubeError(
            "Google не выдал refresh token. Отзовите доступ приложения на myaccount.google.com/permissions "
            "и пройдите авторизацию ещё раз."
        )
    token = directory / "token.json"
    save_token(token, json.loads(credentials.to_json()))
    return token
