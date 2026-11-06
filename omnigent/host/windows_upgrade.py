"""Windows process shutdown and rename-aside for synchronous CLI upgrades."""

from __future__ import annotations

import contextlib
import filecmp
import os
import shutil
import sys
import tomllib
import uuid
from collections.abc import Iterator
from pathlib import Path
from time import time

import psutil


def installed_launchers() -> list[Path]:
    """Return entrypoints owned by this venv, including uv's external copies."""
    prefix = Path(sys.prefix)
    scripts = prefix / "Scripts"
    receipt = prefix / "uv-receipt.toml"
    candidates = [scripts / f"{name}.exe" for name in ("omni", "omnigent")]
    if receipt.is_file():
        tool = tomllib.loads(receipt.read_text(encoding="utf-8"))["tool"]
        candidates = [
            Path(entry["install-path"])
            for entry in tool.get("entrypoints", [])
            if entry.get("name") in ("omni", "omnigent")
            and entry.get("from", "omnigent") == "omnigent"
        ]
    return [
        path
        for path in candidates
        if path.is_absolute()
        and path.name.casefold() in ("omni.exe", "omnigent.exe")
        and path.is_file()
        and (scripts / path.name).is_file()
        and filecmp.cmp(path, scripts / path.name, shallow=False)
    ]


def _fresh_processes() -> Iterator[psutil.Process]:
    """Avoid process_iter's cached Process identities after PID reuse."""
    for pid in psutil.pids():
        try:
            yield psutil.Process(pid)
        except psutil.NoSuchProcess:
            continue


def capture_host_tree() -> list[psutil.Process]:
    """Capture the installed Host and its autostart loop before disabling it."""
    autostart = Path.home() / ".omnigent" / "autostart"
    script = str(autostart / "Start-OmnigentHostDefault.ps1").casefold()
    pwsh = shutil.which("pwsh")
    executables = {str(path).casefold() for path in installed_launchers()}
    protected = {os.getpid(), *(proc.pid for proc in psutil.Process().parents())}
    captured: dict[int, psutil.Process] = {}
    for proc in _fresh_processes():
        try:
            args = proc.cmdline()
            exe = proc.exe().casefold()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        try:
            lowered = [arg.casefold() for arg in args]
            file_arg = 1
            while file_arg < len(args):
                option = lowered[file_arg]
                if option in ("-noprofile", "-nologo", "-noninteractive"):
                    file_arg += 1
                elif option in ("-executionpolicy", "-windowstyle"):
                    file_arg += 2
                else:
                    break
            loop = (
                pwsh is not None
                and exe == pwsh.casefold()
                and file_arg + 1 < len(args)
                and lowered[file_arg] == "-file"
                and lowered[file_arg + 1] == script
            )
            host = (
                exe in executables
                and len(args) >= 4
                and args[1:3] == ["host", "--server"]
                and bool(args[3])
            )
            if not (loop or host):
                continue
            tree = [proc, *proc.children(recursive=True)]
            if not proc.is_running() or proc.cmdline() != args:
                continue
            if any(child.pid in protected for child in tree):
                raise RuntimeError(
                    "Run the upgrade from a terminal outside the Host process tree."
                )
            captured.update((child.pid, child) for child in tree)
        except psutil.NoSuchProcess:
            continue
    return list(captured.values())


def stop_host_tree(processes: list[psutil.Process]) -> None:
    """Wait for supervisor shutdown, then terminate and reap captured stragglers."""
    if not processes:
        return
    captured = {proc.pid: proc for proc in processes}
    created = {proc.pid: proc.create_time() for proc in processes}
    ended: dict[int, float] = {}
    _, pending = psutil.wait_procs(list(captured.values()), timeout=2)
    while True:
        for proc in pending:
            stopping_at = time()
            try:
                proc.terminate()
            except psutil.NoSuchProcess:
                pass
            else:
                ended[proc.pid] = stopping_at
        _, alive = psutil.wait_procs(pending, timeout=5)
        for proc in alive:
            stopping_at = time()
            try:
                proc.kill()
            except psutil.NoSuchProcess:
                pass
            else:
                ended[proc.pid] = stopping_at
        _, alive = psutil.wait_procs(alive, timeout=5)
        if alive:
            raise RuntimeError(f"Host processes did not exit: {[proc.pid for proc in alive]}")
        pending = []
        current = {proc.pid: proc for proc in _fresh_processes()}
        # Exited, unrecorded intermediaries can hide orphans. Do not chase them;
        # if a survivor blocks staging/install, retained_install restores the old paths.
        for proc in current.values():
            try:
                parent = proc.ppid()
                if proc.pid in captured or parent not in created:
                    continue
                born = proc.create_time()
                if born < created[parent]:
                    continue
                parent_alive = (
                    parent in current
                    and current[parent].create_time() == created[parent]
                    and current[parent].is_running()
                )
                # Signal time is a conservative end bound; waiting can outlast PID reuse.
                if not parent_alive and born > ended.get(parent, float("-inf")):
                    continue
                created[proc.pid] = born
                captured[proc.pid] = proc
                pending.append(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        if not pending:
            return


@contextlib.contextmanager
def retained_install(argv: list[str]) -> Iterator[None]:
    """Move locked entrypoints aside; restore the uv environment on install failure."""
    suffix = f".previous-{uuid.uuid4().hex}"
    moved: list[tuple[Path, Path]] = []
    try:
        paths = installed_launchers()
        # uv replaces the tool environment; retaining it also covers a partial
        # removal of the running interpreter (including its loaded DLLs).
        prefix = Path(sys.prefix)
        if (
            len(argv) >= 3
            and Path(argv[0]).stem.casefold() == "uv"
            and argv[1] == "tool"
            and (prefix / "uv-receipt.toml").is_file()
        ):
            if argv[2] == "install":
                paths.append(prefix)
            else:
                backup = prefix.with_name(prefix.name + suffix)
                shutil.copytree(prefix, backup)
                moved.append((prefix, backup))
        for path in dict.fromkeys(paths):
            backup = path.with_name(path.name + suffix)
            path.rename(backup)
            moved.append((path, backup))
        yield
        for path, backup in moved:
            if not path.exists():
                backup.rename(path)
    except BaseException as exc:
        failures: list[str] = []
        for path, backup in reversed(moved):
            try:
                if not backup.exists():
                    continue
                if path.exists():
                    path.rename(path.with_name(path.name + f".failed-{uuid.uuid4().hex}"))
                backup.rename(path)
            except OSError as restore_error:
                failures.append(f"{backup} -> {path}: {restore_error}")
        if failures:
            raise OSError(
                "Could not restore retained install; backups kept: " + "; ".join(failures)
            ) from exc
        raise
    else:
        # A live Windows executable/DLL cannot be unlinked yet. Keep that
        # backup until this CLI exits instead of turning success into failure.
        for _, backup in moved:
            if backup.is_file():
                with contextlib.suppress(OSError):
                    backup.unlink()
            elif backup.is_dir():
                print(
                    f"Previous tool environment retained until this CLI exits: {backup}",
                    file=sys.stderr,
                )
