import json

import pytest

from youtube_mcp_server import __version__
from youtube_mcp_server.auth import find_client_secret
from youtube_mcp_server.client import YouTubeError, credentials_from_env
from youtube_mcp_server.server import main, seconds


def run(argv: list[str]) -> int:
    with pytest.raises(SystemExit) as caught:
        main(argv)
    return caught.value.code


def test_version(capsys):
    assert run(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"youtube-mcp-server {__version__}"


def test_check_without_token_explains_how_to_authorise(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("YOUTUBE_MCP_DIR", str(tmp_path))
    assert run(["check"]) == 1
    assert f"YOUTUBE_MCP_DIR={tmp_path} uvx" in capsys.readouterr().err


def test_auth_without_client_secret(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("YOUTUBE_MCP_DIR", str(tmp_path))
    assert run(["auth"]) == 1
    assert "Desktop app" in capsys.readouterr().err


def test_credentials_prints_the_saved_access(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("YOUTUBE_MCP_DIR", str(tmp_path))
    token = {"client_id": "id", "client_secret": "s", "refresh_token": "1//r", "token": "ya29"}
    (tmp_path / "token.json").write_text(json.dumps(token))
    assert run(["credentials"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "YOUTUBE_CLIENT_ID=id",
        "YOUTUBE_CLIENT_SECRET=s",
        "YOUTUBE_REFRESH_TOKEN=1//r",
    ]


def test_credentials_without_token_explains_how_to_authorise(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("YOUTUBE_MCP_DIR", str(tmp_path))
    assert run(["credentials"]) == 1
    assert "auth" in capsys.readouterr().err


def test_empty_plugin_settings_fall_back_to_token_json(monkeypatch):
    for var in ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN"):
        monkeypatch.setenv(var, "")
    assert credentials_from_env() is None


def write_client(path, kind="installed"):
    path.write_text(json.dumps({kind: {"client_id": "id", "client_secret": "s"}}))
    return path


def test_client_secret_is_found_by_its_download_name(tmp_path):
    secret = write_client(tmp_path / "client_secret_123-abc.apps.googleusercontent.com.json")
    assert find_client_secret(tmp_path) == secret


def test_several_client_secrets_need_an_explicit_choice(tmp_path):
    write_client(tmp_path / "client_secret_1.json")
    write_client(tmp_path / "client_secret_2.json")
    with pytest.raises(YouTubeError, match="--client-secret"):
        find_client_secret(tmp_path)
    assert find_client_secret(tmp_path, tmp_path / "client_secret_2.json").name == "client_secret_2.json"


def test_web_client_is_rejected(tmp_path):
    write_client(tmp_path / "client_secret.json", kind="web")
    with pytest.raises(YouTubeError, match="Desktop app"):
        find_client_secret(tmp_path)


@pytest.mark.parametrize(
    ("duration", "expected"),
    [("PT12M5S", 725), ("PT45S", 45), ("PT1H2M", 3720), ("P1DT1S", 86401), ("P0D", 0), ("bogus", None)],
)
def test_iso_durations(duration, expected):
    assert seconds(duration) == expected
