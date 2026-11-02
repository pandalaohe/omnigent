"""Native uv transactions against tiny local wheels, with isolated tool directories."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from functools import partial
from pathlib import Path


def run(helper: Path, fixtures: Path, uv: str, pwsh: str) -> None:
    """Exercise uv receipts, launchers and rollback without the installed application."""
    for mode in ("success", "import-fails", "reconnect-fails"):
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
                "import json, os, pathlib, sys\n"
                f"VERSION = {version!r}\n"
                "def main():\n"
                "    if '--version' in sys.argv: print(VERSION)\n"
                "    elif sys.argv[1:3] == ['host', 'stop']:\n"
                "        pathlib.Path(os.environ['PROBE_ACTIONS']).write_text('stopped')\n"
                "    elif sys.argv[1:3] == ['host', 'status']:\n"
                f"        online = not ({mode!r} == 'reconnect-fails' and VERSION == '0.0.2')\n"
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
        assert invoke([str(omni), "--version"]).stdout.strip() == "0.0.1"
        wrapper = root / "supervisor.cmd"
        wrapper.write_text('@echo off\r\n>>"%~dp0supervisor-actions.txt" echo %1\r\nexit /b 0\r\n')
        result = root / "result.json"
        log = root / "update.log"
        lock = root / "update.lock"
        instruction = root / "instruction.json"
        instruction.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "parent_pid": 0,
                    "constraints_dir": "",
                    "tool_venv_path": str(venv),
                    "cli_bin_path": str(omni),
                    "install_argv": [*installer, str(wheels[1])],
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
        fixture_helper = root / "helper.ps1"
        # Only shorten the reconnection deadline; execute the real entry point.
        fixture_helper.write_text(
            helper.read_text().replace("UtcNow.AddSeconds(60)", "UtcNow.AddSeconds(0)")
        )
        completed = invoke(
            [
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
        )
        payload = json.loads(result.read_text(encoding="utf-8-sig"))
        expected = "complete" if mode == "success" else "failed"
        assert payload["status"] == expected, (payload, completed.stderr)
        assert completed.returncode == (0 if mode == "success" else 1), completed.stderr
        version = invoke([str(omni), "--version"])
        assert version.returncode == 0, version.stderr
        assert version.stdout.strip() == ("0.0.2" if mode == "success" else "0.0.1")
        if mode != "success":
            assert payload["restored"] and payload["recovery_status"] == "online", payload
            assert (venv / "uv-receipt.toml").read_bytes() == old_receipt
        if mode == "reconnect-fails":
            assert (root / "cli-actions.txt").read_text() == "stopped"
            assert "stop" in (root / "supervisor-actions.txt").read_text().split()
        assert not lock.exists()
        print(
            f"PASS real uv {mode}: {payload['status']}, runnable version {version.stdout.strip()}"
        )


if __name__ == "__main__":
    run(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve(), sys.argv[3], sys.argv[4])
