"""Native uv transactions against tiny local wheels, with isolated tool directories."""

from __future__ import annotations

import ast
import contextlib
import ctypes
import dataclasses
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from collections.abc import Sequence
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def identity_and_wrapper_checks(root: Path, helper: Path) -> dict:
    """Execute actual Python selectors and wrapper generation without importing the CLI."""
    source = Path(__file__).parents[2] / "omnigent" / "cli.py"
    names = {
        "_WindowsProcessInfo",
        "_normalize_windows_fs_path",
        "_windows_path_holds_tool_venv",
        "_windows_is_host_launcher",
        "_windows_process_arguments",
        "_windows_is_host_daemon",
        "_windows_stopped_host_tree_pids",
        "_terminate_windows_process",
        "_list_windows_processes",
        "_arm_windows_custom_host_recovery",
        "_validate_windows_custom_host_recovery_paths",
        "_wait_for_windows_custom_host_venv_release",
        "_windows_venv_locking_processes",
        "_reconcile_custom_host_update_result",
        "_ensure_no_running_custom_host_helper",
        "_valid_git_sha",
    }
    tree = ast.parse(source.read_text())
    nodes = [node for node in tree.body if getattr(node, "name", "") in names]
    namespace = {
        "__name__": __name__,
        "dataclass": dataclasses.dataclass,
        "Path": Path,
        "Sequence": Sequence,
        "re": re,
        "contextlib": contextlib,
        "os": os,
        "shutil": shutil,
        "click": SimpleNamespace(ClickException=RuntimeError),
        "time": time,
        "subprocess": subprocess,
        "_CUSTOM_HOST_VENV_RELEASE_TIMEOUT_S": 15.0,
        "_CUSTOM_HOST_TREE_GRACE_S": 2.0,
        "_CUSTOM_HOST_PARENT_EXIT_TIMEOUT_S": 120.0,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), namespace)
    row = namespace["_WindowsProcessInfo"]
    venv = r"C:\Tools\omnigent"
    cli = r"C:\Profiles\fixture\.local\bin\omni.exe"
    processes = [
        row(
            10,
            1,
            "python.exe",
            r"C:\Python\python.exe",
            "controller.py",
            (venv + r"\Lib\worker.pyd",),
            1.0,
        ),
        row(
            11,
            10,
            "python.exe",
            r"C:\Python\python.exe",
            "python -m omnigent.host._daemon_entry",
            (),
            2.0,
        ),
        row(12, 10, "python.exe", r"C:\Python\python.exe", "other.py", (), 3.0),
        row(13, 11, "python.exe", r"C:\Python\python.exe", "runner.py", (), 4.0),
    ]
    selected = namespace["_windows_stopped_host_tree_pids"](
        processes,
        stopped_host_pids=[11],
        tool_venv_path=venv,
        cli_bin_path=cli,
        excluded_pids=set(),
    )
    assert selected == [11, 13], selected
    startup_path = r"C:\Profiles\fixture\.omnigent\autostart\Start-OmnigentHostDefault.ps1"
    for command in (
        f'pwsh.exe -Command Get-Content "{startup_path}"',
        f'pwsh.exe -File "{startup_path}.bak"',
        f'pwsh.exe -File "{startup_path}".bak',
    ):
        assert not namespace["_windows_is_host_launcher"](
            row(20, 1, "pwsh.exe", r"C:\PowerShell\pwsh.exe", command, (), 5.0), cli
        ), "Incidental script text authorized process termination"
    killed = []
    namespace["psutil"] = SimpleNamespace(
        Error=OSError,
        Process=lambda _pid: SimpleNamespace(
            create_time=lambda: 9.0, kill=lambda: killed.append(True), wait=lambda **_kw: None
        ),
    )
    namespace["_terminate_windows_process"](11, 2.0)
    assert not killed, "Recycled PID was terminated"
    mapped = SimpleNamespace(
        info={
            "pid": 11,
            "ppid": 1,
            "name": "python.exe",
            "exe": r"C:\Python\python.exe",
            "cmdline": ["python", "-m", "worker"],
            "create_time": 5.0,
        },
        memory_maps=lambda: [SimpleNamespace(path=venv + r"\Lib\worker.pyd")],
        open_files=list,
    )
    namespace["psutil"].process_iter = lambda _fields: [mapped]
    namespace["os"] = SimpleNamespace(name="nt")
    observed = namespace["_list_windows_processes"]()
    assert namespace["_windows_path_holds_tool_venv"](
        observed[0], tool_venv_path=venv, cli_bin_path=cli
    )
    namespace["os"] = os
    child = row(
        30,
        29,
        "python.exe",
        r"C:\Python\python.exe",
        "worker.py",
        (venv + r"\Lib\worker.pyd",),
        2.0,
    )
    alive = [child]
    stopped = []
    namespace["_list_windows_processes"] = lambda: alive[:]

    def stop_captured(pid: int, created: float) -> None:
        if alive and pid == child.pid and created == child.created_at:
            stopped.append(pid)
            alive.clear()

    namespace["_terminate_windows_process"] = stop_captured
    namespace["_wait_for_windows_custom_host_venv_release"](
        tool_venv_path=venv,
        cli_bin_path=cli,
        stopped_host_pids=[29],
        process_identities=[child],
        timeout_s=1,
        grace_s=0,
    )
    assert stopped == [30], "Captured child was lost after its root exited"
    wrapper = root / ".local" / "bin" / "omni-host-service.cmd"
    startup = root / ".omnigent" / "autostart" / "Start-OmnigentHostDefault.ps1"
    startup.parent.mkdir(parents=True, exist_ok=True)
    startup.write_text(
        "#requires -Version 7.0\n"
        "param([string]$Message = 'fixture boot started')\nWrite-Output $Message\n"
    )
    instruction = root / ".omnigent" / "updates" / "instruction.json"
    instruction.parent.mkdir(parents=True, exist_ok=True)
    namespace["_custom_host_tool_venv_path"] = lambda: root / "tools" / "omnigent"
    namespace["_custom_host_update_paths"] = lambda: {
        "helper": helper,
        "instruction": instruction,
        "result": root / "reconcile.json",
        "lock": root / "reconcile.lock",
    }
    with patch.object(Path, "home", return_value=root):
        try:
            namespace["_validate_windows_custom_host_recovery_paths"](
                root / "tools" / "omnigent" / "Scripts" / "supervisor.cmd"
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("A recovery wrapper inside the movable venv was accepted")
        namespace["_arm_windows_custom_host_recovery"](wrapper, helper, instruction)
        armed = wrapper.read_bytes()
        namespace["_arm_windows_custom_host_recovery"](wrapper, helper, instruction)
        assert wrapper.read_bytes() == armed, "Wrapper arming was not idempotent"
    assert "-RecoverOnly" in startup.read_text() and "-ResumeAutostart" in startup.read_text()
    parsed = subprocess.run(
        [
            shutil.which("pwsh"),
            "-NoProfile",
            "-Command",
            "$t=$null;$e=$null;[void][Management.Automation.Language.Parser]::ParseFile("
            "$env:PROBE_STARTUP,[ref]$t,[ref]$e);if($e.Count){$e;exit 1}",
        ],
        env={**os.environ, "PROBE_STARTUP": str(startup)},
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert parsed.returncode == 0, parsed.stdout + parsed.stderr
    transaction = root / "tools" / ("omnigent.update-" + "d" * 32)
    transaction.mkdir()
    receipt = {
        "status": "failed",
        "phase": "rolling_back",
        "old_commit": "a" * 40,
        "target_commit": "b" * 40,
        "backup_path": str(transaction / "previous"),
        "error": "Installer failed",
    }
    namespace["_read_custom_host_update_result"] = lambda: receipt
    paths = namespace["_custom_host_update_paths"]()
    paths["lock"].write_text("")
    with patch.dict(
        sys.modules,
        {
            "omnigent.install_ledger": SimpleNamespace(
                atomic_write_json=lambda path, value: path.write_text(json.dumps(value)),
            )
        },
    ):
        reconciled = namespace["_reconcile_custom_host_update_result"]("b" * 40, persist=True)
    for saved in (paths["result"], transaction / "transaction.json"):
        assert json.loads(saved.read_text())["phase"] == "completed"
    namespace["_read_custom_host_update_result"] = lambda: reconciled
    namespace["_ensure_no_running_custom_host_helper"]()
    namespace["_read_custom_host_update_result"] = lambda: receipt
    try:
        namespace["_ensure_no_running_custom_host_helper"]()
    except RuntimeError as error:
        assert "independent recovery" in str(error), str(error)
    else:
        raise AssertionError("A live CLI attempted synchronous recovery")
    print(
        "PASS Python process authority, orphan cleanup, terminal reconciliation, "
        "independent recovery and param startup"
    )
    return namespace


def process_alive(pid: int) -> bool:
    """Check fixture PIDs without importing application dependencies."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x100000, False, pid)
    if not handle:
        return False
    try:
        return kernel.WaitForSingleObject(handle, 0) == 258
    finally:
        kernel.CloseHandle(handle)


def run(helper: Path, fixtures: Path, uv: str, pwsh: str) -> None:
    """Exercise uv receipts, launchers and rollback without the installed application."""
    for mode in (
        "success",
        "import-fails",
        "version-mismatch",
        "reconnect-fails",
        "child-survives-stop",
        "stop-fails",
        "status-hangs",
        "helper-killed-missing",
        "helper-killed-child",
        "helper-killed-boot",
        "helper-killed-rollback",
        "reconciled-healthy",
    ):
        root = fixtures / mode
        root.mkdir(parents=True)
        tools = root / "tools"
        binary = root / ".local" / "bin"
        venv = tools / "omnigent"
        env = {
            key: value
            for key, value in os.environ.items()
            if key in {"SYSTEMROOT", "SystemRoot", "WINDIR", "PATH", "COMSPEC", "PATHEXT"}
        }
        env.update(
            UV_TOOL_DIR=str(tools),
            UV_TOOL_BIN_DIR=str(binary),
            UV_CACHE_DIR=str(root / "cache"),
            UV_NO_PROGRESS="1",
            HOME=str(root),
            USERPROFILE=str(root),
            APPDATA=str(root / "appdata"),
            LOCALAPPDATA=str(root / "localappdata"),
            TEMP=str(root),
            TMP=str(root),
            PROBE_ACTIONS=str(root / "cli-actions.txt"),
            PROBE_ROOT=str(root),
            PROBE_MODE=mode,
        )

        invoke = partial(
            subprocess.run,
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )

        wheels = []
        for version in ("0.0.1", "0.0.2"):
            wheel = root / f"omnigent-{version}-py3-none-any.whl"
            info = f"omnigent-{version}.dist-info"
            cli = (
                "import json, os, pathlib, sys, time, signal\n"
                f"VERSION = {version!r}\n"
                "def main():\n"
                "    if '--version' in sys.argv:\n"
                "        sha = 'aaaaaaaa' if VERSION == '0.0.1' "
                f"or {mode!r} == 'version-mismatch' else 'bbbbbbbb'\n"
                "        print('omnigent ' + VERSION + ' (' + sha + ', built fixture)')\n"
                "    elif sys.argv[1:3] == ['host', 'stop']:\n"
                "        pathlib.Path(os.environ['PROBE_ACTIONS']).write_text('stopped')\n"
                "        root = pathlib.Path(os.environ['PROBE_ROOT'])\n"
                "        if VERSION == '0.0.2' and os.environ['PROBE_MODE'] ="
                "= 'stop-fails': sys.exit(3)\n"
                "        if (root / 'host.pid').exists():\n"
                "            try: os.kill(int((root / 'host.pid').read_text()), signal.SIGTERM)\n"
                "            except OSError: pass\n"
                "    elif sys.argv[1:3] == ['host', 'status']:\n"
                "        if VERSION == '0.0.2' and os.environ['PROBE_MODE'] ="
                "= 'status-hangs': time.sleep(120)\n"
                f"        online = not ({mode!r} in "
                "('reconnect-fails','child-survives-stop','stop-fails',"
                "'helper-killed-rollback') and VERSION == '0.0.2')\n"
                "        print(json.dumps({'daemons': [{'host_id': 'fixture', 'pid': 222,\n"
                "            'process': 'online', 'host_status': 'online'}] if online else []}))\n"
            )
            entry = (
                "raise ImportError('injected missing runner dependency')\n"
                if version == "0.0.2" and mode == "import-fails"
                else ""
            )
            files = {
                "omnigent/__init__.py": "",
                "omnigent/cli.py": cli,
                "omnigent/runner/__init__.py": "",
                "omnigent/runner/_entry.py": entry,
                "omnigent/_version.txt": version,
                "omnigent/host/__init__.py": "",
                "omnigent/host/_daemon_entry.py": (
                    "import ctypes, os, pathlib, shutil, subprocess, sys, time\n"
                    "root = pathlib.Path(os.environ['PROBE_ROOT'])\n"
                    "(root / 'host.pid').write_text(str(os.getpid()))\n"
                    "dll = pathlib.Path(sys.prefix) / 'fixture-version.dll'\n"
                    "shutil.copyfile(pathlib.Path(os.environ['SYSTEMROOT']) / 'Sy"
                    "stem32/version.dll', dll)\n"
                    "library = ctypes.WinDLL(str(dll))\n"
                    "child = subprocess.Popen([sys.executable, '-c', "
                    "'import ctypes,time,sys,pathlib,os; ctypes.WinDLL(sys.argv[1"
                    "]); pathlib.Path(sys.argv[2]).write_text(str(os.getpid())); "
                    "time.sleep(120)', str(dll), str(root / 'host-child.pid')], "
                    "creationflags=subprocess.CREATE_NO_WINDOW)\n"
                    "time.sleep(120)\n"
                ),
                f"{info}/METADATA": (
                    f"Metadata-Version: 2.1\nName: omnigent\nVersion: {version}\n"
                ),
                f"{info}/WHEEL": "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
                f"{info}/entry_points.txt": (
                    "[console_scripts]\nomni = omnigent.cli:main\nomnigent = omnigent.cli:main\n"
                ),
            }
            files[f"{info}/RECORD"] = "".join(f"{name},,\n" for name in [*files, f"{info}/RECORD"])
            with zipfile.ZipFile(wheel, "w") as archive:
                for name, content in files.items():
                    archive.writestr(name, content)
            wheels.append(wheel)
        installer = [
            uv,
            "--no-config",
            "tool",
            "install",
            "--offline",
            "--force",
            "--reinstall",
            "--python",
            sys.executable,
        ]
        setup = invoke([*installer, str(wheels[0])])
        assert setup.returncode == 0, setup.stderr
        old_receipt = (venv / "uv-receipt.toml").read_bytes()
        omni = binary / "omni.exe"
        assert invoke([str(omni), "--version"]).stdout.startswith("omnigent 0.0.1")
        supervisor = root / "supervisor.py"
        supervisor.write_text(
            "import os,pathlib,subprocess,sys,time\n"
            "root=pathlib.Path(os.environ['PROBE_ROOT'])\n"
            "with (root/'supervisor-actions.txt').open('a') as f: f.write(sys.argv[1]+'\\n')\n"
            "version=(root/'tools/omnigent/Lib/site-packages/omnigent/_version.txt').read_text()\n"
            "if sys.argv[1]=='start' and version=='0.0.2' and os.environ["
            "'PROBE_MODE'] in ('child-survives-stop','stop-fails','helper-killed-rollback'):\n"
            "    subprocess.Popen([str(root/'tools/omnigent/Scripts/pytho"
            "n.exe'),'-I','-m','omnigent.host._daemon_entry'],creationfla"
            "gs=subprocess.CREATE_NO_WINDOW,stdin=subprocess.DEVNULL,stdo"
            "ut=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
            "    deadline=time.monotonic()+5\n"
            "    while not (root/'host-child.pid').exists() and time.mono"
            "tonic()<deadline: time.sleep(.02)\n"
        )
        wrapper = binary / "omni-host-service.cmd"
        env["PROBE_WRAPPER"] = str(wrapper)
        wrapper.write_text(
            f'@echo off\r\n"{sys.executable}" "{supervisor}" %*\r\nexit /b %errorlevel%\r\n'
        )
        fixture_helper = root / "helper.ps1"
        helper_text = (
            helper.read_text()
            .replace("UtcNow.AddSeconds(60)", "UtcNow.AddSeconds(0)")
            .replace("TimeoutSeconds = 30", "TimeoutSeconds = 1")
            .replace("-TimeoutSeconds 30", "-TimeoutSeconds 5")
        )
        if mode == "helper-killed-rollback":
            point = (
                "                Wait-CustomHostVenvReleased -Instruction $instruction "
                "-LogPath $LogPath -HelperPid $PID -Identities $identities"
            )
            assert point in helper_text
            helper_text = helper_text.replace(
                point,
                "                [IO.File]::WriteAllText("
                "(Join-Path $env:PROBE_ROOT 'rollback-ready'), 'ready')\n"
                "                Start-Sleep -Seconds 120\n" + point,
            )
        fixture_helper.write_text(helper_text)
        namespace = identity_and_wrapper_checks(root, fixture_helper)
        result = root / "result.json"
        log = root / "update.log"
        lock = root / "update.lock"
        instruction = root / ".omnigent" / "updates" / "instruction.json"
        install_args = [*installer, str(wheels[1])]
        if mode in {"helper-killed-missing", "helper-killed-child", "helper-killed-boot"}:
            stalled = root / "stalled-installer.py"
            stalled.write_text(
                "import ctypes,os,pathlib,shutil,subprocess,sys,time\n"
                "root=pathlib.Path(os.environ['PROBE_ROOT'])\n"
                "if os.environ['PROBE_MODE']=='helper-killed-child':\n"
                "    venv=root/'tools/omnigent'; venv.mkdir()\n"
                "    dll=venv/'fixture-version.dll'\n"
                "    shutil.copyfile(pathlib.Path(os.environ['SYSTEMROOT'])/'"
                "System32/version.dll',dll)\n"
                "    child=subprocess.Popen([sys.executable,'-c','import ctyp"
                "es,time,sys; ctypes.WinDLL(sys.argv[1]); time.sleep(120)',st"
                "r(dll)],creationflags=subprocess.CREATE_NO_WINDOW)\n"
                "    (root/'installer-child.pid').write_text(str(child.pid))\n"
                "(root/'installer.pid').write_text(str(os.getpid()))\n"
                "time.sleep(120)\n"
            )
            install_args = [sys.executable, str(stalled)]
        instruction.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "parent_pid": 0,
                    "constraints_dir": "",
                    "tool_venv_path": str(venv),
                    "cli_bin_path": str(omni),
                    "install_argv": install_args,
                    "helper_path": str(fixture_helper),
                    "disabled_marker": str(root / ".omnigent" / "autostart" / "disabled"),
                    "probe_argv": [
                        str(venv / "Scripts" / "python.exe"),
                        "-I",
                        "-c",
                        "import omnigent.cli, omnigent.runner._entry; print('b' * 40)",
                    ],
                    "version_argv": [str(omni), "--version"],
                    "status_argv": [str(omni), "host", "status", "--json"],
                    "previous_records": [],
                    "host_id": "fixture",
                    "old_commit": "a" * 40,
                    "target_commit": "b" * 40,
                    "wrapper_path": str(wrapper),
                    "result_path": str(result),
                    "log_path": str(log),
                    "lock_path": str(lock),
                    "rollback_path": str(root / "rollback.json"),
                }
            )
        )
        lock.write_text("")
        helper_argv = [
            pwsh,
            "-NoProfile",
            "-File",
            str(fixture_helper),
            "-InstructionPath",
            str(instruction),
            "-ResultPath",
            str(result),
            "-LogPath",
            str(log),
            "-LockPath",
            str(lock),
            "-WrapperPath",
            str(wrapper),
            "-OldCommit",
            "a" * 40,
            "-TargetCommit",
            "b" * 40,
            "-HostId",
            "fixture",
        ]
        if mode.startswith("helper-killed"):
            process = subprocess.Popen(
                helper_argv,
                cwd=root,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            deadline = time.monotonic() + 60
            ready = root / (
                "rollback-ready" if mode == "helper-killed-rollback" else "installer.pid"
            )
            while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            assert ready.exists(), result.read_text() if result.exists() else "no receipt"
            installer_pid = (
                int((root / "host-child.pid").read_text())
                if mode == "helper-killed-rollback"
                else int((root / "installer.pid").read_text())
            )
            if mode == "helper-killed-rollback":
                assert not process_alive(int((root / "host.pid").read_text()))
                persisted = json.loads(result.read_text(encoding="utf-8-sig"))
                assert any(p["pid"] == installer_pid for p in persisted["recovery_processes"])
            process.kill()
            process.wait(timeout=5)
            assert process_alive(installer_pid), "Fixture did not leave a surviving installer"
            marker = root / ".omnigent" / "autostart" / "disabled"
            marker.write_text("")
            if mode == "helper-killed-boot":
                completed = invoke(
                    [
                        pwsh,
                        "-NoProfile",
                        "-File",
                        str(marker.with_name("Start-OmnigentHostDefault.ps1")),
                    ]
                )
                assert not marker.exists(), "Boot recovery did not resume autostart"
            else:
                completed = invoke(
                    [
                        pwsh,
                        "-NoProfile",
                        "-Command",
                        "& $env:PROBE_WRAPPER start; exit $LASTEXITCODE",
                    ]
                )
            assert completed.returncode == 0, completed.stderr
            assert not process_alive(installer_pid), "Installer survived independent recovery"
            child_file = root / "installer-child.pid"
            if child_file.exists():
                assert not process_alive(int(child_file.read_text())), (
                    "Installer descendant survived its guard"
                )
        else:
            completed = invoke(helper_argv)
        if mode == "reconciled-healthy":
            receipt = json.loads(result.read_text(encoding="utf-8-sig"))
            receipt.update(status="failed", phase="rolling_back", error="Restore blocked")
            result.write_text(json.dumps(receipt))
            (Path(receipt["backup_path"]).parent / "transaction.json").write_text(
                json.dumps(receipt)
            )
            namespace["_read_custom_host_update_result"] = lambda result=result: json.loads(
                result.read_text()
            )
            namespace["_custom_host_update_paths"] = lambda result=result, lock=lock: {
                "result": result,
                "lock": lock,
            }
            with patch.dict(
                sys.modules,
                {
                    "omnigent.install_ledger": SimpleNamespace(
                        atomic_write_json=lambda path, value: path.write_text(json.dumps(value)),
                    )
                },
            ):
                namespace["_reconcile_custom_host_update_result"]("b" * 40, persist=True)
            healthy = (venv / "uv-receipt.toml").read_bytes()
            resumed = invoke(
                [pwsh, "-NoProfile", "-Command", "& $env:PROBE_WRAPPER start; exit $LASTEXITCODE"]
            )
            assert resumed.returncode == 0, resumed.stderr
            assert (venv / "uv-receipt.toml").read_bytes() == healthy
        payload = json.loads(result.read_text(encoding="utf-8-sig"))
        succeeded = mode in {"success", "reconciled-healthy"}
        expected = "complete" if succeeded else "failed"
        assert payload["status"] == expected, (payload, completed.stderr)
        if not mode.startswith("helper-killed"):
            assert completed.returncode == (0 if succeeded else 1), completed.stderr
        version = invoke([str(omni), "--version"])
        assert version.returncode == 0, version.stderr
        assert version.stdout.startswith("omnigent " + ("0.0.2" if succeeded else "0.0.1"))
        if not succeeded:
            assert payload["restored"], payload
            if not mode.startswith("helper-killed"):
                assert payload["recovery_status"] == "online", payload
            assert (venv / "uv-receipt.toml").read_bytes() == old_receipt
        if mode in {"reconnect-fails", "child-survives-stop", "stop-fails"}:
            assert (root / "cli-actions.txt").read_text() == "stopped"
            assert "stop" in (root / "supervisor-actions.txt").read_text().split()
        child_file = root / "host-child.pid"
        if child_file.exists():
            assert not process_alive(int(child_file.read_text())), (
                "Replacement Host child survived rollback"
            )
        assert not lock.exists()
        print(
            f"PASS real uv {mode}: {payload['status']}, runnable version {version.stdout.strip()}"
        )


if __name__ == "__main__":
    run(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve(), sys.argv[3], sys.argv[4])
