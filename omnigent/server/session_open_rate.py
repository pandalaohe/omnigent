"""Process-local open-rate admission for ``sys_session_open``.

One sliding window per session owner, read from the owner's
``session_collab`` preferences on every call so a settings change takes
effect immediately. Process-local and bounded by owner count, matching
the server's other process-local guards (``_PeerAdmission``, the hand-off
owner locks).
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any

from omnigent.server.user_preferences_store import read_collab_settings

# owner → monotonic timestamps of admitted opens inside the window.
_OPEN_TIMESTAMPS: dict[str, deque[float]] = {}


def _window_text(window_s: int) -> str:
    """Render a window in seconds as the settings-UI wording.

    :param window_s: Window length in seconds, e.g. ``60``.
    :returns: ``"1 minute"``, ``"N minutes"`` or ``"N seconds"``.
    """
    if window_s == 60:
        return "1 minute"
    if window_s % 60 == 0:
        return f"{window_s // 60} minutes"
    return f"{window_s} seconds"


def admit_open(app_state: Any, owner: str) -> str | None:
    """Admit one session open for *owner*, or return the refusal text.

    Reads the owner's collaboration settings first (missing store, owner,
    namespace or malformed row → defaults), then drops timestamps older
    than the window and refuses when the window already holds
    ``open_rate_count`` opens. An admitted open appends the current
    monotonic time.

    :param app_state: FastAPI app state carrying
        ``user_preferences_store`` (may be absent/``None``).
    :param owner: Session owner whose window applies.
    :returns: ``None`` when admitted, else the human refusal text.
    """
    settings = read_collab_settings(getattr(app_state, "user_preferences_store", None), owner)
    count = settings.open_rate_count
    window_s = settings.open_rate_window_s
    now = time.monotonic()
    stamps = _OPEN_TIMESTAMPS.setdefault(owner, deque())
    while stamps and stamps[0] <= now - window_s:
        stamps.popleft()
    if len(stamps) >= count:
        return (
            f"Opening sessions too fast (setting: {count} per "
            f"{_window_text(window_s)}; Settings > General > Session collaboration)"
        )
    stamps.append(now)
    return None
