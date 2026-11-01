"""Digest-pinned configuration refresh at the private-home generation boundary."""

from __future__ import annotations

import os
import stat
import tomllib
from pathlib import Path

import pytest

from omnigent.inner import codex_config_sync
from omnigent.inner.codex_executor import _populate_codex_home_config

_ABC_SHA256 = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
_SOURCE = 'model = "host"\n[auto_review]\nextra_policy = "abc"\n'
_PRIVATE = 'model = "session" # retain private selection\n'


@pytest.fixture
def homes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    config_home = tmp_path / "deployment"
    config_home.mkdir()
    (config_home / "config.yaml").write_text(
        f'codex_config_sync:\n  auto_review.extra_policy: "{_ABC_SHA256}"\n'
    )
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(config_home))
    source, target = tmp_path / "source", tmp_path / "private"
    source.mkdir()
    target.mkdir()
    (source / "config.toml").write_text(_SOURCE)
    return source, target


@pytest.mark.parametrize("existing", [None, _PRIVATE, _PRIVATE + "[auto_review]\nother = true\n"])
def test_populate_syncs_approved_value_and_restart_is_noop(
    homes: tuple[Path, Path], existing: str | None
) -> None:
    source, target = homes
    path = target / "config.toml"
    if existing is not None:
        path.write_text(existing)
    _populate_codex_home_config(target, source)
    result = path.read_bytes()
    parsed = tomllib.loads(result.decode())
    assert parsed["auto_review"]["extra_policy"] == "abc"
    assert parsed["model"] == ("host" if existing is None else "session")
    if existing is not None:
        without_leaf = result.replace(b'extra_policy = "abc"\n', b"")
        if "[auto_review]" not in existing:
            without_leaf = without_leaf.removesuffix(b"\n[auto_review]\n")
        assert without_leaf == existing.encode()
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    before = path.stat()
    _populate_codex_home_config(target, source)
    assert path.read_bytes() == result
    assert path.stat() == before
    assert (source / "config.toml").read_text() == _SOURCE


@pytest.mark.parametrize(
    "source_text,target_text,reason",
    [
        (
            _SOURCE,
            _PRIVATE + '[auto_review]\nextra_policy = "different"\n',
            "TARGET_POLICY_CONFLICT",
        ),
        (
            _SOURCE,
            _PRIVATE + '[auto_review]\npolicy = "replacement"\n',
            "TARGET_REPLACEMENT_POLICY",
        ),
        (_SOURCE, 'auto_review = "scalar"\n', "TARGET_POLICY_CONFLICT"),
        (_SOURCE, 'invalid = ["DO_NOT_LOG",\n', "UnexpectedCharError"),
        (_PRIVATE, _PRIVATE, "SOURCE_POLICY_MISSING"),
        (_SOURCE.replace('"abc"', '"other"'), _PRIVATE, "SOURCE_POLICY_MISMATCH"),
        (_SOURCE.replace('"abc"', "42"), _PRIVATE, "SOURCE_POLICY_MISMATCH"),
        (_SOURCE + 'policy = "replacement"\n', _PRIVATE, "SOURCE_REPLACEMENT_POLICY"),
        ('invalid = ["DO_NOT_LOG",\n', _PRIVATE, "TOML"),
        (None, _PRIVATE, "SOURCE_POLICY_MISSING"),
    ],
)
def test_populate_conflicts_keep_the_file_and_do_not_log_config(
    homes: tuple[Path, Path],
    caplog: pytest.LogCaptureFixture,
    source_text: str | None,
    target_text: str,
    reason: str,
) -> None:
    source, target = homes
    source_path, target_path = source / "config.toml", target / "config.toml"
    if source_text is None:
        source_path.unlink()
    else:
        source_path.write_text(source_text)
    target_path.write_text(target_text)
    before = target_path.stat()
    _populate_codex_home_config(target, source)
    assert target_path.read_text() == target_text
    assert target_path.stat() == before
    assert reason in caplog.text
    assert "DO_NOT_LOG" not in caplog.text
    assert "replacement" not in caplog.text


