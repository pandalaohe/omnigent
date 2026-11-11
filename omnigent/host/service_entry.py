"""Process entry point used by operating-system host services."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import click

from omnigent.host import HOST_FATAL_EXIT_CODE

# How often a service host waiting behind another daemon checks whether it still runs.
_STANDBY_POLL_S = 60.0
_WINDOWS_RESTART_BACKOFF_S = 5.0


def _load_environment(path: Path) -> None:
    from omnigent.host.service import _clean_environment

    environment = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(environment, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in environment.items()
    ):
        raise ValueError("Host service environment must be an object of string values.")
    environment = _clean_environment(environment)
    os.environ.clear()
    os.environ.update(environment)


def _windows_supervise(arguments: list[str]) -> int:
    """Relaunch failed workers until normal or permanent exit.

    Remove when Task Scheduler restarts non-zero action exits or upstream
    supplies a Windows host service.
    """
    while True:
        result = subprocess.run(
            [
                sys.executable,
                "-P",
                "-m",
                "omnigent.host.service_entry",
                *arguments,
                "--service-worker",
            ],
            check=False,
        )
        if result.returncode in (0, HOST_FATAL_EXIT_CODE):
            return 0
        time.sleep(_WINDOWS_RESTART_BACKOFF_S)


def _windows_stdio() -> None:
    if sys.stdout is None or sys.stderr is None:
        from omnigent.process_logging import data_dir

        path = data_dir() / "logs" / "host" / "service.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        output = path.open("a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = output
        if sys.stderr is None:
            sys.stderr = output


def _wait_while_workspace_is_served(server: str) -> None:
    """Wait while another live daemon already serves *server*'s workspace.

    The host would be refused and exit, and the service manager would restart
    it every few seconds. Waiting keeps the other daemon as the only one and
    lets this service take over once it stops, up to a minute later.

    :param server: Remote Omnigent server URL, e.g.
        ``"https://example.databricks.com/omnigent"``.
    """
    from omnigent.cli import _resolve_host_server, _same_workspace_daemon

    resolved = _resolve_host_server(server)
    announced = False
    while (sibling := _same_workspace_daemon(resolved)) is not None:
        if not announced:
            click.echo(
                f"Host daemon pid {sibling.pid} already serves this workspace through "
                f"{sibling.target}; waiting until it stops.",
                err=True,
            )
            announced = True
        time.sleep(_STANDBY_POLL_S)


def main() -> int:
    """Run the foreground host command and normalize permanent failures."""
    parser = argparse.ArgumentParser(description="Omnigent host service")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--server", help="Remote Omnigent server URL.")
    mode.add_argument("--local", action="store_true", help="Run a local Omnigent server.")
    parser.add_argument("--env-file", type=Path, help="Saved service environment.")
    parser.add_argument("--service-worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.env_file is not None:
        _load_environment(args.env_file)
    if sys.platform == "win32":
        _windows_stdio()
        if args.env_file is not None:
            from omnigent.host.windows_task import SUPERVISOR_ENV

            os.environ[SUPERVISOR_ENV] = "windows_task"
            if not args.service_worker:
                return _windows_supervise(sys.argv[1:])

    from omnigent.host.restart import mark_service_entry

    mark_service_entry()

    from omnigent.cli import cli

    server = "" if args.local else args.server
    try:
        if server:
            _wait_while_workspace_is_served(server)
        cli.main(
            args=["host", "--server", server, "--non-interactive"],
            prog_name="omnigent",
            standalone_mode=False,
        )
    except click.ClickException as exc:
        exc.show()
        return exc.exit_code
    except click.Abort:
        click.echo("Aborted!", err=True)
        return 1
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        # A permanent auth/config failure should leave the service enabled but
        # stopped instead of entering a supervisor restart loop.
        return 0 if code == HOST_FATAL_EXIT_CODE else code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
