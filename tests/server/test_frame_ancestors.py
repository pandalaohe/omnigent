"""SPA framing policy, cache transitions and CLI config delivery."""

from pathlib import Path

import pytest
from click.testing import CliRunner
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

from omnigent.server.app import _SPAStaticFiles
from omnigent.server.server_config import frame_ancestors_policy


@pytest.mark.parametrize(
    "entries",
    [
        [],
        ["'self'"],
        ["'none'"],
        [
            "https://*.example.org:443",
            "http://127.0.0.1:8000",
            "https://[::1]",
            "https://[::ffff:192.0.2.1]",
        ],
    ],
)
def test_valid_policy(entries):
    assert frame_ancestors_policy({"frame_ancestors": entries}) == (
        "frame-ancestors " + " ".join(entries) if entries else None
    )


@pytest.mark.parametrize(
    "entries",
    [
        None,
        "'self'",
        [1],
        ["self"],
        ["*"],
        ["https:"],
        ["https://example.org/"],
        ["https://user@example.org"],
        ["https://example.org?q=1"],
        ["https://example.org#x"],
        ["https://example.org;"],
        ["https://example.org,"],
        ["https://example.org\n"],
        ["https://*example.org"],
        ["https://example.org:0"],
        ["https://example.org:65536"],
        ["https://*.127.0.0.1"],
        ["'none'", "'self'"],
    ],
)
def test_invalid_policy(entries):
    with pytest.raises(ValueError, match="frame_ancestors"):
        frame_ancestors_policy({"frame_ancestors": entries})


def test_shell_policy_and_cache_transitions(tmp_path):
    (tmp_path / "index.html").write_text("<html>board</html>")
    previous = None
    for policy in [None, "frame-ancestors 'self'", "frame-ancestors https://example.org", None]:
        with TestClient(
            Starlette(
                routes=[
                    Mount(
                        "/",
                        app=_SPAStaticFiles(directory=tmp_path, html=True, framing_policy=policy),
                    )
                ]
            )
        ) as client:
            for path in [
                "/",
                "/login",
                "/register",
                "/approve/session/request",
                "/frame/task-board",
                "/frame/usage/board/account/settings",
                "/index.html",
            ]:
                response = client.get(path, headers={"If-None-Match": previous or ""})
                assert response.status_code == 200
                assert response.headers.get("content-security-policy") == policy
                cached = client.get(path, headers={"If-None-Match": response.headers["etag"]})
                assert cached.status_code == 304
                assert cached.headers.get("content-security-policy") == policy
            previous = response.headers["etag"]
            assert "content-security-policy" not in client.get("/v1/unknown").headers


@pytest.mark.parametrize("source", ["flag", "env", "default"])
@pytest.mark.parametrize("valid", [True, False])
def test_cli_delivers_policy(tmp_path: Path, monkeypatch, source, valid):
    from omnigent.cli import cli
    from omnigent.server import app as app_module
    from omnigent.server import server_config

    config = tmp_path / "server.yaml"
    config.write_text("frame_ancestors: [\"'self'\"]\n" if valid else 'frame_ancestors: ["*"]\n')
    monkeypatch.delenv("OMNIGENT_CONFIG", raising=False)
    monkeypatch.setattr(server_config, "resolve_data_dir", lambda: tmp_path)
    if source == "default":
        config.rename(tmp_path / "config.yaml")
    elif source == "env":
        monkeypatch.setenv("OMNIGENT_CONFIG", str(config))

    class Captured(Exception):
        pass

    real_create_app = app_module.create_app
    apps = []

    def capture(**kwargs):
        apps.append(real_create_app(**kwargs))
        raise Captured()

    monkeypatch.setattr(app_module, "create_app", capture)
    (tmp_path / "index.html").write_text("<html>board</html>")
    monkeypatch.setattr(app_module, "_WEB_UI_DIST", tmp_path)
    args = [
        "server",
        "--database-uri",
        f"sqlite:///{tmp_path / 'test.db'}",
        "--artifact-location",
        str(tmp_path / "artifacts"),
    ]
    if source == "flag":
        args += ["-c", str(config)]
    result = CliRunner().invoke(cli, args)
    if valid:
        assert isinstance(result.exception, Captured), result.exception
        with TestClient(apps[0]) as client:
            assert (
                client.get("/frame/task-board").headers["content-security-policy"]
                == "frame-ancestors 'self'"
            )
    else:
        assert isinstance(result.exception, ValueError), result.exception
        assert "frame_ancestors" in str(result.exception)
