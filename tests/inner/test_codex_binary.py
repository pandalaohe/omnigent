"""Package pinning, conservative wrapper resolution and snapshot retention."""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import psutil
import pytest

from omnigent.inner import codex_binary as module


def _vendor(root: Path, *, target: str = "fixture-target", windows: bool = False) -> Path:
    binary = root / "bin" / ("codex.exe" if windows else "codex")
    binary.parent.mkdir(parents=True)
    binary.write_bytes((b"MZ00" if windows else b"\x7fELF") + b"old codex")
    binary.chmod(0o755)
    host = binary.with_name("codex-code-mode-host.exe" if windows else "codex-code-mode-host")
    host.write_bytes(b"old host")
    (root / "codex-resources").mkdir()
    (root / "codex-resources" / "data").write_text("old resource")
    (root / "codex-package.json").write_text(
        json.dumps(
            {
                "layoutVersion": 1,
                "version": "0.164.0",
                "target": target,
                "entrypoint": f"bin/{binary.name}",
                "resourcesDir": "codex-resources",
            }
        )
    )
    return binary


@pytest.fixture
def package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(module, "data_dir", lambda: tmp_path / "data")
    return _vendor(tmp_path / "vendor")


def test_update_cannot_change_a_selected_package(package: Path) -> None:
    selected = Path(module.snapshot_codex_binary(str(package)))
    package.write_bytes(b"new global codex")
    package.with_name("codex-code-mode-host").write_bytes(b"new global host")
    assert selected.read_bytes() == b"\x7fELFold codex"
    assert selected.with_name("codex-code-mode-host").read_bytes() == b"old host"
    assert selected.stat().st_ino != package.stat().st_ino
    assert module.codex_binary_read_root(str(selected)) == selected.parent.parent
    assert (selected.parent.parent / "codex-resources" / "data").read_text() == "old resource"
    # Reusing the pinned path cannot follow the changed global installation.
    assert module.snapshot_codex_binary(str(selected)) == str(selected)


@pytest.mark.parametrize("windows", [False, True])
def test_concurrent_launches_publish_one_complete_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows: bool
) -> None:
    monkeypatch.setattr(module, "data_dir", lambda: tmp_path / "data")
    binary = _vendor(tmp_path / "vendor", windows=windows)
    copy = Mock(wraps=module.shutil.copytree)
    # copytree recursively calls itself, so count only the source package root.
    original = module.shutil.copytree

    def copying(source, destination, *args, **kwargs):
        if Path(source) == binary.parent.parent:
            copy(source, destination, *args, **kwargs)
        else:
            original(source, destination, *args, **kwargs)

    monkeypatch.setattr(module.shutil, "copytree", copying)
    with ThreadPoolExecutor(max_workers=4) as executor:
        paths = list(executor.map(module.snapshot_codex_binary, [str(binary)] * 4))
    assert len(set(paths)) == 1
    assert Path(paths[0]).read_bytes() == binary.read_bytes()
    assert copy.call_count == 1
    assert not list((tmp_path / "data" / "codex-bin").glob(".building-*"))


