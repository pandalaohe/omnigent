"""Target-version Windows upgrader, run outside the tool environment it replaces."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import psutil

from omnigent.host import windows_task, windows_upgrade
from omnigent.host.daemon_lifecycle import HostDaemonRecord, pid_is_recorded_daemon
from omnigent.host.windows_upgrade_lock import Owner, acquire_lock, release_lock
from omnigent.process_logging import data_dir
from omnigent.update_check import _probe_installed_distribution, _split_vcs_url
from omnigent.util.atomic_json import atomic_write_json

HELPER_VERSION = 1
ORIGINAL_PATH = "OMNIGENT_UPGRADE_ORIGINAL_PATH"


def last_upgrade_line() -> str | None:
    try:
        record = json.loads(
            (data_dir() / "updates" / "last-upgrade.json").read_text(encoding="utf-8")
        )
        result = record.get("result") or f"did not finish at {record.get('step', 'unknown')}"
        return (
            f"last upgrade: {record.get('from', '')}→{record.get('to', '')} {result} "
            f"[at {record.get('step', '')}] ({record.get('at', '')}, log {record.get('log', '')})"
        )
    except (OSError, ValueError, AttributeError):
        return None


def clean_environment() -> dict[str, str]:
    """Restore the caller's PATH before registering a persistent service."""
    env = dict(os.environ)
    if ORIGINAL_PATH in env:
        env["PATH"] = env.pop(ORIGINAL_PATH)
    for name in (
        "VIRTUAL_ENV",
        "__PYVENV_LAUNCHER__",
        "PYTHONHOME",
        "PYTHONPATH",
        "UV_RUN_RECURSION_DEPTH",
        "OMNIGENT_SKIP_WEB_UI",
    ):
        env.pop(name, None)
    return env


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    for name in ("install-spec", "target", "tool-dir", "bin-dir", "python", "log"):
        result.add_argument(f"--{name}", required=True)
    result.add_argument("--previous", default="")
    mode = result.add_mutually_exclusive_group(required=True)
    mode.add_argument("--server")
    mode.add_argument("--local", action="store_true")
    result.add_argument("--wait-pid", action="append", type=int, default=[])
    result.add_argument("--force", action="store_true")
    result.add_argument("--dry-run", action="store_true")
    result.add_argument("--web-ui", choices=("skip", "try"), default="skip")
    return result


def wait_for_parents(pids: list[int]) -> None:
    processes = []
    for pid in pids:
        with contextlib.suppress(psutil.NoSuchProcess):
            processes.append(psutil.Process(pid))
    _, alive = psutil.wait_procs(processes, timeout=60)
    if alive:
        raise RuntimeError(f"Upgrade callers did not exit: {[p.pid for p in alive]}")


def _run(argv: list[str], env: dict[str, str]) -> None:
    result = subprocess.run(argv, env=env, check=False)
    if result.returncode:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {subprocess.list2cmdline(argv)}"
        )


def enable_host(args: argparse.Namespace, env: dict[str, str]) -> None:
    mode = ["--server", args.server] if args.server else ["--local"]
    _run(
        [str(Path(args.bin_dir) / "omnigent.exe"), "host", "enable", "--non-interactive", *mode],
        env,
    )


def require_host_alive(record: HostDaemonRecord) -> None:
    if not pid_is_recorded_daemon(record):
        raise RuntimeError("The registered Host exited during upgrade verification.")


def verify_host(args: argparse.Namespace) -> tuple[str, HostDaemonRecord]:
    from omnigent import cli
    from omnigent.host.daemon_lifecycle import pid_is_recorded_daemon, read_daemon_records

    snapshot = windows_task.installed(windows_task.description())
    if snapshot is None or snapshot[1] != args.server:
        raise RuntimeError("The standard Host task was not registered to the requested target.")
    target = cli._normalize_daemon_target(args.server) if args.server else None
    deadline = time.monotonic() + 60
    record = None
    while time.monotonic() < deadline:
        record = next(
            (
                r
                for r in read_daemon_records()
                if (
                    (
                        r.mode == "local"
                        if target is None
                        else cli._normalize_daemon_target(r.target) == target
                    )
                    and pid_is_recorded_daemon(r)
                )
            ),
            None,
        )
        if record is not None:
            break
        time.sleep(1)
    if record is None:
        raise RuntimeError("The registered Host did not start within 60 seconds.")
    if not args.server:
        require_host_alive(record)
        return "online", record
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        require_host_alive(record)
        payload = cli._daemon_status_payload(record, include_sessions=False)
        if payload.get("host_status") == "online":
            require_host_alive(record)
            return "online", record
        time.sleep(2)
    require_host_alive(record)
    print("Host started; server connection is not online yet.", flush=True)
    return "not-online-yet", record


