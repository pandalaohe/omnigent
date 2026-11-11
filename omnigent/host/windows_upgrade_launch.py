"""Small stable shim for launching the target version's Windows upgrade helper."""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import click
import psutil

from omnigent.host import windows_upgrade
from omnigent.host.windows_upgrade_helper import ORIGINAL_PATH, clean_environment, release_lock
from omnigent.process_logging import data_dir
from omnigent.update_check import (
    _InstalledWheelInfo,
    _split_vcs_url,
    _uv_python_pin,
    _web_ui_bundled,
)
from omnigent.util.atomic_json import atomic_write_json


def in_job() -> bool:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    kernel.IsProcessInJob.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_int),
    ]
    result = ctypes.c_int()
    if not kernel.IsProcessInJob(kernel.GetCurrentProcess(), None, ctypes.byref(result)):
        raise ctypes.WinError()  # type: ignore[attr-defined]
    return bool(result.value)


def acquire_lock(path: Path) -> None:
    for _ in range(2):
        try:
            with path.open("x", encoding="utf-8") as stream:
                json.dump(
                    {"pid": os.getpid(), "create_time": psutil.Process().create_time()}, stream
                )
            return
        except FileExistsError:
            try:
                owner = json.loads(path.read_text(encoding="utf-8"))
                if psutil.Process(owner["pid"]).create_time() == owner["create_time"]:
                    raise RuntimeError("Another Windows upgrade is already running.")
            except psutil.NoSuchProcess:
                pass
            except (ValueError, KeyError) as exc:
                raise RuntimeError(f"Unreadable upgrade lock: {path}") from exc
            path.unlink()
    raise RuntimeError("Could not acquire the Windows upgrade lock.")


def launch(
    info: _InstalledWheelInfo, target: str, *, extras: tuple[str, ...], force: bool, dry_run: bool
) -> None:
    from omnigent import cli
    from omnigent.host.service import installed_user_host_service

    if (
        os.environ.get("OMNIGENT_RUNNER_ID")
        or os.environ.get("OMNIGENT_SESSION_ID")
        or os.environ.get("OMNIGENT") == "1"
    ):
        raise RuntimeError("Run `omni up` from a PowerShell window outside Omnigent.")
    uv = windows_upgrade.resolve_uv_executable()
    prefix = Path(sys.prefix)
    launchers = windows_upgrade.installed_launchers()
    if not launchers:
        raise RuntimeError("Cannot identify this tool's uv launchers; install unchanged.")
    bin_dir = launchers[0].parent
    repo, _ = _split_vcs_url(info.vcs_url or "")
    source = f"git+{repo}@{target}"
    requested = sorted(set(info.extras) | set(extras))
    spec = source + "#egg=omnigent" + (f"[{','.join(requested)}]" if requested else "")
    snapshot = installed_user_host_service()
    server = snapshot[1] if snapshot else cli._load_global_config().get("server")
    mode = ["--server", str(server)] if server else ["--local"]
    updates = data_dir() / "updates"
    updates.mkdir(parents=True, exist_ok=True)
    log = updates / f"upgrade-{uuid.uuid4().hex}.log"
    parents = [os.getpid()]
    for proc in psutil.Process().parents():
        if Path(proc.exe()).name.casefold() not in (
            "python.exe",
            "pythonw.exe",
            "omni.exe",
            "omnigent.exe",
        ):
            break
        parents.append(proc.pid)
    argv = [
        uv,
        "run",
        "--no-project",
        "--python",
        _uv_python_pin(),
        "--with",
        source,
        "python",
        "-P",
        "-m",
        "omnigent.host.windows_upgrade_helper",
        "--install-spec",
        spec,
        "--target",
        target,
        "--previous",
        info.commit_sha or "",
        "--tool-dir",
        str(prefix),
        "--bin-dir",
        str(bin_dir),
        *mode,
        "--python",
        _uv_python_pin(),
        "--log",
        str(log),
        "--web-ui",
        "try" if _web_ui_bundled() else "skip",
    ]
    for pid in parents:
        argv.extend(["--wait-pid", str(pid)])
    if force:
        argv.append("--force")
    if dry_run:
        argv.append("--dry-run")
    processes = windows_upgrade.capture_host_tree(
        prefix=prefix, bin_dir=bin_dir, include_install_users=True
    )
    click.echo(f"Target: {target}; would stop PIDs: {[p.pid for p in processes]}")
    if dry_run:
        click.echo(subprocess.list2cmdline(argv))
        click.echo(
            "Environment: restore caller PATH; remove Python/venv overrides; "
            "skip helper web UI build."
        )
    flags = 0x00000200 | 0x08000000  # NEW_PROCESS_GROUP | NO_WINDOW
    breakaway = 0x01000000
    job = in_job()
    lock = updates / "upgrade.lock"
    acquire_lock(lock)
    env = clean_environment()
    env[ORIGINAL_PATH] = env.get("PATH", "")
    env["OMNIGENT_SKIP_WEB_UI"] = "true"
    try:
        # Validate job breakaway before the local-server drain can stop anything.
        if not dry_run and not force:
            if job:
                with subprocess.Popen(
                    [sys.executable, "-I", "-c", "pass"],
                    creationflags=flags | breakaway,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                ) as probe:
                    if probe.wait(timeout=15):
                        raise RuntimeError("Could not verify upgrade job breakaway.")
            cli._drain_and_stop_local_server(force=False)
        with log.open("ab") as stream:
            try:
                child = subprocess.Popen(
                    argv,
                    cwd=updates,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=stream,
                    stderr=stream,
                    creationflags=flags | breakaway,
                )
            except OSError:
                if job:
                    raise RuntimeError(
                        "This Windows job forbids upgrade breakaway; use an external PowerShell."
                    ) from None
                child = subprocess.Popen(
                    argv,
                    cwd=updates,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=stream,
                    stderr=stream,
                    creationflags=flags,
                )
        # uv may spend minutes building before importing the helper. Keep its
        # live identity in the lock during that interval, then the helper takes it.
        owner = json.loads(lock.read_text(encoding="utf-8"))
        if owner["pid"] == os.getpid():
            atomic_write_json(
                lock, {"pid": child.pid, "create_time": psutil.Process(child.pid).create_time()}
            )
    except BaseException:
        release_lock(lock, parents)
        raise
    viewer = shutil.which("powershell.exe")
    if viewer:
        escaped = str(log).replace("'", "''")
        with contextlib.suppress(OSError):
            subprocess.Popen(
                [viewer, "-NoProfile", "-Command", f"Get-Content -LiteralPath '{escaped}' -Wait"],
                creationflags=0x00000010,
                stdin=subprocess.DEVNULL,
            )
    click.echo(
        f"Upgrade log: {log}\nThe Host restarts in about a minute; "
        "`omni host status` shows the result."
    )