@pytest.mark.parametrize("failure", ["copy", "update-during-copy"])
def test_failed_publication_can_retry_without_a_partial_version(
    package: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    original = module.shutil.copytree

    def copying(source, destination, *args, **kwargs):
        result = original(source, destination, *args, **kwargs)
        if Path(source) == package.parent.parent:
            if failure == "copy":
                raise OSError("copy failed")
            package.with_name("codex-code-mode-host").write_bytes(b"updated host")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(module.shutil, "copytree", copying)
        with pytest.raises(OSError, match=r"copy failed|installation changed"):
            module.snapshot_codex_binary(str(package))
    root = module.data_dir() / "codex-bin"
    assert list(root.iterdir()) == [root / ".lock"]
    selected = Path(module.snapshot_codex_binary(str(package)))
    assert selected.is_file()


@pytest.mark.parametrize(
    "system,machine,target,dependency,windows,entry",
    [
        ("Darwin", "arm64", "aarch64-apple-darwin", "codex-darwin-arm64", False, "js"),
        ("Linux", "x86_64", "x86_64-unknown-linux-musl", "codex-linux-x64", False, "js"),
        ("Windows", "AMD64", "x86_64-pc-windows-msvc", "codex-win32-x64", True, "js"),
        ("Windows", "AMD64", "x86_64-pc-windows-msvc", "codex-win32-x64", True, "cmd"),
        ("Windows", "AMD64", "x86_64-pc-windows-msvc", "codex-win32-x64", True, "custom-cmd"),
    ],
)
def test_official_npm_shim_uses_its_platform_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    system: str,
    machine: str,
    target: str,
    dependency: str,
    windows: bool,
    entry: str,
) -> None:
    monkeypatch.setattr(module, "data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(module.platform, "system", lambda: system)
    monkeypatch.setattr(module.platform, "machine", lambda: machine)
    package = tmp_path / "node_modules" / "@openai" / "codex"
    shim = package / "bin" / "codex.js"
    shim.parent.mkdir(parents=True)
    shim.write_text(
        "// Unified entry point for the Codex CLI.\n"
        "const binaryPath = findCodexExecutable();\nspawn(binaryPath, process.argv.slice(2));"
    )
    (package / "package.json").write_text('{"name":"@openai/codex"}')
    optional = package / "node_modules" / "@openai" / dependency
    binary = _vendor(optional / "vendor" / target, target=target, windows=windows)
    (optional / "package.json").write_text("{}")
    if entry != "js":
        shim = tmp_path / "codex.cmd"
        # npm/cmd-shim's node launcher, including its empty args field.
        cmd = r"""@ECHO off
GOTO start
:find_dp0
SET dp0=%~dp0
EXIT /b
:start
SETLOCAL
CALL :find_dp0

IF EXIST "%dp0%\node.exe" (
  SET "_prog=%dp0%\node.exe"
) ELSE (
  SET "_prog=node"
)

endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & """ + (
            r'set PATHEXT=%PATHEXT:;.JS;=;% & "%_prog%"  '
            r'"%dp0%\node_modules\@openai\codex\bin\codex.js" %*'
        )
        if entry == "custom-cmd":
            cmd = "@set CUSTOM_FLAG=1\n" + cmd
        shim.write_bytes((cmd + "\n").replace("\n", "\r\n").encode())
    selected = Path(module.snapshot_codex_binary(str(shim)))
    if entry == "custom-cmd":
        assert selected == shim
        assert not (tmp_path / "data").exists()
        return
    assert selected != binary
    assert selected.read_bytes() == binary.read_bytes()


@pytest.mark.parametrize("launcher", ["custom-wrapper", "codex.js", "codex.cmd"])
def test_ambiguous_wrappers_are_not_bypassed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launcher: str
) -> None:
    monkeypatch.setattr(module, "data_dir", lambda: tmp_path / "unused-data")
    path = tmp_path / launcher
    path.write_text("#!/usr/bin/env node\n// custom launch behavior\n")
    assert module.snapshot_codex_binary(str(path)) == str(path)
    assert not (tmp_path / "unused-data").exists()


@pytest.mark.parametrize("launch", ["wrapper-with-runtime", "wrapper-only", "direct"])
def test_wrapper_runtime_hint_preserves_launcher_and_owns_snapshot(
    package: Path, tmp_path: Path, launch: str
) -> None:
    wrapper = tmp_path / "wrapper" / "codex"
    wrapper.parent.mkdir()
    wrapper.write_text("#!/usr/bin/env node\n// preserves custom environment\n")
    wrapper.chmod(0o755)
    directories = [str(wrapper.parent)]
    if launch != "wrapper-only":
        directories.append(str(package.parent))
    env = {
        "PATH": os.pathsep.join(directories),
        "WRAPPER_SETTING": "preserve",
        "OMNIGENT_CODEX_RUNTIME_BIN": "stale inherited hint",
    }
    original = str(package if launch == "direct" else wrapper)
    selected = module.snapshot_codex_binary(original, env=env)
    assert env["WRAPPER_SETTING"] == "preserve"
    if launch == "wrapper-only":
        assert selected == original
        assert "OMNIGENT_CODEX_RUNTIME_BIN" not in env
        return
    if launch == "direct":
        assert selected != original
        assert "OMNIGENT_CODEX_RUNTIME_BIN" not in env
        runtime = Path(selected)
    else:
        assert selected == original
        runtime = Path(env["OMNIGENT_CODEX_RUNTIME_BIN"])
        assert runtime != package
    assert runtime.read_bytes() == b"\x7fELFold codex"
    snapshot = runtime.parent.parent
    owner = json.loads((snapshot / ".owners" / str(os.getpid())).read_text())
    assert owner["pid"] == os.getpid()
    stale = time.time() - 8 * 24 * 60 * 60
    os.utime(snapshot, (stale, stale))
    module._reap_unused(snapshot.parent, time.time())
    assert runtime.exists()