def holders(prefix: Path) -> str:
    found = []
    for proc in windows_upgrade._fresh_processes():
        try:
            paths = [p.path for p in proc.open_files()]
            memory_maps = getattr(proc, "memory_maps", None)
            if memory_maps is not None:
                paths.extend(p.path for p in memory_maps())
            if any(windows_upgrade._inside_install(p, prefix) for p in paths):
                found.append(f"PID {proc.pid} ({proc.name()})")
        except psutil.Error:
            continue
    return ", ".join(found) or "no holders visible with current permissions"


def run(args: argparse.Namespace) -> int:
    updates = data_dir() / "updates"
    updates.mkdir(parents=True, exist_ok=True)
    record: dict[str, object] = {
        "from": args.previous,
        "to": args.target,
        "log": args.log,
        "helper_version": HELPER_VERSION,
        "at": time.time(),
    }

    def step(name: str, result: str | None = None) -> None:
        record["step"] = name
        if result:
            record["result"] = result
        atomic_write_json(updates / "last-upgrade.json", record)
        print(f"Upgrade: {name}" + (f" ({result})" if result else ""), flush=True)

    prefix, bin_dir = Path(args.tool_dir).resolve(), Path(args.bin_dir).resolve()
    env = clean_environment()
    env["UV_TOOL_DIR"] = str(prefix.parent)
    env["UV_TOOL_BIN_DIR"] = str(bin_dir)
    backup = prefix.with_name(prefix.name + f".previous-{uuid.uuid4().hex}")
    launchers: list[tuple[Path, Path]] = []
    stopped = False
    saved = False
    owner: Owner | None = None
    try:
        step("wait")
        wait_for_parents(args.wait_pid)
        lock = updates / "upgrade.lock"
        owner = acquire_lock(lock, handoff_from={proc.pid for proc in psutil.Process().parents()})
        step("resolve")
        if (
            not prefix.is_dir()
            or not bin_dir.is_dir()
            or not (prefix / "uv-receipt.toml").is_file()
            or prefix == Path(sys.prefix).resolve()
        ):
            raise RuntimeError(
                "Expected an existing uv tool install outside the helper environment."
            )
        processes = windows_upgrade.capture_host_tree(
            prefix=prefix, bin_dir=bin_dir, include_install_users=True
        )
        if args.dry_run:
            print(f"Tool: {prefix}; bin: {bin_dir}; stop: {[p.pid for p in processes]}")
            step("dry-run", "dry-run-ok")
            return 0
        if not args.force:
            step("drain")
            from omnigent import cli
            from omnigent.host.daemon_lifecycle import read_daemon_records

            for daemon in read_daemon_records():
                if args.server:
                    if cli._normalize_daemon_target(daemon.target) != cli._normalize_daemon_target(
                        args.server
                    ):
                        continue
                elif daemon.mode != "local":
                    continue
                cli._wait_for_running_sessions(
                    lambda daemon=daemon: cli._count_running_daemon_sessions(daemon),
                    force_hint="use --force",
                )
        step("stop")
        stopped = True
        windows_task.stop(windows_task.description())
        windows_upgrade.stop_host_tree(processes)
        step("holders")
        try:
            prefix.rename(backup)
        except OSError as exc:
            raise RuntimeError(f"Cannot retain {prefix}: {holders(prefix)}") from exc
        saved = True
        for name in ("omni.exe", "omnigent.exe"):
            path = bin_dir / name
            if path.is_file():
                retained = path.with_name(path.name + backup.name[len(prefix.name) :])
                shutil.copy2(path, retained)
                launchers.append((path, retained))
        step("install")
        uv = windows_upgrade.resolve_uv_executable()
        command = [
            uv,
            "tool",
            "install",
            "--reinstall",
            "--python",
            args.python,
            args.install_spec,
        ]
        install_env = dict(env)
        if args.web_ui == "skip":
            install_env["OMNIGENT_SKIP_WEB_UI"] = "true"
        try:
            _run(command, install_env)
        except RuntimeError:
            if args.web_ui != "try":
                raise
            _run(command, {**env, "OMNIGENT_SKIP_WEB_UI": "true"})
        step("verify")
        _, sha = _probe_installed_distribution(str(prefix / "Scripts" / "python.exe"), env=env)
        if sha != args.target:
            raise RuntimeError(f"Commit mismatch: expected {args.target}, installed {sha}")
        _run([str(bin_dir / "omni.exe"), "--version"], env)
        step("register")
        enable_host(args, env)
        record["host"], host_record = verify_host(args)
        step("channel")
        receipt = updates / "custom-host-rollback.json"
        if receipt.exists():
            channel = json.loads(receipt.read_text(encoding="utf-8"))
            channel["channel_tracking_migrated"] = True
            atomic_write_json(receipt, channel)
        step("commit")
        require_host_alive(host_record)
        step("cleanup")
        with contextlib.suppress(OSError):
            shutil.rmtree(backup)
        for _, retained in launchers:
            with contextlib.suppress(OSError):
                retained.unlink()
        step("complete", "ok")
        return 0
    except Exception as exc:  # noqa: BLE001 — every transaction failure must enter recovery
        failed_step = str(record.get("step", "lock"))
        record["failed_step"] = failed_step
        record["error"] = str(exc)
        record["snapshot"] = str(backup)
        outcomes: dict[str, str] = {}
        record["recovery"] = outcomes
        print(f"Upgrade failed at {failed_step}: {exc}", flush=True)

        def attempt(name: str, action: Callable[[], object]) -> bool:
            step(name)
            try:
                action()
            except Exception as recovery:  # noqa: BLE001 — one failure cannot skip other recovery
                outcomes[name] = f"failed: {recovery}"
                print(f"{name}: {recovery}", flush=True)
                step(name)
                return False
            outcomes[name] = "ok"
            step(name)
            return True

        repo, _ = _split_vcs_url(args.install_spec)
        fragment = args.install_spec.partition("#")[2]
        spec = f"git+{repo}@{args.previous}" + (f"#{fragment}" if fragment else "")
        uv = "uv"
        with contextlib.suppress(OSError):
            uv = windows_upgrade.resolve_uv_executable()
        command = [
            uv,
            "tool",
            "install",
            "--reinstall",
            "--python",
            args.python,
            spec if args.previous else args.install_spec,
        ]
        # PowerShell needs the invocation operator for a quoted executable path.
        record["recovery_command"] = "& " + " ".join(
            "'" + argument.replace("'", "''") + "'" for argument in command
        )
        if saved and backup.exists():
            attempt("recovery-stop-task", lambda: windows_task.stop(windows_task.description()))
            captured: list[psutil.Process] = []
            attempt(
                "recovery-capture",
                lambda: captured.extend(
                    windows_upgrade.capture_host_tree(
                        prefix=prefix, bin_dir=bin_dir, include_install_users=True
                    )
                ),
            )
            attempt("recovery-stop-tree", lambda: windows_upgrade.stop_host_tree(captured))
            if prefix.exists():
                attempt("recovery-remove-partial", lambda: shutil.rmtree(prefix))
            restored = attempt("recovery-restore-tool", lambda: backup.rename(prefix))
            for path, retained in launchers:
                attempt(
                    f"recovery-restore-{path.name}",
                    lambda path=path, retained=retained: shutil.copy2(retained, path),
                )
            if not restored and args.previous:
                attempt(
                    "recovery-reinstall",
                    lambda: _run(command, {**env, "OMNIGENT_SKIP_WEB_UI": "true"}),
                )
        if stopped:
            attempt("recovery-restart", lambda: enable_host(args, env))
        record["recovery_result"] = (
            "incomplete" if any(value != "ok" for value in outcomes.values()) else "ok"
        )
        print("Recovery: " + str(record["recovery_command"]), flush=True)
        step("recovery-complete", "failed")
        return 1
    finally:
        if owner is not None:
            release_lock(updates / "upgrade.lock", owner)


def main() -> int:
    args, _ = parser().parse_known_args()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
