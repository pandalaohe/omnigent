"""Windows process shutdown and rename-aside for synchronous CLI upgrades."""

from __future__ import annotations

import contextlib
import filecmp
import gc
import importlib
import os
import re
import shutil
import sys
import tomllib
import uuid
from collections.abc import Iterator
from pathlib import Path
from time import sleep, time

import psutil

_TRASH_SUFFIX = re.compile(r"\.trash-[0-9a-f]{32}")


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


def _inside_install(path: str) -> bool:
    prefix = str(Path(sys.prefix)).casefold().rstrip("\\/")
    return path.casefold().startswith(prefix + os.sep)


def capture_host_tree(*, include_install_users: bool = False) -> list[psutil.Process]:
    """Capture Host trees and, for a forced upgrade, detached install users."""
    autostart = Path.home() / ".omnigent" / "autostart"
    script = str(autostart / "Start-OmnigentHostDefault.ps1").casefold()
    pwsh = shutil.which("pwsh")
    executables = {str(path).casefold() for path in installed_launchers()}
    interpreters = {
        os.path.realpath(sys.executable).casefold(),
        os.path.realpath(getattr(sys, "_base_executable", sys.executable)).casefold(),
    }
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
            if proc.pid in protected:
                runtime = any(
                    lowered[index : index + 2]
                    in (
                        ["-m", "omnigent.host._daemon_entry"],
                        ["-m", "omnigent.host.service_entry"],
                        ["-m", "omnigent.runner._entry"],
                    )
                    for index in range(1, len(lowered) - 1)
                )
                if loop or host or runtime:
                    raise RuntimeError(
                        "Run the upgrade from a terminal outside the Host process tree."
                    )
                continue
            install_user = False
            if include_install_users:
                install_user = (
                    exe in executables
                    or _inside_install(exe)
                    or (
                        exe in interpreters
                        and (
                            bool(args and _inside_install(args[0]))
                            or bool(len(args) > 1 and lowered[1] in executables)
                        )
                    )
                )
                # uv's Python trampoline can run the base interpreter outside
                # the venv. Loaded extension paths identify orphaned workers.
                if not install_user:
                    with contextlib.suppress(psutil.AccessDenied):
                        memory_maps = getattr(proc, "memory_maps", None)
                        install_user = (
                            any(_inside_install(mapping.path) for mapping in memory_maps())
                            if memory_maps is not None
                            else False
                        )
            if not (loop or host or install_user):
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
        # Exited intermediaries and clock rollback with an absent parent remain risks.
        # Locks from survivors fall back to rename-aside/restore; this is not containment.
        for proc in current.values():
            try:
                parent = proc.ppid()
                if proc.pid in captured or parent not in created:
                    continue
                born = proc.create_time()
                if born < created[parent]:
                    continue
                parent_alive = parent in current and current[parent].is_running()
                if parent_alive:
                    if current[parent].create_time() != created[parent]:
                        continue
                # Signal time bounds enrollment only when the recorded parent is gone.
                elif born > ended.get(parent, float("-inf")):
                    continue
                created[proc.pid] = born
                captured[proc.pid] = proc
                pending.append(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        if not pending:
            return


def file_users(path: Path) -> str:
    """Describe registered file users without shutting down unrelated applications."""
    import ctypes
    from ctypes import wintypes

    class UniqueProcess(ctypes.Structure):
        _fields_ = [("pid", wintypes.DWORD), ("started", wintypes.FILETIME)]

    class ProcessInfo(ctypes.Structure):
        _fields_ = [
            ("process", UniqueProcess),
            ("name", wintypes.WCHAR * 256),
            ("service", wintypes.WCHAR * 64),
            ("kind", wintypes.UINT),
            ("status", wintypes.ULONG),
            ("session", wintypes.DWORD),
            ("restartable", wintypes.BOOL),
        ]

    rm = ctypes.WinDLL("Rstrtmgr")  # type: ignore[attr-defined]
    rm.RmStartSession.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, wintypes.LPWSTR]
    rm.RmRegisterResources.argtypes = [
        wintypes.DWORD,
        wintypes.UINT,
        ctypes.POINTER(wintypes.LPCWSTR),
        wintypes.UINT,
        ctypes.POINTER(UniqueProcess),
        wintypes.UINT,
        ctypes.POINTER(wintypes.LPCWSTR),
    ]
    rm.RmGetList.argtypes = [
        wintypes.DWORD,
        ctypes.POINTER(wintypes.UINT),
        ctypes.POINTER(wintypes.UINT),
        ctypes.POINTER(ProcessInfo),
        ctypes.POINTER(wintypes.DWORD),
    ]
    rm.RmEndSession.argtypes = [wintypes.DWORD]
    for function in (rm.RmStartSession, rm.RmRegisterResources, rm.RmGetList, rm.RmEndSession):
        function.restype = wintypes.DWORD
    session = wintypes.DWORD()
    key = ctypes.create_unicode_buffer(32 + 1)  # CCH_RM_SESSION_KEY plus terminator.
    code = rm.RmStartSession(ctypes.byref(session), 0, key)
    if code:
        raise OSError(code, "Restart Manager could not start")
    try:
        paths = (
            [str(file) for file in path.rglob("*") if file.is_file()]
            if path.is_dir()
            else [str(path)]
        )
        names = (wintypes.LPCWSTR * len(paths))(*paths)
        code = rm.RmRegisterResources(session, len(paths), names, 0, None, 0, None)
        if code:
            raise OSError(code, "Restart Manager could not register the files")
        needed, count, reasons = wintypes.UINT(), wintypes.UINT(), wintypes.DWORD()
        entries = None
        for _ in range(3):
            code = rm.RmGetList(
                session, ctypes.byref(needed), ctypes.byref(count), entries, ctypes.byref(reasons)
            )
            if code == 0:
                return (
                    ", ".join(
                        f"PID {entry.process.pid} ({entry.name})"
                        for entry in (entries or [])[: count.value]
                    )
                    or "no file users reported (the holder may already have exited)"
                )
            if code != 234:  # ERROR_MORE_DATA
                raise OSError(code, "Restart Manager could not list file users")
            count.value = needed.value
            entries = (ProcessInfo * count.value)()
        raise OSError("Restart Manager file users changed during enumeration")
    finally:
        rm.RmEndSession(session)


