"""Verify shutdown and recover children after their supervisor disappears."""

from __future__ import annotations

import contextlib
import json
import os
import socket
import time
from pathlib import Path
from urllib.parse import urlsplit

import psutil


def owned_processes(output: Path) -> list[psutil.Process]:
    """Identify this attempt by open files, retaining PID birth-time protection."""
    owned = set()
    logs = {str(output / f"{name}.log") for name in ("model", "server", "runner")}
    for candidate in psutil.process_iter():
        try:
            process = psutil.Process(candidate.pid)
            if process.pid == psutil.Process().pid:
                continue
            if any(file.path in logs and file.fd in (1, 2) for file in process.open_files()):
                owned.add(process)
                owned.update(process.children(recursive=True))
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            continue
    manifest = output / "processes.json"
    if manifest.exists():
        for identity in json.loads(manifest.read_text()):
            try:
                process = psutil.Process(identity["pid"])
                if process.create_time() == identity["created"]:
                    owned.add(process)
                    owned.update(process.children(recursive=True))
            except psutil.NoSuchProcess:
                continue
            except psutil.AccessDenied as exc:
                raise RuntimeError(f"Cannot inspect attempt process; inspect {output}") from exc
    live = []
    for process in owned:
        with contextlib.suppress(psutil.NoSuchProcess):
            if process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                live.append(process)
    return live


def stop(output: Path, timeout: float = 30) -> None:
    """Request graceful shutdown, then recover only proven attempt processes."""
    from .runtime import write_json

    previous = json.loads((output / "environment.json").read_text())
    manifest = output / "processes.json"
    identities = json.loads(manifest.read_text()) if manifest.exists() else []
    supervised = any(p.get("name") == "supervisor" for p in identities)
    parents_live = bool(identities)
    for identity in identities:
        try:
            process = psutil.Process(identity["pid"])
            parents_live &= (
                process.create_time() == identity["created"]
                and process.is_running()
                and process.status() != psutil.STATUS_ZOMBIE
            )
        except psutil.NoSuchProcess:
            parents_live = False
    # Capture the live tree before its parents react to the stop request.
    seen = set(owned_processes(output))
    (output / "stop").touch()
    deadline = time.monotonic() + timeout
    while seen and time.monotonic() < deadline:
        _, alive = psutil.wait_procs(list(seen), timeout=0.2)
        seen = set(alive) | set(owned_processes(output))
    for process in seen:
        with contextlib.suppress(psutil.NoSuchProcess):
            process.terminate()
    _, alive = psutil.wait_procs(list(seen), timeout=5)
    for process in alive:
        with contextlib.suppress(psutil.NoSuchProcess):
            process.kill()
    _, alive = psutil.wait_procs(alive, timeout=5)
    remaining = set(alive) | set(owned_processes(output))
    if remaining:
        raise RuntimeError(
            f"Environment processes still running: {sorted(p.pid for p in remaining)}; "
            f"inspect {output} and retry stop"
        )
    state_file = output / "environment.json"
    state = json.loads(state_file.read_text())
    # Old environments may have lost their log handles. Never claim success while
    # one of their recorded listeners still accepts connections.
    for key in ("base_url", "mock_url"):
        if url := state.get(key):
            address = urlsplit(url)
            try:
                with socket.create_connection((address.hostname, address.port), timeout=1):
                    raise RuntimeError(f"{key} still reachable; inspect {output} and retry stop")
            except ConnectionRefusedError:
                pass
            except OSError as exc:
                raise RuntimeError(
                    f"Cannot verify {key} shutdown; inspect {output}: {exc}"
                ) from exc
    if not manifest.exists():
        raise RuntimeError(
            f"Process identities unavailable; cannot verify cleanup; inspect {output}"
        )
    groups = {p["group"] for p in json.loads(manifest.read_text()) if "group" in p}
    for process in psutil.process_iter():
        try:
            if os.getpgid(process.pid) in groups and process.status() != psutil.STATUS_ZOMBIE:
                raise RuntimeError(
                    f"Unverified descendant {process.pid} remains; inspect {output} and retry stop"
                )
        except (ProcessLookupError, psutil.NoSuchProcess):
            continue
        except (PermissionError, psutil.AccessDenied) as exc:
            raise RuntimeError(f"Cannot verify process groups; inspect {output}") from exc
    completed = (
        supervised
        and parents_live
        and state.get("cleanup_complete")
        and state["status"] == "stopped"
    )
    if not completed and not previous.get("shutdown_verified"):
        raise RuntimeError(
            f"Supervised cleanup not proven; known services stopped but detached "
            f"descendant cleanup is unverified; inspect {output}"
        )
    state["status"] = "stopped"
    state["shutdown_verified"] = True
    write_json(state_file, state)
