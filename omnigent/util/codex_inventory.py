"""Bounded loaded-thread evidence shared by Codex websocket and stdio owners."""

import asyncio
from collections.abc import Awaitable, Callable

from omnigent.util.json_types import JsonObject


async def read_loaded_codex_thread_ids(
    request: Callable[[str, JsonObject], Awaitable[JsonObject]],
    is_current: Callable[[], bool],
    *,
    timeout_s: float = 5.0,
) -> tuple[str, ...] | None:
    """None is unknown; IDs alone prove neither ownership, aliases nor absence."""
    if not 0 < timeout_s <= 5.0 or not is_current():
        return None
    ids: list[str] = []
    cursors: set[str] = set()
    cursor: str | None = None
    try:
        async with asyncio.timeout(timeout_s):
            for _ in range(10):
                if not is_current():
                    return None
                params: JsonObject = {"limit": 100}
                if cursor is not None:
                    params["cursor"] = cursor
                response = await request("thread/list/loaded", params)
                if not is_current():
                    return None
                result = response.get("result")
                if not isinstance(result, dict) or "nextCursor" not in result:
                    return None
                page = result.get("data")
                if (
                    not isinstance(page, list)
                    or len(page) > 100
                    or any(not isinstance(item, str) or not item for item in page)
                ):
                    return None
                ids.extend(page)
                if len(set(ids)) != len(ids):
                    return None
                cursor = result["nextCursor"]
                if cursor is None:
                    return tuple(ids)
                if not isinstance(cursor, str) or not cursor or cursor in cursors:
                    return None
                cursors.add(cursor)
    except asyncio.CancelledError:
        # Owners can cancel pending RPC futures independently of the caller.
        task = asyncio.current_task()
        if task is None or task.cancelling():
            raise
    except (TimeoutError, ConnectionError, RuntimeError):
        pass
    return None
