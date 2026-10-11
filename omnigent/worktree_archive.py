"""Archive policy shared by the server and host Git reader."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

DEFAULT_REBUILDABLE_PATHS = [
    "node_modules/",
    ".venv/",
    "venv/",
    "__pycache__/",
    ".pytest_cache/",
    ".ruff_cache/",
    ".mypy_cache/",
    ".tox/",
    "dist/",
    "build/",
    ".next/",
    "coverage/",
]
ARCHIVE_MODES = frozenset({"never", "green_only", "yellow_too"})


def validate_rebuildable_paths(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > 64:
        raise ValueError("rebuildable_paths must be a list of at most 64 paths")
    paths: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > 256 or "\\" in item:
            raise ValueError("invalid rebuildable path")
        if any(ord(char) < 32 for char in item) or any(char in item for char in "*?[]{}$~:"):
            raise ValueError("rebuildable path cannot contain expansion syntax")
        path = item.rstrip("/")
        parts = path.split("/")
        if (
            PurePosixPath(item).is_absolute()
            or not path
            or any(part in ("", ".", "..") or part.casefold() == ".git" for part in parts)
        ):
            raise ValueError("rebuildable path must be a relative directory")
        paths.append(item)
    if len(set(paths)) != len(paths):
        raise ValueError("duplicate rebuildable path")
    return paths


def normalize_archive_preference(value: Any) -> dict[str, Any]:
    default = {
        "mode": "never",
        "ask_before_archive": True,
        "rebuildable_paths": DEFAULT_REBUILDABLE_PATHS.copy(),
    }
    if value is None:
        return default
    if not isinstance(value, dict):
        raise ValueError("invalid worktree_archive preference")
    if value == {"mode": "delete_safe"}:
        return {**default, "mode": "green_only"}
    if value == {"mode": "never"}:
        return {**default, "ask_before_archive": False}
    if set(value) != set(default) or value.get("mode") not in ARCHIVE_MODES:
        raise ValueError("invalid worktree_archive preference")
    if type(value.get("ask_before_archive")) is not bool:
        raise ValueError("ask_before_archive must be boolean")
    return {**value, "rebuildable_paths": validate_rebuildable_paths(value["rebuildable_paths"])}


def file_kind(path: str, status: str, rebuildable_paths: list[str]) -> str:
    if status != "!!":
        return "source"
    parts = path.rstrip("/").split("/")
    is_directory = path.endswith("/")
    for pattern in rebuildable_paths:
        target = pattern.rstrip("/").split("/")
        if len(target) == 1 and (
            target[0] in parts[:-1] or (is_directory and parts[-1] == target[0])
        ):
            return "rebuildable"
        if (
            len(target) > 1
            and parts[: len(target)] == target
            and (len(parts) > len(target) or is_directory)
        ):
            return "rebuildable"
    return "local"


def worktree_state(files: list[dict[str, str]]) -> str:
    kinds = {file["kind"] for file in files}
    return "dirty" if "source" in kinds else "local" if "local" in kinds else "clean"


def archive_can_remove(state: str, mode: str) -> bool:
    return mode != "never" and (state == "clean" or (state == "local" and mode == "yellow_too"))
