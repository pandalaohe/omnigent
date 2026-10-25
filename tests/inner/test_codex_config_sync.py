"""Digest-pinned configuration refresh at the private-home generation boundary."""

from __future__ import annotations

import os
import stat
from contextlib import nullcontext
from pathlib import Path

import pytest
import tomllib

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
    with pytest.raises(RuntimeError, match="Codex config sync conflict"):
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
    with pytest.raises(RuntimeError, match="Codex config sync conflict"):
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
    with pytest.raises(RuntimeError, match="Codex config sync conflict"):
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
    with pytest.raises(RuntimeError, match="Codex config sync conflict"):
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
    with (
        pytest.raises(RuntimeError, match="Codex config sync conflict") if pins else nullcontext()
    ):
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
    with pytest.raises(RuntimeError, match="Codex config sync conflict"):
        _populate_codex_home_config(target, source)
    assert not (target / "config.toml").exists()
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
    with pytest.raises(RuntimeError, match="Codex config sync conflict"):
        _populate_codex_home_config(target, source)
    assert path.read_text() == _PRIVATE
    assert "YAMLError" in caplog.text
    assert "DO_NOT_LOG" not in caplog.text


async def test_native_startup_conflict_stops_before_config_writers(
    homes: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import AsyncMock, Mock

    from omnigent.harnesses.codex_native import app_server
    from tests.harnesses.codex_native.app_server._support import _test_app_server

    source, target = homes
    path = target / "config.toml"
    content = _PRIVATE + '[auto_review]\nextra_policy = "different"\n'
    path.write_text(content)
    monkeypatch.setenv("CODEX_HOME", str(source))
    monkeypatch.setattr(app_server, "_codex_cli_version", AsyncMock(return_value=(0, 147, 0)))
    monkeypatch.setattr(app_server.CodexNativeAppServer, "_populate_context_catalog", AsyncMock())
    materialize = Mock(side_effect=AssertionError("later config writer reached"))
    monkeypatch.setattr(app_server, "materialize_codex_provider_config", materialize)
    server = _test_app_server(tmp_path, target, tmp_path / "bridge", tmp_path)
    try:
        with pytest.raises(RuntimeError, match="Codex config sync conflict"):
            await server.start()
        materialize.assert_not_called()
        assert path.read_text() == content
        assert server.proc is None
    finally:
        await server.close()


async def test_signer_startup_conflict_stops_before_config_writers(
    homes: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import Mock

    from omnigent.inner import codex_executor
    from tests.inner.test_model_signer_lifecycle import _Signer

    source, _ = homes
    source_config = source / "config.toml"
    content = _SOURCE.replace('"abc"', '"unapproved"')
    source_config.write_text(content)
    monkeypatch.setattr(codex_executor, "_codex_home_config_source_from_env", lambda: source)
    monkeypatch.setattr(codex_executor, "codex_home_staging_root", lambda: tmp_path)
    materialize = Mock(side_effect=AssertionError("later config writer reached"))
    monkeypatch.setattr(codex_executor, "materialize_codex_provider_config", materialize)
    signer = _Signer([])
    session = codex_executor._CodexAppServerSession(
        codex_path="/bin/echo",
        cwd=str(tmp_path),
        env={},
        tool_executor=None,
        signer_factory=lambda: signer,
    )
    with pytest.raises(RuntimeError, match="Codex config sync conflict"):
        await session.start()
    materialize.assert_not_called()
    assert signer.closed
    assert session._proc is None
    assert source_config.read_text() == content
