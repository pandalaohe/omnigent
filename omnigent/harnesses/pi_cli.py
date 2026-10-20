"""Shared Pi CLI argv for RPC startup and version discovery."""

from __future__ import annotations

import re
from pathlib import Path

from omnigent._platform import IS_WINDOWS, resolve_cli_binary


def pi_cli_argv(pi_path: str) -> list[str]:
    """Bypass npm's Windows shell shim so Pi arguments remain literal."""
    path = Path(pi_path)
    if not IS_WINDOWS or path.suffix.lower() not in {".cmd", ".bat"}:
        return [pi_path]
    # npm's cmd-shim quotes its JS entry relative to %dp0%. Executing the
    # batch file would let cmd reinterpret prompts, %variables% and metacharacters.
    shim = path.read_text(encoding="utf-8")
    entry = re.search(r'"%dp0%[\\/]([^"\r\n]+\.[cm]?js)"', shim, re.IGNORECASE)
    if entry is None:
        raise OSError(
            "Pi batch launcher is not an npm shim; configure a native executable instead"
        )
    script = (path.parent / entry[1].replace("\\", "/")).resolve()
    if not script.is_file():
        raise FileNotFoundError(f"Pi npm entry point does not exist: {script}")
    sibling_node = path.parent / "node.exe"
    node = str(sibling_node) if sibling_node.is_file() else resolve_cli_binary("node")
    if not node:
        raise FileNotFoundError("Pi npm launcher requires Node.js")
    return [node, str(script)]
