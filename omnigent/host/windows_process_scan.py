"""Bounded subprocess entry for the Windows custom updater's candidate mappings."""

from __future__ import annotations

import contextlib
import json
import sys
from collections.abc import Sequence

import psutil


def inspect_loaded_paths(identities: Sequence[dict[str, int | float]]) -> dict[str, list[str]]:
    """Read candidate mappings, ignoring processes that disappeared or changed identity."""
    paths: dict[str, list[str]] = {}
    for identity in identities:
        with contextlib.suppress(psutil.Error, OSError):
            proc = psutil.Process(int(identity["pid"]))
            if identity["created_at"] <= 0 or proc.create_time() != identity["created_at"]:
                continue
            # open_files() is incomplete on Windows and can stall on handle
            # names. The installer separately verifies exclusive file access.
            mappings = proc.memory_maps()  # pyrefly: ignore[missing-attribute]
            paths[str(proc.pid)] = sorted({item.path for item in mappings})
    return paths


if __name__ == "__main__":
    json.dump(inspect_loaded_paths(json.load(sys.stdin)), sys.stdout)
