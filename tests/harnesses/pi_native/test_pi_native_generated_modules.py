"""Load the actual native bridge and required child guard with Node's resolver."""

from __future__ import annotations

import shutil
import subprocess
from importlib.resources import files
from pathlib import Path

import pytest

from omnigent.harnesses.pi_native.bridge import write_extension_files


@pytest.mark.parametrize("origin", ["parent", "child"])
def test_native_extension_modules_load(tmp_path: Path, origin: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to load Pi extensions")
    if origin == "parent":
        extension, _ = write_extension_files(
            tmp_path / "bridge",
            session_id="fixture-session",
            server_url="http://omnigent.test",
            conversation_url="http://omnigent.test/c/fixture-session",
        )
    else:
        # Child sessions load this packaged guard, not a second generated bridge.
        extension = Path(str(files("omnigent.resources.pi_native"))) / (
            "omnigent_pi_child_extension.js"
        )
    result = subprocess.run(
        [
            node,
            "-e",
            'require("node:assert/strict").equal(typeof require(process.argv[1]), "function");',
            str(extension),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
