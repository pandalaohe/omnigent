"""Allow-listed agent settings, their undo log, and settings-root routing."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from omnigent.harnesses.claude_native import main as claude_native
from omnigent.harnesses.codex_native import app_server as codex_app
from omnigent.harnesses.pi_native.credentials import global_pi_agent_dir
from omnigent.host import agent_settings
from omnigent.host.agent_settings import (
    CHANGED_SINCE,
    JSON_READ_ONLY,
    NOT_SIGNED_IN,
    UNDO_CHANGED,
    AgentSettingsError,
)
from omnigent.host.connect import _build_runner_env
from omnigent.host.frames import (
    HostAgentSettingsResultFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.inner.codex_executor import _codex_home_config_source_from_env
from omnigent.onboarding import harness_accounts, harness_login

_CANARY = "sk" + "-test-CANARY-never-leaves-the-host"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(home / ".omnigent"))
    for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "PI_CODING_AGENT_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        agent_settings,
        "_probe_account",
        lambda _descriptor, _root: {"kind": "none", "label": NOT_SIGNED_IN},
    )


def _field(view: agent_settings.AgentSettingsView, key: str) -> agent_settings.FieldView:
    return next(field for field in view["fields"] if field["key"] == key)


def test_json_write_preserves_unknown_fields_mode_and_undo(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "claude"
    root.mkdir()
    settings = root / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "model": "old-model",
                "effortLevel": "medium",
                "permissions": {"defaultMode": "default", "unknown": True},
                "unknown": {"kept": [1, 2]},
                "env": {"TOKEN": _CANARY},
                "apiKeyHelper": _CANARY,
                "sandboxProxy": _CANARY,
            }
        ),
        encoding="utf-8",
    )
    settings.chmod(0o640)
    (root / "auth.json").write_text(_CANARY, encoding="utf-8")

    before = agent_settings.read_agent_settings("claude", str(root))
    after = agent_settings.write_agent_setting(
        "claude", str(root), "model", "new-model", before["revision"]
    )
    document = json.loads(settings.read_text(encoding="utf-8"))
    assert document["model"] == "new-model"
    assert document["unknown"] == {"kept": [1, 2]}
    assert document["permissions"]["unknown"] is True
    assert stat.S_IMODE(settings.stat().st_mode) == 0o640
    assert _field(after, "model")["undo_available"] is True

    undo_files = list((Path.home() / ".omnigent" / "agent-settings-undo").glob("*.json"))
    assert len(undo_files) == 1
    preimage = undo_files[0].read_text(encoding="utf-8")
    assert json.loads(preimage)["model"]["value"] == "old-model"

    restored = agent_settings.undo_agent_setting("claude", str(root), "model")
    assert _field(restored, "model")["value"] == "old-model"
    assert _field(restored, "model")["undo_available"] is False
    assert json.loads(settings.read_text(encoding="utf-8"))["unknown"] == {"kept": [1, 2]}
    for payload in (json.dumps(before), json.dumps(after), preimage, caplog.text):
        assert _CANARY not in payload
    assert _CANARY not in encode_host_frame(
        HostAgentSettingsResultFrame("settings", "ok", dict(after))
    )


def test_toml_write_keeps_comments_and_unknown_keys(tmp_path: Path) -> None:
    root = tmp_path / "codex"
    root.mkdir()
    config = root / "config.toml"
    config.write_text(
        '# keep this comment\nmodel = "old-model"\nunknown = { kept = true }\n',
        encoding="utf-8",
    )
    before = agent_settings.read_agent_settings("codex", str(root))
    after = agent_settings.write_agent_setting(
        "codex", str(root), "approval_policy", "on-request", before["revision"]
    )
    text = config.read_text(encoding="utf-8")
    assert "# keep this comment" in text
    assert "unknown = { kept = true }" in text
    assert 'approval_policy = "on-request"' in text
    assert _field(after, "model")["value"] == "old-model"

    agent_settings.undo_agent_setting("codex", str(root), "approval_policy")
    restored = config.read_text(encoding="utf-8")
    assert "# keep this comment" in restored
    assert "approval_policy" not in restored


def test_write_cas_and_undo_refuse_later_changes(tmp_path: Path) -> None:
    root = tmp_path / "claude"
    root.mkdir()
    path = root / "settings.json"
    path.write_text('{"model":"old-model"}\n', encoding="utf-8")
    before = agent_settings.read_agent_settings("claude", str(root))
    with pytest.raises(AgentSettingsError, match=CHANGED_SINCE):
        agent_settings.write_agent_setting("claude", str(root), "model", "new", "stale")

    after = agent_settings.write_agent_setting(
        "claude", str(root), "model", "new-model", before["revision"]
    )
    path.write_text('{"model":"changed-elsewhere"}\n', encoding="utf-8")
    with pytest.raises(AgentSettingsError, match=UNDO_CHANGED):
        agent_settings.undo_agent_setting("claude", str(root), "model")
    assert _field(after, "model")["value"] == "new-model"


def test_jsonc_and_symlink_are_read_only(tmp_path: Path) -> None:
    jsonc_root = tmp_path / "jsonc"
    jsonc_root.mkdir()
    jsonc = jsonc_root / "settings.json"
    text = '{// comment\n"model":"old-model"}\n'
    jsonc.write_text(text, encoding="utf-8")
    view = agent_settings.read_agent_settings("claude", str(jsonc_root))
    assert view["read_only_reason"] == JSON_READ_ONLY
    assert _field(view, "model")["writable"] is False
    with pytest.raises(AgentSettingsError):
        agent_settings.write_agent_setting(
            "claude", str(jsonc_root), "model", "new-model", view["revision"]
        )
    assert jsonc.read_text(encoding="utf-8") == text

    linked_root = tmp_path / "linked"
    linked_root.mkdir()
    target = tmp_path / "target.json"
    target.write_text('{"model":"old-model"}\n', encoding="utf-8")
    os.symlink(target, linked_root / "settings.json")
    linked = agent_settings.read_agent_settings("claude", str(linked_root))
    assert linked["read_only_reason"] is not None
    with pytest.raises(AgentSettingsError):
        agent_settings.write_agent_setting(
            "claude", str(linked_root), "model", "new-model", linked["revision"]
        )
    assert json.loads(target.read_text(encoding="utf-8"))["model"] == "old-model"


def test_set_root_preserves_launch_config_and_reports_no_login(tmp_path: Path) -> None:
    config_home = Path.home() / ".omnigent"
    config_home.mkdir(parents=True)
    (config_home / "config.yaml").write_text(
        "harness:\n  default: codex\n  codex:\n    command: codex-wrapper\n"
        "    args: ['--existing']\n",
        encoding="utf-8",
    )
    root = tmp_path / "empty-codex"
    root.mkdir()
    view = agent_settings.agent_settings_set_root("codex", str(root))
    assert view["root"] == str(root)
    assert view["account"] == {"kind": "none", "label": NOT_SIGNED_IN}
    config = (config_home / "config.yaml").read_text(encoding="utf-8")
    assert "command: codex-wrapper" in config
    assert "--existing" in config
    assert f"config_root: {root}" in config
    assert agent_settings.resolved_root("codex") == str(root)

    agent_settings.agent_settings_set_root("codex", None)
    assert agent_settings.resolved_root("codex") is None
    with pytest.raises(AgentSettingsError, match="fixed"):
        agent_settings.agent_settings_set_root("agy", str(root))


def test_unlisted_family_has_no_launch_override(monkeypatch: pytest.MonkeyPatch) -> None:
    assert agent_settings.agent_settings_launch_env("opencode") == {}
    assert agent_settings.agent_settings_launch_env("acp:hermes") == {}
    monkeypatch.setattr(harness_accounts.shutil, "which", lambda _name: None)
    assert harness_accounts.probe_harness_account("opencode") is None
    with pytest.raises(AgentSettingsError, match="not known"):
        agent_settings.descriptor_for("acp:hermes")


def test_hjson_field_is_visible_but_not_writable(tmp_path: Path) -> None:
    root = tmp_path / "antigravity-acp"
    root.mkdir()
    (root / "settings.json").write_text(
        '{// hjson comment\nauth: {type: "oauth",},}\n', encoding="utf-8"
    )
    view = agent_settings.read_agent_settings("agy-acp", str(root))
    assert _field(view, "auth.type") == {
        "key": "auth.type",
        "present": True,
        "value": "oauth",
        "writable": False,
        "undo_available": False,
    }
    with pytest.raises(AgentSettingsError, match="isn't writable"):
        agent_settings.write_agent_setting(
            "agy-acp", str(root), "auth.type", "other", view["revision"]
        )


def test_result_frame_drops_non_view_data(tmp_path: Path) -> None:
    root = tmp_path / "claude"
    root.mkdir()
    (root / "settings.json").write_text('{"model":"model"}\n', encoding="utf-8")
    view = dict(agent_settings.read_agent_settings("claude", str(root)))
    view["raw"] = _CANARY
    wire = encode_host_frame(HostAgentSettingsResultFrame("settings", "ok", view))
    decoded = decode_host_frame(wire)
    assert isinstance(decoded, HostAgentSettingsResultFrame)
    assert decoded.view is not None
    assert "raw" not in decoded.view
    assert _CANARY not in wire


def test_runner_and_catalog_roots_follow_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude_root = tmp_path / "claude-a"
    codex_root = tmp_path / "codex-a"
    pi_root = tmp_path / "pi-a"
    for family, root in (("claude", claude_root), ("codex", codex_root), ("pi", pi_root)):
        root.mkdir()
        agent_settings.agent_settings_set_root(family, str(root))

    env = _build_runner_env(
        {},
        server_url="https://example.test",
        runner_id="runner_test",
        binding_token="test-binding",
        workspace=str(tmp_path),
        parent_pid=123,
    )
    assert env["CLAUDE_CONFIG_DIR"] == str(claude_root)
    assert env["CODEX_HOME"] == str(codex_root)
    assert env["PI_CODING_AGENT_DIR"] == str(pi_root)
    assert _codex_home_config_source_from_env() == codex_root
    assert global_pi_agent_dir() == pi_root
    assert claude_native.build_native_claude_terminal_env(None)["CLAUDE_CONFIG_DIR"] == str(
        claude_root
    )
    _command, _args, probe_env = claude_native._claude_model_probe_invocation(None)
    assert probe_env["CLAUDE_CONFIG_DIR"] == str(claude_root)

    monkeypatch.setattr(
        claude_native, "_claude_model_probe_launch", lambda *_a, **_k: ("claude", [])
    )
    monkeypatch.setattr(
        "omnigent.models.model_catalog_store.binary_identity", lambda value: str(value)
    )
    monkeypatch.setattr("omnigent.onboarding.ambient.claude_managed_model_picker", lambda: None)
    monkeypatch.setattr(claude_native, "managed_claude_gateway_signal", lambda: (None, None))
    first_fingerprint = claude_native.claude_catalog_fingerprint(None)
    claude_b = tmp_path / "claude-b"
    claude_b.mkdir()
    agent_settings.agent_settings_set_root("claude", str(claude_b))
    assert claude_native.claude_catalog_fingerprint(None) != first_fingerprint


def test_codex_probe_home_and_fingerprint_follow_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "codex-a"
    second = tmp_path / "codex-b"
    for root, model in ((first, "model-a"), (second, "model-b")):
        root.mkdir()
        (root / "config.toml").write_text(f'model = "{model}"\n', encoding="utf-8")
    monkeypatch.setattr(codex_app, "_find_codex_cli", lambda: "/opt/work/bin/codex")
    monkeypatch.setattr(
        "omnigent.models.model_catalog_store.binary_identity", lambda value: str(value)
    )
    launch = codex_app.NativeCodexLaunch([], None, None)

    agent_settings.agent_settings_set_root("codex", str(first))
    first_fingerprint = codex_app.codex_catalog_fingerprint(launch)
    first_probe = codex_app._probe_codex_home([])
    assert 'model = "model-a"' in (first_probe / "config.toml").read_text(encoding="utf-8")

    agent_settings.agent_settings_set_root("codex", str(second))
    second_fingerprint = codex_app.codex_catalog_fingerprint(launch)
    second_probe = codex_app._probe_codex_home([])
    assert second_fingerprint != first_fingerprint
    assert second_probe != first_probe
    assert 'model = "model-b"' in (second_probe / "config.toml").read_text(encoding="utf-8")


def test_account_probes_use_selected_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = {family: tmp_path / family for family in ("claude", "codex", "pi")}
    for family, root in roots.items():
        root.mkdir()
        agent_settings.agent_settings_set_root(family, str(root))
    monkeypatch.setattr(harness_accounts.shutil, "which", lambda name: f"/opt/work/bin/{name}")
    command_env: dict[str, object] = {}

    def _command(_argv: list[str], **kwargs: object) -> tuple[int, dict[str, object]]:
        command_env.update(kwargs)
        return 0, {"loggedIn": False}

    monkeypatch.setattr(harness_accounts, "_run_json_command", _command)
    harness_accounts.probe_harness_account("claude")
    assert isinstance(command_env["env"], dict)
    assert command_env["env"]["CLAUDE_CONFIG_DIR"] == str(roots["claude"])

    class _Rpc:
        env: dict[str, str] | None = None

        def __init__(self, *_args: object, **kwargs: object) -> None:
            type(self).env = dict(kwargs["env"])  # type: ignore[arg-type]

        def request(self, _method: str, _params: dict[str, object]) -> dict[str, object]:
            return {"account": None}

        def notify(self, _method: str) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr(harness_accounts, "_StdioJsonRpc", _Rpc)
    harness_accounts.probe_harness_account("codex")
    assert _Rpc.env is not None
    assert _Rpc.env["CODEX_HOME"] == str(roots["codex"])


@pytest.mark.asyncio
async def test_login_processes_use_selected_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex_root = tmp_path / "codex-login"
    claude_root = tmp_path / "claude-logout"
    codex_root.mkdir()
    claude_root.mkdir()
    agent_settings.agent_settings_set_root("codex", str(codex_root))
    agent_settings.agent_settings_set_root("claude", str(claude_root))
    monkeypatch.setattr(harness_login.shutil, "which", lambda name: f"/opt/work/bin/{name}")
    environments: list[dict[str, str]] = []

    class _Process:
        stdin = None
        stdout = None
        stderr = None
        returncode = 0
        pid = 123

        async def wait(self) -> int:
            return 0

        def kill(self) -> None:
            return None

    async def _exec(*_args: object, **kwargs: object) -> _Process:
        environments.append(dict(kwargs["env"]))  # type: ignore[arg-type]
        return _Process()

    monkeypatch.setattr(harness_login.asyncio, "create_subprocess_exec", _exec)
    session = harness_login.CodexLoginSession()
    monkeypatch.setattr(session, "request", AsyncMock(return_value={}))
    monkeypatch.setattr(session, "notify", AsyncMock())
    await session.start_app_server()
    await session.close()
    await harness_login.claude_logout()
    assert environments[0]["CODEX_HOME"] == str(codex_root)
    assert environments[1]["CLAUDE_CONFIG_DIR"] == str(claude_root)