@pytest.mark.parametrize("linked", ["source", "private"])
def test_symlink_is_not_read_or_replaced(
    homes: tuple[Path, Path], linked: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    source, target = homes
    outside = tmp_path / "outside.toml"
    outside.write_text(_SOURCE)
    path = (source if linked == "source" else target) / "config.toml"
    path.unlink(missing_ok=True)
    path.symlink_to(outside)
    _populate_codex_home_config(target, source)
    assert path.is_symlink()
    assert outside.read_text() == _SOURCE
    assert "UNSAFE_FILE" in caplog.text


@pytest.mark.parametrize("changed", ["source", "private", "new_private"])
def test_change_during_preparation_aborts_without_overwriting(
    homes: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    changed: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    source, target = homes
    target_path = target / "config.toml"
    if changed != "new_private":
        target_path.write_text(_PRIVATE)
    concurrent = 'model = "concurrent"\n'

    def change_during_flush(fd: int) -> None:
        path = source / "config.toml" if changed == "source" else target_path
        path.write_text(concurrent)

    monkeypatch.setattr(codex_config_sync.os, "fsync", change_during_flush)
    _populate_codex_home_config(target, source)
    assert target_path.read_text() == (_PRIVATE if changed == "source" else concurrent)
    assert "conflict" in caplog.text
    assert sorted(p.name for p in target.iterdir()) == ["config.toml"]


def test_atomic_write_failure_keeps_original(
    homes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    source, target = homes
    target_path = target / "config.toml"
    target_path.write_text(_PRIVATE)

    def fail_replace(*args: object) -> None:
        raise OSError("DO_NOT_LOG")

    monkeypatch.setattr(codex_config_sync.os, "replace", fail_replace)
    _populate_codex_home_config(target, source)
    assert target_path.read_text() == _PRIVATE
    assert sorted(p.name for p in target.iterdir()) == ["config.toml"]
    assert "conflict" in caplog.text
    assert "DO_NOT_LOG" not in caplog.text


@pytest.mark.parametrize(
    "pins", [None, {}, {"auto_review.extra_policy": "invalid"}, {"model": _ABC_SHA256}]
)
def test_missing_pins_do_not_migrate_and_invalid_pins_conflict(
    homes: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    pins: object,
    caplog: pytest.LogCaptureFixture,
) -> None:
    source, target = homes
    path = target / "config.toml"
    path.write_text(_PRIVATE)
    monkeypatch.setattr(
        codex_config_sync, "load_global_config", lambda: {"codex_config_sync": pins}
    )
    _populate_codex_home_config(target, source)
    assert path.read_text() == _PRIVATE
    assert ("INVALID_SYNC_CONFIG" in caplog.text) == bool(pins)


@pytest.mark.parametrize("existing", [False, True])
def test_signer_gets_only_noncredential_leaf(homes: tuple[Path, Path], existing: bool) -> None:
    source, target = homes
    (source / "config.toml").write_text('model_provider = "private"\n' + _SOURCE)
    path = target / "config.toml"
    if existing:
        path.write_text(_PRIVATE)
    _populate_codex_home_config(target, source, minimal_config=True, include_credentials=False)
    data = tomllib.loads(path.read_text())
    assert data["auto_review"] == {"extra_policy": "abc"}
    assert "model_provider" not in data
    assert data.get("model") == ("session" if existing else None)


def test_minimal_probe_keeps_its_narrow_copy(homes: tuple[Path, Path]) -> None:
    source, target = homes
    _populate_codex_home_config(target, source, minimal_config=True)
    assert tomllib.loads((target / "config.toml").read_text()) == {}


@pytest.mark.parametrize("signer", [False, True])
def test_stance_refresh_without_pins_tracks_source_and_removes_stale_keys(
    homes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, signer: bool
) -> None:
    source, target = homes
    monkeypatch.setattr(codex_config_sync, "load_global_config", dict)
    path = target / "config.toml"
    path.write_text(
        'model = "session"\napproval_policy = "never"\n'
        'sandbox_mode = "danger-full-access"\ndefault_permissions = ":danger-full-access"\n'
    )
    (source / "config.toml").write_text(
        'approval_policy = "on-request"\nsandbox_mode = "workspace-write"\n'
        'approvals_reviewer = "auto_review"\n'
    )
    for _restart in range(2):
        _populate_codex_home_config(
            target, source, minimal_config=signer, include_credentials=not signer
        )
        assert tomllib.loads(path.read_text()) == {
            "model": "session",
            "approval_policy": "on-request",
            "sandbox_mode": "workspace-write",
            "approvals_reviewer": "auto_review",
        }
    (source / "config.toml").write_text('default_permissions = ":workspace"\n')
    _populate_codex_home_config(
        target, source, minimal_config=signer, include_credentials=not signer
    )
    assert tomllib.loads(path.read_text()) == {
        "model": "session",
        "default_permissions": ":workspace",
    }


@pytest.mark.parametrize(
    "conflict", ["invalid_pin", "source_policy", "target_policy", "missing_source"]
)
def test_policy_conflict_does_not_block_permission_refresh(
    homes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, conflict: str
) -> None:
    source, target = homes
    source_path, target_path = source / "config.toml", target / "config.toml"
    source_path.write_text('approvals_reviewer = "auto_review"\n' + _SOURCE)
    target_path.write_text(
        'approval_policy = "never"\nsandbox_mode = "danger-full-access"\n'
        '[auto_review]\nextra_policy = "different"\n'
    )
    if conflict == "invalid_pin":
        monkeypatch.setattr(
            codex_config_sync,
            "load_global_config",
            lambda: {"codex_config_sync": {"auto_review.extra_policy": "invalid"}},
        )
    elif conflict == "source_policy":
        source_path.write_text('approvals_reviewer = "auto_review"\n')
    elif conflict == "missing_source":
        source_path.unlink()
    _populate_codex_home_config(target, source)
    assert tomllib.loads(target_path.read_text()) == {
        **({"approvals_reviewer": "auto_review"} if conflict != "missing_source" else {}),
        "auto_review": {"extra_policy": "different"},
    }


def test_windows_sandbox_still_refreshes(
    homes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target = homes
    (source / "config.toml").write_text('windows.sandbox = "unelevated"\n' + _SOURCE)
    path = target / "config.toml"
    path.write_text('windows.sandbox = "disabled"\n' + _PRIVATE)
    monkeypatch.setattr("omnigent.inner.codex_executor.sys.platform", "win32")
    _populate_codex_home_config(target, source)
    data = tomllib.loads(path.read_text())
    assert data["windows"]["sandbox"] == "unelevated"
    assert data["auto_review"]["extra_policy"] == "abc"
    assert data["model"] == "session"


@pytest.mark.parametrize(
    "target_text",
    [
        "auto_review.other = true\n",
        "auto_review = { other = true }\n",
        "[auto_review]\r\nother = true\r\n",
    ],
)
def test_existing_toml_spellings_keep_unrelated_settings(
    homes: tuple[Path, Path], target_text: str
) -> None:
    source, target = homes
    path = target / "config.toml"
    path.write_bytes(target_text.encode())
    _populate_codex_home_config(target, source)
    assert tomllib.loads(path.read_text()) == {
        "auto_review": {"other": True, "extra_policy": "abc"}
    }
    assert (
        target_text.rstrip() in path.read_bytes().decode() or "{ other = true," in path.read_text()
    )


def test_hash_covers_toml_value_including_trailing_newline(
    homes: tuple[Path, Path], caplog: pytest.LogCaptureFixture
) -> None:
    source, target = homes
    (source / "config.toml").write_text('[auto_review]\nextra_policy = """\nabc\n"""\n')
    (target / "config.toml").write_text(_PRIVATE)
    _populate_codex_home_config(target, source)
    assert (target / "config.toml").read_text() == _PRIVATE
    assert "SOURCE_POLICY_MISMATCH" in caplog.text


def test_equal_target_is_untouched(homes: tuple[Path, Path]) -> None:
    source, target = homes
    path = target / "config.toml"
    content = b"# keep encoding and quoting\r\nauto_review.extra_policy = 'abc'\r\n"
    path.write_bytes(content)
    before = path.stat().st_mtime_ns, path.stat().st_ino
    _populate_codex_home_config(target, source)
    assert path.read_bytes() == content
    assert (path.stat().st_mtime_ns, path.stat().st_ino) == before


def test_bad_deployment_yaml_is_sanitized(
    homes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import yaml

    source, target = homes
    path = target / "config.toml"
    path.write_text(_PRIVATE)

    def fail_load() -> dict[str, object]:
        raise yaml.YAMLError("DO_NOT_LOG")

    monkeypatch.setattr(codex_config_sync, "load_global_config", fail_load)
    _populate_codex_home_config(target, source)
    assert path.read_text() == _PRIVATE
    assert "YAMLError" in caplog.text
    assert "DO_NOT_LOG" not in caplog.text


@pytest.mark.parametrize("source_policy", ["abc", "changed", None])
async def test_native_startup_conflict_keeps_existing_initialization(
    homes: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_policy: str | None,
) -> None:
    from unittest.mock import AsyncMock, Mock

    from omnigent.harnesses.codex_native import app_server
    from tests.harnesses.codex_native.app_server._support import (
        _disable_codex_startup_rpc,
        _test_app_server,
    )

    source, target = homes
    (source / "config.toml").write_text(
        _SOURCE.replace('"abc"', f'"{source_policy}"') if source_policy is not None else _PRIVATE
    )
    path = target / "config.toml"
    content = _PRIVATE + '[auto_review]\nextra_policy = "different"\n'
    path.write_text(content)
    monkeypatch.setenv("CODEX_HOME", str(source))
    monkeypatch.setattr(app_server, "_codex_cli_version", AsyncMock(return_value=(0, 147, 0)))
    monkeypatch.setattr(app_server.CodexNativeAppServer, "_populate_context_catalog", AsyncMock())
    materialize = Mock(wraps=app_server.materialize_codex_provider_config)
    monkeypatch.setattr(app_server, "materialize_codex_provider_config", materialize)
    _disable_codex_startup_rpc(monkeypatch)
    server = _test_app_server(tmp_path, target, tmp_path / "bridge", tmp_path)
    try:
        await server.start()
        materialize.assert_called_once()
        assert tomllib.loads(path.read_text())["auto_review"]["extra_policy"] == "different"
        assert server.proc is not None
    finally:
        await server.close()


@pytest.mark.parametrize("source_policy", ["unapproved", None])
async def test_signer_startup_conflict_keeps_existing_initialization(
    homes: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_policy: str | None,
) -> None:
    from unittest.mock import AsyncMock, Mock

    from omnigent.inner import codex_executor
    from omnigent.inner.codex_worker import CodexWorkerLaunch
    from tests.inner.test_codex_executor import _FakeProcess
    from tests.inner.test_model_signer_lifecycle import _Pipe, _Signer

    source, _ = homes
    source_config = source / "config.toml"
    content = (
        _SOURCE.replace('"abc"', f'"{source_policy}"') if source_policy is not None else _PRIVATE
    )
    source_config.write_text(content)
    monkeypatch.setattr(codex_executor, "_codex_home_config_source_from_env", lambda: source)
    monkeypatch.setattr(codex_executor, "codex_home_staging_root", lambda: tmp_path)
    materialize = Mock(wraps=codex_executor.materialize_codex_provider_config)
    monkeypatch.setattr(codex_executor, "materialize_codex_provider_config", materialize)
    monkeypatch.setattr(
        codex_executor,
        "prepare_codex_worker",
        lambda **kwargs: CodexWorkerLaunch("/bin/echo", sandboxed=True),
    )
    process = _FakeProcess()
    process.stdout = _Pipe()
    process.stderr = _Pipe()
    monkeypatch.setattr(codex_executor, "_create_subprocess_exec", AsyncMock(return_value=process))
    signer = _Signer([])
    session = codex_executor._CodexAppServerSession(
        codex_path="/bin/echo",
        cwd=str(tmp_path),
        env={},
        tool_executor=None,
        codex_config_overrides=['model_providers.fixture.base_url="https://example.test/v1"'],
        signer_factory=lambda: signer,
    )
    session._request = AsyncMock(return_value={"result": {}})
    try:
        await session.start()
        materialize.assert_called_once()
        assert session._started
        assert not signer.closed
        assert session._codex_home_dir is not None
        data = tomllib.loads((session._codex_home_dir / "config.toml").read_text())
        assert "auto_review" not in data
        assert data["model_providers"]["fixture"]["base_url"] == "https://example.test/v1"
    finally:
        await session.close()
    assert signer.closed
    assert source_config.read_text() == content


@pytest.mark.parametrize(
    "conflict",
    [
        "source_missing",
        "source_mismatch",
        "target_different",
        "replacement",
        "symlink",
        "concurrent",
        "invalid_config",
        "target_invalid",
    ],
)
def test_conflict_keeps_base_role_and_windows_refresh(
    homes: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    conflict: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    source, target = homes
    source_path, target_path = source / "config.toml", target / "config.toml"
    routing = 'windows.sandbox = "unelevated"\n[agents]\ndefault_subagent_model = "host-role"\n'
    source_path.write_text(routing + '[auto_review]\nextra_policy = "abc"\n')
    private = (
        _PRIVATE
        + 'windows.sandbox = "disabled"\n[agents]\ndefault_subagent_model = "private-role"\n'
    )
    target_path.write_text(private + '[auto_review]\nextra_policy = "private"\n')
    monkeypatch.setattr("omnigent.inner.codex_executor.sys.platform", "win32")
    if conflict == "source_missing":
        source_path.write_text(routing)
    elif conflict == "source_mismatch":
        source_path.write_text(routing + '[auto_review]\nextra_policy = "changed"\n')
    elif conflict == "replacement":
        target_path.write_text(private + '[auto_review]\npolicy = "replacement"\n')
    elif conflict == "symlink":
        linked = tmp_path / "linked-source.toml"
        source_path.rename(linked)
        source_path.symlink_to(linked)
    elif conflict == "concurrent":
        target_path.write_text(private)

        def change_during_flush(fd: int) -> None:
            target_path.write_text(
                private.replace('"session"', '"concurrent"')
                + '[auto_review]\nextra_policy = "private"\n'
            )

        monkeypatch.setattr(codex_config_sync.os, "fsync", change_during_flush)
    elif conflict == "invalid_config":
        monkeypatch.setattr(
            codex_config_sync, "load_global_config", lambda: {"codex_config_sync": "invalid"}
        )
    elif conflict == "target_invalid":
        target_path.write_text('invalid = ["DO_NOT_LOG",\n')
    _populate_codex_home_config(target, source)
    assert "Codex config sync conflict" in caplog.text
    if conflict == "target_invalid":
        assert target_path.read_text() == 'invalid = ["DO_NOT_LOG",\n'
    else:
        data = tomllib.loads(target_path.read_text())
        assert data["model"] == ("concurrent" if conflict == "concurrent" else "session")
        assert data["agents"]["default_subagent_model"] == "host-role"
        assert data["windows"]["sandbox"] == "unelevated"
        assert data["auto_review"] == (
            {"policy": "replacement"} if conflict == "replacement" else {"extra_policy": "private"}
        )


def test_fresh_conflict_retains_base_copy_behavior(
    homes: tuple[Path, Path], caplog: pytest.LogCaptureFixture
) -> None:
    source, target = homes
    content = _SOURCE.replace('"abc"', '"changed"')
    (source / "config.toml").write_text(content)
    _populate_codex_home_config(target, source)
    assert (target / "config.toml").read_text() == content
    assert "SOURCE_POLICY_MISMATCH" in caplog.text
