"""Shared Pi CLI argv for RPC startup and version discovery."""

from __future__ import annotations

import re
import sys
from pathlib import Path

from omnigent._platform import IS_WINDOWS, resolve_cli_binary


def pi_cli_argv(pi_path: str) -> list[str]:
    """Launch JS through Node and bypass npm's Windows shell shim."""
    path = Path(pi_path)
    if not IS_WINDOWS:
        return [pi_path]
    if path.suffix.lower() in {".cmd", ".bat"}:
        # Running the batch file would let cmd reinterpret prompts and metacharacters.
        shim = path.read_text(encoding="utf-8")
        entry = re.search(r'"%dp0%[\\/]([^"\r\n]+\.[cm]?js)"', shim, re.IGNORECASE)
        if entry is None:
            raise OSError(
                "Pi batch launcher is not an npm shim; configure a native executable instead"
            )
        script = (path.parent / entry[1].replace("\\", "/")).resolve()
    elif path.suffix.lower() in {".js", ".cjs", ".mjs"}:
        script = path.resolve()
    elif path.suffix.lower() == ".py":
        return [sys.executable, "-P", str(path)]
    else:
        return [pi_path]
    if not script.is_file():
        raise FileNotFoundError(f"Pi npm entry point does not exist: {script}")
    sibling_node = path.parent / "node.exe"
    node = str(sibling_node) if sibling_node.is_file() else resolve_cli_binary("node")
    if not node:
        raise FileNotFoundError("Pi launcher requires Node.js")
    return [node, str(script)]
