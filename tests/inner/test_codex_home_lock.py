import errno
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from omnigent.harnesses.codex_native.app_server import (
    _build_native_codex_app_server_argv,
    _pin_codex_config_model,
    _sync_codex_developer_instructions,
)
from omnigent.harnesses.codex_native.bridge import _upsert_top_level_config_key
from omnigent.harnesses.codex_native.launch_args import materialize_codex_config_profile
from omnigent.inner.codex_executor import (
    _populate_codex_home_config,
    _sync_codex_role_defaults,
    _sync_codex_windows_sandbox,
    materialize_codex_provider_config,
)
from omnigent.util import codex_home_lock


@pytest.mark.parametrize("writer", ["model", "roles", "windows", "instructions", "bridge"])
def test_writer_preserves_concurrent_relay_edit(tmp_path, monkeypatch, writer):
    config = tmp_path / "config.toml"
    config.write_text('model = "old"\nopenai_base_url = "https://old.example"\n')
    source = tmp_path / "source.toml"
    source.write_text(
        '[agents]\ndefault_subagent_model = "worker"\n[windows]\nsandbox = "elevated"\n'
    )
    read_text = Path.read_text
    edited = False

    def read_with_edit(path, *args, **kwargs):
        nonlocal edited
        text = read_text(path, *args, **kwargs)
        if path == config and not edited:
            edited = True
            config.write_text(text.replace("old.example", "new.example"))
        return text

    monkeypatch.setattr(Path, "read_text", read_with_edit)
    if writer == "model":
        _pin_codex_config_model(tmp_path, "new-model")
    elif writer == "roles":
        _sync_codex_role_defaults(source, config)
    elif writer == "windows":
        _sync_codex_windows_sandbox(source, config)
    elif writer == "instructions":
        _sync_codex_developer_instructions(tmp_path, "agent instruction")
    else:
        assert _upsert_top_level_config_key(config, "model", "new-model")
    assert 'openai_base_url = "https://new.example"' in config.read_text()
    expected = {
        "model": 'model = "new-model"',
        "roles": 'default_subagent_model = "worker"',
        "windows": 'sandbox = "elevated"',
        "instructions": "agent instruction",
        "bridge": 'model = "new-model"',
    }
    assert expected[writer] in config.read_text()


def test_profile_retry_restores_prepared_journal(tmp_path, monkeypatch):
    home, source = tmp_path / "private", tmp_path / "source"
    home.mkdir()
    source.mkdir()
    config = home / "config.toml"
    config.write_text('openai_base_url = "https://old.example"\n')
    (source / "work.config.toml").write_text('model = "work-model"\n')
    read_text = Path.read_text
    edited = False

    def read_with_edit(path, *args, **kwargs):
        nonlocal edited
        text = read_text(path, *args, **kwargs)
        if path == config and not edited:
            edited = True
            config.write_text(text.replace("old.example", "new.example"))
        return text

    monkeypatch.setattr(Path, "read_text", read_with_edit)
    materialize_codex_config_profile(home, source, "work", codex_version=(0, 146, 0))
    assert "https://new.example" in config.read_text()
    assert "work-model" in config.read_text()
    materialize_codex_config_profile(home, source, None, codex_version=(0, 146, 0))
    assert "https://new.example" in config.read_text()
    assert "work-model" not in config.read_text()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX interoperability")
def test_external_holder_waits_then_skips_and_next_sync_succeeds(tmp_path, monkeypatch, caplog):
    config = tmp_path / "config.toml"
    original = 'model = "old"\n'
    config.write_text(original)
    lock = tmp_path / ".collab-generations" / "home.lock"
    lock.parent.mkdir()
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl,sys; "
            "f=open(sys.argv[1], 'a+'); fcntl.flock(f, fcntl.LOCK_EX); "
            "print('held', flush=True); sys.stdin.read()",
            str(lock),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "held"
        monkeypatch.setattr(codex_home_lock, "_TIMEOUT", 0.02)
        sleeps = []
        sleep = codex_home_lock.time.sleep

        def record_wait(seconds):
            sleeps.append(seconds)
            sleep(seconds)

        monkeypatch.setattr(codex_home_lock.time, "sleep", record_wait)
        _pin_codex_config_model(tmp_path, "new-model")
        assert sleeps
        assert config.read_text() == original
        assert "lock busy" in caplog.text
    finally:
        child.communicate(timeout=5)
    _pin_codex_config_model(tmp_path, "new-model")
    assert 'model = "new-model"' in config.read_text()


def test_exception_releases_lock_and_reentry_uses_same_holder(tmp_path):
    config = tmp_path / "config.toml"
    with pytest.raises(ValueError), codex_home_lock.home_lock(config):
        with codex_home_lock.home_lock(config):
            raise ValueError("failed writer")
    # A separate thread must acquire the OS lock, rather than reentering locally.
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(_pin_codex_config_model, tmp_path, "after-failure").result(timeout=3)
    assert "after-failure" in config.read_text()


def test_windows_uses_msvcrt_byte_zero(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setitem(
        sys.modules,
        "msvcrt",
        SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=lambda *args: calls.append(args)),
    )
    with (tmp_path / "lock").open("a+b") as handle:
        handle.seek(5)
        codex_home_lock._windows_lock(handle.fileno())
        assert handle.tell() == 0
        codex_home_lock._windows_lock(handle.fileno(), release=True)
        assert calls == [(handle.fileno(), 2, 1), (handle.fileno(), 0, 1)]


