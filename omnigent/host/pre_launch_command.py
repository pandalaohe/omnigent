"""Run the host's own pre-launch command before a runner starts.

The command comes from the host's startup config (``host.pre_launch_command``),
so no server-side value can choose what runs. It runs once per launch in the
launch workspace, shell-free, and is best-effort: a malformed key, a failure
or a timeout is logged and the launch goes ahead.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path

from omnigent.host.post_bind_hook import (
    _CommandConfigError,
    _host_env,
    _load_command,
    _read_tail,
)

_logger = logging.getLogger(__name__)

# Wall-clock cap on one run. A module constant so tests can lower it. The
# server waits 30 s for a UI launch's result, and the harness check before
# this may already take 10 s.
PRE_LAUNCH_TIMEOUT_S: float = 10.0


def run_pre_launch_command(config_path: Path, workspace: Path) -> None:
    """Run ``host.pre_launch_command`` in ``workspace`` when it is configured.

    Blocking by design: callers run it off the event loop with the orphan
    reaper paused. Never raises for a command or config failure.

    :param config_path: The config file the host process was started with;
        re-read on every launch so an edit applies without a restart.
    :param workspace: The launch workspace, used as the command's cwd.
    """
    try:
        argv = _load_command(config_path, "pre_launch_command")
    except _CommandConfigError as exc:
        _logger.warning("Pre-launch command not run: %s", exc)
        return
    if argv is None:
        return
    # Output goes to a temp file, never a pipe: no reader can block on a
    # descendant that inherited the write end.
    with tempfile.TemporaryFile() as output:
        try:
            proc = subprocess.Popen(
                argv,
                cwd=workspace,
                env=_host_env(),
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                shell=False,
            )
        except OSError as exc:
            _logger.warning("Pre-launch command failed to start in %s: %s", workspace, exc)
            return
        try:
            exit_code = proc.wait(timeout=PRE_LAUNCH_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            _logger.warning(
                "Pre-launch command timed out after %.0f s in %s: %s",
                PRE_LAUNCH_TIMEOUT_S,
                workspace,
                _read_tail(output),
            )
            return
        if exit_code != 0:
            _logger.warning(
                "Pre-launch command exited %d in %s: %s",
                exit_code,
                workspace,
                _read_tail(output),
            )
            return
        _logger.info("Pre-launch command ran in %s: %s", workspace, _read_tail(output))