def _visible_file_users(path: Path) -> str:
    """Offer process-table evidence when Restart Manager cannot query file users."""
    launchers: set[str] = set()
    with contextlib.suppress(OSError, ValueError):
        launchers.update(str(launcher).casefold() for launcher in installed_launchers())
    target = str(path).casefold()
    directory = path.is_dir()
    users: list[str] = []
    for proc in _fresh_processes():
        try:
            image = proc.exe()
            related = image.casefold() in launchers or _inside_install(image)
            with contextlib.suppress(psutil.Error):
                related |= any(
                    entry.path.casefold() == target or (directory and _inside_install(entry.path))
                    for entry in proc.open_files()
                )
            if related and proc.is_running():
                users.append(f"PID {proc.pid} ({image})")
        except psutil.Error:
            continue
    return "; ".join(users) or (
        f"no visible candidates; updater PID {os.getpid()} ({sys.executable}, protected). "
        "The lock owner could not be identified with the available process permissions"
    )


def _rename_with_retry(path: Path, destination: Path) -> None:
    for attempt in range(6):
        try:
            path.rename(destination)
            return
        except OSError as exc:
            if getattr(exc, "winerror", None) not in (32, 33):
                raise
            if attempt < 5:
                sleep(0.2 * (attempt + 1))
                continue
            try:
                users = file_users(path)
            except OSError as diagnostic_error:
                users = (
                    f"Restart Manager unavailable: {diagnostic_error}; "
                    "visible process candidates (not confirmed lock holders): "
                    f"{_visible_file_users(path)}"
                )
            raise OSError(f"{exc}; applications using {path}: {users}") from exc


def _release_launcher_zip() -> None:
    """Close the launcher zip that importlib.metadata keeps open.

    uv's launcher runs ``python <omni.exe>``, so the exe (a zip) is ``sys.path[0]``; a metadata
    lookup caches an open ``ZipFile`` on it, and Windows refuses to rename an open file.
    """
    importlib.invalidate_caches()
    gc.collect()


def _remove_trash(paths: list[Path]) -> None:
    """Delete leftovers that an earlier verified upgrade marked as trash; skip what is in use."""
    for path in paths:
        try:
            siblings = list(path.parent.iterdir())
        except OSError:
            continue
        for sibling in siblings:
            name = sibling.name
            if (
                not name.startswith(path.name)
                or not _TRASH_SUFFIX.fullmatch(name[len(path.name) :])
                or sibling.is_symlink()
            ):
                continue
            with contextlib.suppress(OSError):
                if sibling.is_dir():
                    shutil.rmtree(sibling)
                else:
                    sibling.unlink()


@contextlib.contextmanager
def retained_install(argv: list[str]) -> Iterator[None]:
    """Move locked entrypoints aside; restore the uv environment on install failure."""
    token = uuid.uuid4().hex
    suffix = f".previous-{token}"
    moved: list[tuple[Path, Path]] = []
    try:
        paths = installed_launchers()
        # uv replaces the tool environment; retaining it also covers a partial
        # removal of the running interpreter (including its loaded DLLs).
        prefix = Path(sys.prefix)
        _remove_trash([*paths, prefix])
        _release_launcher_zip()
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
            _rename_with_retry(path, backup)
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
        # A live Windows executable/DLL cannot be deleted yet; mark what is left
        # as trash so the next upgrade removes it, instead of failing the upgrade.
        for path, backup in moved:
            if not backup.exists():
                continue
            if backup.is_file():
                try:
                    backup.unlink()
                    continue
                except OSError:
                    pass
            trash = path.with_name(f"{path.name}.trash-{token}")
            try:
                backup.rename(trash)
            except OSError:
                trash = backup
            if trash.is_dir():
                print(
                    f"Previous tool environment kept until the next upgrade: {trash}",
                    file=sys.stderr,
                )
