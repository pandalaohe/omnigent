"""Verify the root conftest scrubs inherited runner-session variables.

CI never launches pytest from inside an agent session, so the in-process
assertion here (:func:`test_inherited_runner_env_absent`) is vacuous there:
it passes whether or not conftest scrubs, because the variables were never
set. The subprocess test re-runs that assertion with the runner variables
injected, so the scrub itself is verified on any machine.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import _INHERITED_RUNNER_ENV_NAMES, _INHERITED_RUNNER_ENV_PREFIX


def test_inherited_runner_env_absent() -> None:
    """No inherited runner-session variable survives conftest import."""
    prefix_hits = [name for name in os.environ if name.startswith(_INHERITED_RUNNER_ENV_PREFIX)]
    name_hits = sorted(name for name in _INHERITED_RUNNER_ENV_NAMES if name in os.environ)
    assert prefix_hits == []
    assert name_hits == []


@pytest.mark.timeout(240)
def test_conftest_drops_inherited_runner_env() -> None:
    """A subprocess run with runner vars set still passes the assertion."""
    repo_root = Path(__file__).resolve().parents[1]
    env = {
        **os.environ,
        "OMNIGENT_RUNNER_ID": "runner_fake",
        "OMNIGENT_RUNNER_PARENT_PID": "1",
        "OMNIGENT_PROCESS_LOG_FILE": "/opt/work/omnigent/fork/wt/runner.log",
        "OMNIGENT_RUNNER_CONNECT_MARKER": "/opt/work/omnigent/fork/wt/runner.connected",
        "RUNNER_SERVER_URL": "http://127.0.0.1:9",
        "OMNIGENT_TEST_ORPHAN_SWEEP": "0",
    }
    for name in ("PYTEST_XDIST_WORKER", "PYTEST_XDIST_TESTRUNUID", "PYTEST_CURRENT_TEST"):
        env.pop(name, None)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            f"{__file__}::test_inherited_runner_env_absent",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:xdist",
        ],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
