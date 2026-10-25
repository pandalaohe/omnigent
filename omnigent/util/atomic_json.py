"""Atomic replacement of small local JSON state files."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path


def atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    """Replace a JSON file without following a symlinked destination."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise OSError(f"Refusing symlinked JSON destination: {path}")
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, allow_nan=False)
        if path.is_symlink():
            raise OSError(f"Refusing symlinked JSON destination: {path}")
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
