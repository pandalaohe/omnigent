"""Atomicity of the process-local session-open rate limiter."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

from omnigent.server import session_open_rate


class _Preferences:
    """Minimal ``user_preferences_store`` for the collab namespace."""

    def __init__(self, settings: dict[str, Any]) -> None:
        self._settings = settings

    def get(self, _user_id: str) -> dict[str, Any] | None:
        return {"version": 1, "settings": {"session_collab": self._settings}}


def test_concurrent_admits_for_one_owner_admit_exactly_one() -> None:
    """Two threads entering together admit only one open of a full window."""
    state = SimpleNamespace(user_preferences_store=_Preferences({"openRateCount": 1}))
    barrier = threading.Barrier(2)
    results: list[str | None] = []
    results_lock = threading.Lock()

    def admit() -> None:
        barrier.wait()
        text = session_open_rate.admit_open(state, "alice@example.com")
        with results_lock:
            results.append(text)

    threads = [threading.Thread(target=admit) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) == 2
    assert sum(text is None for text in results) == 1
    assert sum(text is not None for text in results) == 1
