"""Windows process shutdown and rename-aside for synchronous CLI upgrades."""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import psutil


def capture_host_tree() -> list[psutil.Process]:
    """Capture the installed Host and its autostart loop before disabling it."""
    autostart = Path.home() / ".omnigent" / "autostart"
    scripts = {
        str(autostart / name).casefold()
        for name in ("Start-OmnigentHostDefault.ps1", "Launch-OmnigentHost.pyw")
    }
    executables = {
        str(Path(path)).casefold() for name in ("omni", "omnigent") if (path := shutil.which(name))
    }
    protected = {os.getpid(), *(proc.pid for proc in psutil.Process().parents())}
    captured: dict[int, psutil.Process] = {}
    for proc in psutil.process_iter():
        try:
            args = proc.cmdline()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        try:
            loop = any(arg.casefold() in scripts for arg in args)
            host = len(args) > 1 and args[0].casefold() in executables and args[1] == "host"
            if not (loop or host):
                continue
            tree = [proc, *proc.children(recursive=True)]
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
    _, alive = psutil.wait_procs(processes, timeout=2)
    for proc in alive:
        with contextlib.suppress(psutil.NoSuchProcess):
            proc.terminate()
    _, alive = psutil.wait_procs(alive, timeout=5)
    for proc in alive:
        with contextlib.suppress(psutil.NoSuchProcess):
            proc.kill()
    _, alive = psutil.wait_procs(alive, timeout=5)
    if alive:
        raise RuntimeError(f"Host processes did not exit: {[proc.pid for proc in alive]}")


@contextlib.contextmanager
def retained_install(argv: list[str]) -> Iterator[None]:
    """Move locked entrypoints aside; restore the uv environment on install failure."""
    suffix = f".previous-{uuid.uuid4().hex}"
    moved: list[tuple[Path, Path]] = []
    try:
        paths = [Path(path) for name in ("omni", "omnigent") if (path := shutil.which(name))]
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
    except BaseException:
        for path, backup in reversed(moved):
            if path.exists():
                path.rename(path.with_name(path.name + f".failed-{uuid.uuid4().hex}"))
            backup.rename(path)
        raise
    finally:
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