def test_path_candidate_removed_after_resolution_cannot_be_offered_as_runtime(
    package: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wrapper = tmp_path / "wrapper"
    wrapper.write_text("#!/usr/bin/env node\n")
    manifest = package.parent.parent / "codex-package.json"
    contents = manifest.read_text()
    resolve = module._resolved_package

    def update_after_resolving(path):
        result = resolve(path)
        if result is not None:
            manifest.unlink()
        return result

    env = {"PATH": str(package.parent)}
    with monkeypatch.context() as patch:
        patch.setattr(module, "_resolved_package", update_after_resolving)
        with pytest.raises(OSError):
            module.snapshot_codex_binary(str(wrapper), env=env)
    assert "OMNIGENT_CODEX_RUNTIME_BIN" not in env
    manifest.write_text(contents)
    assert module.snapshot_codex_binary(str(wrapper), env=env) == str(wrapper)
    assert Path(env["OMNIGENT_CODEX_RUNTIME_BIN"]).is_file()


@pytest.mark.parametrize("state", ["recent", "owner", "child", "unknown", "denied", "unused"])
def test_gc_requires_old_age_dead_owners_and_no_live_executable(
    package: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    selected = Path(module.snapshot_codex_binary(str(package)))
    old = selected.parent.parent
    root = old.parent
    owner = next((old / ".owners").iterdir())
    if state != "owner":
        owner.write_text("bad owner" if state == "unknown" else '{"pid":4194305,"create_time":1}')
    if state != "recent":
        stale = time.time() - 8 * 24 * 60 * 60
        os.utime(old, (stale, stale))
    process = Mock()
    process.exe.return_value = str(selected)
    if state == "denied":
        process.exe.side_effect = psutil.AccessDenied()
    monkeypatch.setattr(
        module.psutil,
        "process_iter",
        lambda: iter([process] if state in {"child", "denied"} else []),
    )
    module._reap_unused(root, time.time())
    assert old.exists() is (state != "unused")


@pytest.mark.skipif(os.name == "nt", reason="directory symlinks need Windows privileges")
def test_snapshot_root_refuses_a_symlink(package: Path) -> None:
    root = module.data_dir() / "codex-bin"
    root.parent.mkdir()
    root.symlink_to(package.parent, target_is_directory=True)
    with pytest.raises(OSError, match="real directory"):
        module.snapshot_codex_binary(str(package))


def test_update_between_version_resolution_and_copy_is_rejected(
    package: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = module._identity
    manifest = package.parent.parent / "codex-package.json"

    def update_before_identity(root):
        value = json.loads(manifest.read_text())
        value["version"] = "0.165.0"
        manifest.write_text(json.dumps(value))
        return identity(root)

    with monkeypatch.context() as patch:
        patch.setattr(module, "_identity", update_before_identity)
        with pytest.raises(OSError, match="changed before copying"):
            module.snapshot_codex_binary(str(package))
    root = module.data_dir() / "codex-bin"
    assert list(root.iterdir()) == [root / ".lock"]
    assert "0.165.0" in module.snapshot_codex_binary(str(package))


@pytest.mark.skipif(os.name == "nt", reason="directory symlinks need Windows privileges")
def test_linked_package_directories_are_not_copied_as_unverified_contents(package: Path) -> None:
    resources = package.parent.parent / "codex-resources"
    outside = resources.parent.with_name("external-resources")
    resources.rename(outside)
    resources.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError, match="linked directory"):
        module.snapshot_codex_binary(str(package))
    assert (outside / "data").read_text() == "old resource"
