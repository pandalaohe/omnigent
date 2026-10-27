"""Immutable Codex package snapshots for long-lived app-server processes."""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import stat
import tempfile
import time
from pathlib import Path

import psutil
from filelock import FileLock

from omnigent.process_logging import data_dir

_logger = logging.getLogger(__name__)
_MARKER = ".omnigent-snapshot.json"
_RETENTION_SECONDS = 7 * 24 * 60 * 60
CODEX_RUNTIME_BIN_ENV = "OMNIGENT_CODEX_RUNTIME_BIN"
_NPM_CMD_SHIM = r"""@ECHO off
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

""" + (
    r"endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "
    r'set PATHEXT=%PATHEXT:;.JS;=;% & "%_prog%"  '
    r'"%dp0%\node_modules\@openai\codex\bin\codex.js" %*'
)
_NATIVE_HEADERS = {
    b"\x7fELF",
    b"MZ",
    b"\xfe\xed\xfa\xce",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",
}


def _json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("expected an object")
    return value


def _native(path: Path) -> bool:
    with path.open("rb") as stream:
        header = stream.read(4)
    return header in _NATIVE_HEADERS or header[:2] == b"MZ"


def _resolve_vendor_binary(path: Path) -> Path | None:
    """Recognize native packages and the official npm shim, never unwrap custom scripts."""
    path = path.resolve(strict=True)
    if _native(path):
        return path
    if path.name.lower() == "codex.cmd":
        if path.read_text(encoding="utf-8-sig").strip() != _NPM_CMD_SHIM:
            return None
        path = path.parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    if path.name != "codex.js" or path.parent.name != "bin":
        return None
    package = path.parent.parent
    if _json(package / "package.json").get("name") != "@openai/codex":
        return None
    shim = path.read_text(encoding="utf-8")
    if not all(
        text in shim
        for text in (
            "// Unified entry point for the Codex CLI.",
            "const binaryPath = findCodexExecutable();",
            "spawn(binaryPath, process.argv.slice(2)",
        )
    ):
        return None
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64", "AMD64": "x64"}.get(
        platform.machine()
    )
    system = {"Darwin": "darwin", "Linux": "linux", "Windows": "win32"}.get(platform.system())
    if system is None or arch is None:
        return None
    target = {
        "darwin": f"{'aarch64' if arch == 'arm64' else 'x86_64'}-apple-darwin",
        "linux": f"{'aarch64' if arch == 'arm64' else 'x86_64'}-unknown-linux-musl",
        "win32": f"{'aarch64' if arch == 'arm64' else 'x86_64'}-pc-windows-msvc",
    }[system]
    executable = "codex.exe" if system == "win32" else "codex"
    dependency = f"codex-{system}-{arch}"
    # Mirror createRequire(codex.js)'s nearest node_modules resolution order.
    for parent in (package, *package.parents):
        optional = parent / "node_modules" / "@openai" / dependency
        if (optional / "package.json").is_file():
            candidate = optional / "vendor" / target / "bin" / executable
            return candidate.resolve(strict=True) if _native(candidate) else None
    candidate = package / "vendor" / target / "bin" / executable
    return candidate.resolve(strict=True) if candidate.is_file() and _native(candidate) else None


def _package(binary: Path) -> tuple[Path, str, str] | None:
    if binary.parent.name != "bin":
        return None
    root = binary.parent.parent
    manifest = _json(root / "codex-package.json")
    version, target = manifest.get("version"), manifest.get("target")
    if (
        manifest.get("layoutVersion") != 1
        or manifest.get("entrypoint") != f"bin/{binary.name}"
        or not isinstance(version, str)
        or re.fullmatch(r"\d+\.\d+\.\d+(?:[-.][A-Za-z0-9.-]+)?", version) is None
        or not isinstance(target, str)
        or re.fullmatch(r"[A-Za-z0-9_-]+", target) is None
        or manifest.get("resourcesDir") not in (None, "codex-resources")
        or manifest.get("pathDir") not in (None, "codex-path")
    ):
        return None
    host = "codex-code-mode-host.exe" if binary.suffix == ".exe" else "codex-code-mode-host"
    if not any((root / directory / host).is_file() for directory in ("bin", "codex-resources")):
        return None
    return root, version, target


def _identity(root: Path) -> dict[str, tuple[int, int, int, int]]:
    result = {}
    for path in root.rglob("*"):
        if path.is_dir() and (path.is_symlink() or path.is_junction()):
            raise OSError("Codex package contains a linked directory")
        if path.is_file():
            info = path.stat()
            result[str(path.relative_to(root))] = (
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
            )
    return result


def _owner_alive(path: Path) -> bool | None:
    try:
        owner = _json(path)
        pid, born = owner["pid"], owner["create_time"]
        if not isinstance(pid, int) or not isinstance(born, (int, float)):
            return None
        return psutil.Process(pid).create_time() == born
    except psutil.NoSuchProcess:
        return False
    except (OSError, ValueError, KeyError, psutil.Error):
        return None


