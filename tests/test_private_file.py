"""Private config writes protect the actual destination and fail without truncation."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from omnigent.config import save_global_config
from omnigent.private_file import write_private_text


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits; Windows uses a protected DACL")
@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("redirected", [False, True])
@pytest.mark.parametrize("writer", ["config", "cli-global", "cli-local"])
def test_config_writers_protect_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: bool, redirected: bool, writer: str
) -> None:
    from omnigent import cli

    monkeypatch.chdir(tmp_path)
    from omnigent import config

    if redirected:
        monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "redirected"))
    else:
        monkeypatch.delenv("OMNIGENT_CONFIG_HOME", raising=False)
        default = tmp_path / "home" / ".omnigent" / "config.yaml"
        monkeypatch.setattr(config, "_GLOBAL_CONFIG_PATH", default)
        monkeypatch.setattr(cli, "_GLOBAL_CONFIG_PATH", default)
    target = (
        tmp_path / ".omnigent" / "config.yaml"
        if writer == "cli-local"
        else config.global_config_path()
    )
    target.parent.mkdir(parents=True)
    if existing:
        target.write_text("retained: yes\n")
        target.chmod(0o644)
    if writer == "config":
        save_global_config({"server": "https://example.test"})
    elif writer == "cli-global":
        cli._save_global_config({"server": "https://example.test"})
    else:
        cli._save_local_config({"server": "https://example.test"})
    assert "https://example.test" in target.read_text()
    assert target.stat().st_mode & 0o777 == 0o600
    if existing:
        assert "retained" in target.read_text()


@pytest.mark.skipif(os.name == "nt", reason="POSIX protection failure injection")
def test_permission_failure_preserves_existing_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "config.yaml"
    target.write_text("old content")

    def denied(*args: object) -> None:
        raise PermissionError("permission denied")

    monkeypatch.setattr(os, "fchmod", denied)
    with pytest.raises(PermissionError):
        write_private_text(target, "replacement")
    assert target.read_text() == "old content"
    assert list(tmp_path.iterdir()) == [target]


def test_private_file_preserves_symlink_destination(tmp_path: Path) -> None:
    target = tmp_path / "actual.yaml"
    target.write_text("old content")
    link = tmp_path / "config.yaml"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation unavailable")
    write_private_text(link, "new content")
    assert link.is_symlink()
    assert target.read_text() == "new content"
    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o600
