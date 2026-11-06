"""Host model-source configuration, refresh and offline CLI contracts."""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import yaml
from click.testing import CliRunner

from omnigent.cli_models import models
from omnigent.models.model_catalog_store import (
    catalog_path,
    fingerprint_of,
    read_catalog,
    shutdown_catalog_probes,
)
from omnigent.models.model_source import ModelSource, fetch_model_source, model_source_for


@pytest.fixture(autouse=True)
async def isolated_sources(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("OMNIGENT_INFERENCE_CONFIG", raising=False)
    yield
    await shutdown_catalog_probes()


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {},
        {"static": [], "agent": True},
        {"static": [1]},
        {"agent": False},
        {"command": ""},
        {"url": "file:///tmp/models"},
        {"url": "http://user:pass@example.test"},
        {"static": [], "extra": True},
        {"url": "http://example.test", "api_key_ref": "literal-secret"},
    ],
)
def test_bad_entry_does_not_disable_siblings(raw):
    config = {"model_sources": {"broken": raw, "good": {"static": ["model-a"]}}}
    with pytest.raises(ValueError):
        model_source_for("broken", config)
    assert model_source_for("good", config) == ModelSource("static", static=("model-a",))
    assert model_source_for("acp:default", {}) == ModelSource("agent")
    assert model_source_for("pi", {}) is None


@pytest.mark.parametrize("keyed", [False, True])
async def test_url_listing_accepts_full_models_url_and_optional_key(keyed, monkeypatch, caplog):
    seen = []
    secret = "source-credential-canary"
    monkeypatch.setenv("LISTING_KEY", secret)
    current = ["model-a"]

    def handler(request):
        seen.append((str(request.url), request.headers.get("Authorization")))
        return httpx.Response(200, json={"data": [{"id": current[0]}]})

    client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: client(**{**kwargs, "transport": httpx.MockTransport(handler)}),
    )
    source = ModelSource(
        "url",
        url="https://example.test/custom/models",
        api_key_ref="env:LISTING_KEY" if keyed else None,
    )
    with caplog.at_level(logging.DEBUG):
        rows = await fetch_model_source("pi", source)
    assert [row["id"] for row in rows] == ["model-a"]
    assert rows[0]["source"] == {"kind": "model-source", "label": "URL", "host": "example.test"}
    assert seen == [("https://example.test/custom/models", f"Bearer {secret}" if keyed else None)]
    current[0] = "model-b"
    assert [row["id"] for row in await fetch_model_source("pi", source, refresh="force")] == [
        "model-b"
    ]
    assert len(seen) == 2
    assert secret not in caplog.text


@pytest.mark.parametrize(
    ("age", "refresh", "refetch"),
    [
        (0, "none", False),
        (301, "none", False),
        (301, "auto", True),
        (0, "force", True),
        (3601, "none", True),
    ],
)
async def test_command_cache_refresh_and_empty_or_failed_answer(
    tmp_path, age, refresh, refetch, caplog
):
    script = tmp_path / "models.py"
    payload = tmp_path / "payload.json"
    script.write_text(
        "import json, pathlib, sys\np = json.loads(pathlib.Path(sys.argv[1]).read_text())\n"
        "if p is None:\n print('command-secret-canary', file=sys.stderr)\n sys.exit(1)\n"
        "print(json.dumps(p))\n"
    )
    payload.write_text('["model-a"]')
    command = shlex.join([sys.executable, str(script), str(payload)])
    source = ModelSource("command", command=command)
    assert [row["id"] for row in await fetch_model_source("copilot", source)] == ["model-a"]
    key = fingerprint_of("copilot", source)
    os.utime(catalog_path("model-source", key), (time.time() - age,) * 2)
    payload.write_text('["model-b"]')
    rows = await fetch_model_source("copilot", source, refresh=refresh)
    if age > 3600 and refresh == "none":
        assert [row["id"] for row in rows] == ["model-a"]
        await fetch_model_source("copilot", source, refresh="force")
    else:
        assert [row["id"] for row in rows] == (["model-b"] if refetch else ["model-a"])
    # Keep the last successful nonempty answer through both kinds of bad refresh.
    previous = await fetch_model_source("copilot", source, refresh="force")
    payload.write_text("[]")
    assert await fetch_model_source("copilot", source, refresh="force") == previous
    payload.write_text("null")
    with caplog.at_level(logging.DEBUG), pytest.raises(RuntimeError):
        await fetch_model_source("copilot", source, refresh="force")
    assert read_catalog("model-source", key) == previous
    assert "command-secret-canary" not in caplog.text


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("model_sources: [", 2),
        ("[]", 2),
        ("model_sources: {copilot: {static: [], surprise: true}}", 2),
        ("model_sources: {copilot: {static: ['--unsafe']}}", 1),
        ("model_sources: {copilot: {static: []}}", 0),
    ],
)
def test_check_exit_codes_and_parse_errors(tmp_path, text, code):
    (tmp_path / "config.yaml").write_text(text)
    result = CliRunner().invoke(models, ["check", "copilot", "--json"])
    assert result.exit_code == code, result.output
    data = json.loads(result.stdout)
    assert data["schema_version"] == 1
    if code == 2:
        assert data["config_errors"]
    else:
        assert data["harnesses"][0]["status"] == ("failed" if code else "empty")


@pytest.mark.parametrize(
    ("source", "code"),
    [({"static": ["model-a"]}, 0), ({"command": "exit 7"}, 1), ({"agent": False}, 2)],
)
def test_check_console_entry_preserves_exit_code(tmp_path, source, code):
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"model_sources": {"copilot": source}}))
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from omnigent.cli import main; main()",
            "models",
            "check",
            "copilot",
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == code, result.stdout + result.stderr
    assert bool(json.loads(result.stdout)["config_errors"]) == (code == 2)


def test_check_names_missing_env_without_rendering_secret_or_command_output(tmp_path, caplog):
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "model_sources": {
                    "copilot": {
                        "url": "http://example.test",
                        "api_key_ref": "env:MODEL_SOURCE_TEST_KEY",
                    }
                }
            }
        )
    )
    with (
        patch.dict(
            os.environ,
            {"OMNIGENT_CONFIG_HOME": str(tmp_path), "OMNIGENT_DATA_DIR": str(tmp_path / "data")},
            clear=True,
        ),
        caplog.at_level(logging.DEBUG),
    ):
        result = CliRunner().invoke(models, ["check", "copilot", "--json"])
    assert result.exit_code == 1, result.output
    data = json.loads(result.stdout)
    assert "MODEL_SOURCE_TEST_KEY" in data["harnesses"][0]["hint"]
    assert data["harnesses"][0]["sample"] == []


def test_guide_is_packaged_and_matches_reader_document():
    result = CliRunner().invoke(models, ["guide"])
    assert result.exit_code == 0
    assert "Connect models to Omnigent" in result.output
    assert (
        result.output.strip()
        == (Path(__file__).parents[1] / "docs/connect-models.md").read_text().strip()
    )