@pytest.mark.parametrize("failure", ["busy", "changed"])
def test_provider_write_failure_refuses_credential_bearing_argv(tmp_path, monkeypatch, failure):
    home = tmp_path / "private"
    home.mkdir()
    config = home / "config.toml"
    config.write_text('model = "original"\n')
    overrides = [
        'model_provider="custom"',
        'model_providers.custom.name="Custom"',
        'model_providers.custom.base_url="https://provider.example/v1"',
        'model_providers.custom.experimental_bearer_token="test-only-credential"',
        'model_providers.custom.auth.command="test-only-auth-command"',
    ]
    held, release = threading.Event(), threading.Event()

    def hold_home():
        with codex_home_lock.home_lock(config):
            held.set()
            assert release.wait(5)

    def build_argv():
        safe = materialize_codex_provider_config(home, iter(overrides))
        return _build_native_codex_app_server_argv(
            codex_argv0="codex",
            session_tag="test-session",
            listen_url="unix:///opt/work/codex.sock",
            config_overrides=safe,
        )

    argv = []
    if failure == "busy":
        monkeypatch.setattr(codex_home_lock, "_TIMEOUT", 0.01)
        with ThreadPoolExecutor(max_workers=1) as executor:
            holder = executor.submit(hold_home)
            try:
                assert held.wait(3)
                with pytest.raises(codex_home_lock.HomeBusy, match=r"retry.*launch"):
                    argv.extend(build_argv())
            finally:
                release.set()
                holder.result(timeout=3)
    else:
        read_text = Path.read_text
        edits = 0

        def read_and_change(path, *args, **kwargs):
            nonlocal edits
            text = read_text(path, *args, **kwargs)
            if path == config:
                edits += 1
                config.write_text(f'model = "concurrent-{edits}"\n')
            return text

        monkeypatch.setattr(Path, "read_text", read_and_change)
        with pytest.raises(codex_home_lock.HomeBusy, match=r"retry.*launch"):
            argv.extend(build_argv())
        assert edits == 3
    assert argv == []
    assert "test-only-credential" not in config.read_bytes().decode()
    assert "test-only-auth-command" not in config.read_bytes().decode()


def test_plain_home_initialization_writes_nested_route_record(tmp_path, monkeypatch, caplog):
    source, private, home = tmp_path / "source", tmp_path / "private", tmp_path / "home"
    source.mkdir()
    (source / "config.toml").write_text('model = "source"\n')
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(codex_home_lock, "_TIMEOUT", 0.01)
    _populate_codex_home_config(private, source, minimal_config=False)
    assert (private / ".omnigent-codex-route").read_text() == "none"
    assert "lock busy" not in caplog.text


@pytest.mark.parametrize("winerror", [5, 32])
def test_windows_replace_retries_sharing_conflict(tmp_path, monkeypatch, winerror):
    config = tmp_path / "config.toml"
    config.write_text('model = "old"\n')
    replace = codex_home_lock.os.replace
    attempts, waits = [], []

    def replace_with_reader(source, target):
        attempts.append(target)
        if len(attempts) <= 2:
            error = PermissionError(errno.EACCES, "reader holds file")
            monkeypatch.setattr(error, "winerror", winerror, raising=False)
            raise error
        replace(source, target)

    monkeypatch.setattr(codex_home_lock.os, "replace", replace_with_reader)
    monkeypatch.setattr(codex_home_lock.time, "sleep", waits.append)
    with codex_home_lock.home_lock(config):
        codex_home_lock.write_config(config, 'model = "new"\n')
    assert config.read_text() == 'model = "new"\n'
    assert len(attempts) == 3
    assert waits == [0.05, 0.05]


def test_windows_replace_rechecks_config_after_sharing_conflict(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text('openai_base_url = "https://old.example"\n')
    concurrent = 'openai_base_url = "https://new.example"\n'
    attempts = []

    def replace_and_change(source, target):
        attempts.append(target)
        config.write_text(concurrent)
        error = PermissionError(errno.EACCES, "reader holds file")
        monkeypatch.setattr(error, "winerror", 32, raising=False)
        raise error

    monkeypatch.setattr(codex_home_lock.os, "replace", replace_and_change)
    monkeypatch.setattr(codex_home_lock.time, "sleep", lambda seconds: None)
    with codex_home_lock.home_lock(config), pytest.raises(codex_home_lock.ConfigChanged):
        codex_home_lock.write_config(config, 'model = "stale-candidate"\n')
    assert config.read_text() == concurrent
    assert len(attempts) == 1


@pytest.mark.parametrize("winerror, count", [(None, 1), (5, 5), (32, 5)])
def test_replace_retry_bound_keeps_original(tmp_path, monkeypatch, winerror, count):
    config = tmp_path / "config.toml"
    original = 'model = "original"\n'
    config.write_text(original)
    errors = []

    def fail_replace(source, target):
        error = PermissionError(errno.EACCES, "file unavailable")
        monkeypatch.setattr(error, "winerror", winerror, raising=False)
        errors.append(error)
        raise error

    monkeypatch.setattr(codex_home_lock.os, "replace", fail_replace)
    monkeypatch.setattr(codex_home_lock.time, "sleep", lambda seconds: None)
    with codex_home_lock.home_lock(config), pytest.raises(PermissionError) as failure:
        codex_home_lock.write_config(config, 'model = "candidate"\n')
    assert config.read_text() == original
    assert len(errors) == count
    assert failure.value is errors[0]
