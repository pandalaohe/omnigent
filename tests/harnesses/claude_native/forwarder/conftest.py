"""Per-test bridge isolation for the forwarder suite."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

import omnigent.harnesses.claude_native.forwarder as forwarder


@pytest.fixture(autouse=True)
def _allow_tmp_path_as_bridge_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    Treat each test's temp dir as the Claude bridge root.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp directory.
    :returns: None.
    """
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._BRIDGE_ROOT", tmp_path)


@pytest.fixture(autouse=True)
def _clear_spawn_tool_use_id_cache() -> Iterator[None]:
    """
    Drop the process-local spawn-id cursor cache between tests.

    :returns: None.
    """
    forwarder._SPAWN_TOOL_USE_ID_CACHE.clear()
    yield
    forwarder._SPAWN_TOOL_USE_ID_CACHE.clear()


@pytest.fixture(autouse=True)
def _isolate_auto_compact_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Keep ambient auto-compaction settings out of the suite.

    The forwarder posts the user's ``CLAUDE_CODE_AUTO_COMPACT_WINDOW`` (from
    the real ``~/.claude/settings.json`` or the shell) as a usage field, so a
    developer who set it would add a POST to every exact-request assertion.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    monkeypatch.setattr(forwarder, "read_user_auto_compact_window", lambda: None)
