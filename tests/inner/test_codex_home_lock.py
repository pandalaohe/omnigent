import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from omnigent.harnesses.codex_native.app_server import (
    _pin_codex_config_model,
    _sync_codex_developer_instructions,
)
from omnigent.harnesses.codex_native.bridge import _upsert_top_level_config_key
from omnigent.harnesses.codex_native.launch_args import materialize_codex_config_profile
from omnigent.inner.codex_executor import _sync_codex_role_defaults, _sync_codex_windows_sandbox
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
