"""Exercise startup ordering through the native bootstrap's Node consumer."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest


def test_stock_subagents_startup_keeps_base_tools(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for native startup tests")
    script = (
        Path(__file__).resolve().parents[3]
        / "omnigent/resources/pi_native/native_support.test.cjs"
    )
    result = subprocess.run(
        [node, "--test", "--test-name-pattern", "startup registrations", str(script)],
        capture_output=True,
        text=True,
        timeout=45,
        env={**os.environ, "HOME": str(tmp_path), "TMPDIR": str(tmp_path)},
    )
    assert result.returncode == 0, result.stdout + result.stderr