def _reap_unused(root: Path, now: float) -> None:
    candidates = []
    for entry in root.iterdir():
        if entry.is_symlink() or not entry.is_dir() or not (entry / _MARKER).is_file():
            continue
        if now - entry.stat().st_mtime <= _RETENTION_SECONDS:
            continue
        try:
            marker = _json(entry / _MARKER)
            if entry.name != f"{marker['version']}-{marker['target']}":
                continue
        except (OSError, ValueError, KeyError):
            continue
        owners = entry / ".owners"
        if not owners.is_dir() or any(
            _owner_alive(owner) is not False for owner in owners.iterdir()
        ):
            continue
        candidates.append(entry)
    if not candidates:
        return
    # A crashed launcher can leave a live child. Unknown process visibility retains everything.
    try:
        for process in psutil.process_iter():
            try:
                executable = process.exe()
            except psutil.NoSuchProcess:
                continue
            if executable:
                candidates = [
                    entry for entry in candidates if not Path(executable).is_relative_to(entry)
                ]
    except psutil.Error:
        return
    for entry in candidates:
        shutil.rmtree(entry)


def _resolved_package(codex_path: str) -> tuple[Path, Path, str, str] | None:
    try:
        binary = _resolve_vendor_binary(Path(codex_path))
        package = _package(binary) if binary is not None else None
    except (OSError, ValueError):
        return None
    return (binary, *package) if binary is not None and package is not None else None


def snapshot_codex_binary(codex_path: str, *, env: dict[str, str] | None = None) -> str:
    """Pin recognized packages; ambiguous wrappers retain their original launch behavior.

    Snapshots and their owner records share a cross-process lock with collection.
    A live launcher retains its selection even between worker launches; orphaned
    child executables are checked before old, unowned snapshots can be removed.

    When supplied, env receives a runtime hint for an unrecognized wrapper,
    selected from the first recognized executable on its PATH. The wrapper
    remains the launcher. An inherited hint is cleared before selecting one.
    """
    if env is not None:
        env.pop(CODEX_RUNTIME_BIN_ENV, None)
    resolved = _resolved_package(codex_path)
    if resolved is None:
        if env is not None:
            names = ("codex.exe", "codex.cmd", "codex") if os.name == "nt" else ("codex",)
            for directory in os.get_exec_path(env):
                for name in names:
                    candidate = str(Path(directory) / name)
                    package = (
                        _resolved_package(candidate) if os.access(candidate, os.X_OK) else None
                    )
                    if package is not None:
                        env[CODEX_RUNTIME_BIN_ENV] = _snapshot_package(package)
                        return codex_path
        _logger.debug("Codex executable package is not unambiguously resolvable; keeping launcher")
        return codex_path
    return _snapshot_package(resolved)


def _snapshot_package(resolved: tuple[Path, Path, str, str]) -> str:
    binary, source, version, target = resolved
    root = data_dir().resolve() / "codex-bin"
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    if not stat.S_ISDIR(root.lstat().st_mode):
        raise OSError("Codex snapshot root must be a real directory")
    if hasattr(os, "getuid") and root.stat().st_uid != os.getuid():
        raise OSError("Codex snapshot root must be owned by the current user")
    root.chmod(0o700)
    with FileLock(str(root / ".lock")):
        before = _identity(source)
        if _package(binary) != (source, version, target):
            raise OSError("Codex installation changed before copying; retry startup")
        destination = root / f"{version}-{target}"
        if not destination.exists():
            with tempfile.TemporaryDirectory(prefix=".building-", dir=root) as temporary:
                copied = Path(temporary) / "package"
                shutil.copytree(source, copied)
                if _identity(source) != before:
                    raise OSError("Codex installation changed while copying; retry startup")
                if _package(copied / "bin" / binary.name) != (copied, version, target):
                    raise OSError("Copied Codex package does not match its selected version")
                (copied / _MARKER).write_text(json.dumps({"version": version, "target": target}))
                (copied / ".owners").mkdir()
                copied.rename(destination)
        if destination.is_symlink() or _json(destination / _MARKER) != {
            "version": version,
            "target": target,
        }:
            raise OSError("Codex snapshot is incomplete or unrecognized")
        owner = {"pid": os.getpid(), "create_time": psutil.Process().create_time()}
        (destination / ".owners" / str(os.getpid())).write_text(json.dumps(owner))
        now = time.time()
        os.utime(destination, (now, now))
        try:
            _reap_unused(root, now)
        except OSError:
            _logger.warning("Could not collect unused Codex snapshots", exc_info=True)
        return str(destination / "bin" / binary.name)


def codex_binary_read_root(codex_path: str) -> Path:
    """Grant the copied resources together with a snapshot's executable directory."""
    binary = Path(codex_path).resolve(strict=False)
    package = binary.parent.parent
    return package if (package / _MARKER).is_file() else binary.parent
