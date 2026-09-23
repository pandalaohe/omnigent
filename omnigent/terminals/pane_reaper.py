"""Idle reaper for native-harness terminal panes (issue #1349).

Native CLI sessions (``claude-native`` / ``codex-native`` / ``cursor-native`` /
...) run their vendor CLI plus a full MCP fleet inside a persistent tmux pane
held for the whole conversation lifetime. Unlike the SDK harness proxies — which
``HarnessProcessManager._idle_reaper_loop`` reaps after an idle window — these
panes have no idle reaper, so on a shared/multi-conversation runner memory grows
without bound as idle conversations accumulate, independent of how many are
actually active (#1349).

This reaps a single native pane only when it is genuinely unused. "Busy" is the
disjunction of three signals (any one spares the pane):

  * an in-flight runner turn (``has_active_turn``), OR
  * the pane's PTY watcher currently reports ``running`` — i.e. the vendor CLI is
    working autonomously *between* runner turns (native turns clear the runner's
    ``_active_turns`` right after the prompt is pasted, so this is the load-bearing
    signal for a long autonomous turn), OR
  * a tmux client is attached (a human is watching the pane).

A pane idle on all three for longer than the window is reaped, with a **second
busy re-check immediately before teardown** to close the select→reap race. The
tmux client probe is a blocking ``subprocess`` call, so it runs off the event
loop via ``asyncio.to_thread``.

Teardown is **pane-scoped** (``reap`` closes only the one native terminal, not
the conversation's other terminals), leaving the session's primary OSEnv +
server-side transcript intact — the next message re-creates the pane and the
vendor CLI resumes via its own ``--resume``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

# A pane whose window emitted output this recently counts as busy. tmux's own
# activity clock is evidence independent of the harness status pipeline, whose
# silent stall must not get a live, producing terminal reaped. Two reaper scan
# intervals, so any output between scans re-arms the idle clock.
PANE_OUTPUT_BUSY_WINDOW_S = 120.0

# Native CLI panes are keyed (conversation_id, <harness short name>, "main") in
# the terminal registry. These short names match the ``terminal_name`` the
# per-harness ``_auto_create_<harness>_terminal`` paths launch with. This is the
# cheap name pre-filter; the wiring additionally confirms the registry resource
# ROLE is a native harness (so a user terminal that merely shares the name is not
# reaped — see ``create_runner_app``).
#
# "kimi" is deliberately absent: kimi records no resumable chat id, so a reaped
# pane cannot be re-created with its context — the next turn would silently
# start a fresh TUI. Keep kimi panes alive until the session is torn down.
NATIVE_PANE_TERMINAL_NAMES: frozenset[str] = frozenset(
    {
        "claude",
        "codex",
        "cursor",
        "goose",
        "hermes",
        "kiro",
        "qwen",
        "pi",
        "antigravity",
        "opencode",
    }
)

# Default idle window before an unused native pane is reaped. Mirrors
# ``HarnessProcessManager``'s 1-hour SDK-proxy default for consistency.
_DEFAULT_IDLE_TIMEOUT_S = 60 * 60
_DEFAULT_REAPER_INTERVAL_S = 60.0
_IDLE_TIMEOUT_ENV = "OMNIGENT_NATIVE_PANE_IDLE_TIMEOUT_S"
RETENTION_LEASE_S = 1200
PENDING_RETIRE_MAX_AGE_S = 3600
PENDING_RETIRE_CLOSE_TIMEOUT_S = 5.0


@dataclass(frozen=True)
class PaneRef:
    """A live native CLI pane the reaper may reclaim.

    :param conversation_id: AP-allocated conversation id, e.g. ``"conv_abc123"``.
    :param terminal_id: Resource id of the native terminal, e.g.
        ``terminal_resource_id("claude", "main")`` — used for pane-scoped close.
    :param terminal_name: Harness short-name, e.g. ``"claude"``.
    :param socket_path: tmux socket for the attached-client probe.
    """

    conversation_id: str
    terminal_id: str
    terminal_name: str
    socket_path: Path
    instance: Any | None = None


class NativePaneStillAlive(RuntimeError):
    def __init__(self, instance: Any, probe: bool | None) -> None:
        super().__init__("native pane still alive before retention release")
        self.instance = instance
        self.probe = probe


@dataclass
class _RetireRecord:
    instance: Any
    conversation_id: str
    terminal_name: str
    first_seen: float
    tail_token: str | None
    warned: bool = False
    cleanup_warned: bool = False


def resolve_native_pane_idle_timeout_s() -> float:
    """Resolve the native-pane idle window in seconds.

    Honors :envvar:`OMNIGENT_NATIVE_PANE_IDLE_TIMEOUT_S` (``0`` disables pane
    reaping); otherwise the 1-hour default. An unparseable or negative value
    logs a warning and falls back to the default rather than failing the runner
    at boot — an env typo shouldn't take the runner down or (worse) make the
    reaper act on a bogus window.
    """
    raw = os.environ.get(_IDLE_TIMEOUT_ENV)
    if not raw:
        return float(_DEFAULT_IDLE_TIMEOUT_S)
    try:
        value = float(raw)
    except ValueError:
        _logger.warning(
            "%s=%r is not a number; using default %ss",
            _IDLE_TIMEOUT_ENV,
            raw,
            _DEFAULT_IDLE_TIMEOUT_S,
        )
        return float(_DEFAULT_IDLE_TIMEOUT_S)
    if value < 0:
        _logger.warning(
            "%s=%r is negative; using default %ss",
            _IDLE_TIMEOUT_ENV,
            raw,
            _DEFAULT_IDLE_TIMEOUT_S,
        )
        return float(_DEFAULT_IDLE_TIMEOUT_S)
    return value


class NativePaneReaper:
    """Background task that reaps idle, unattended native terminal panes.

    :param list_native_panes: Returns the currently-live native panes (already
        role-confirmed by the caller) as :class:`PaneRef` values.
    :param is_busy: ``async`` predicate — ``True`` if the pane has an in-flight
        turn, is reporting ``running``, or has an attached tmux client. Async so
        the (blocking) tmux probe runs off the event loop.
    :param reap: ``async`` pane-scoped teardown — closes only this one native
        terminal, leaving the session resumable.
    :param idle_timeout_s: Idle window before reaping. ``None`` resolves the env
        knob; ``<= 0`` disables legacy idle reaping.
    :param reaper_interval_s: Seconds between scans.
    """

    def __init__(
        self,
        *,
        list_native_panes: Callable[[], list[PaneRef]],
        is_busy: Callable[[PaneRef], Awaitable[bool]],
        reap: Callable[[PaneRef], Awaitable[None]],
        idle_timeout_s: float | None = None,
        reaper_interval_s: float = _DEFAULT_REAPER_INTERVAL_S,
        runtime_token: Callable[[str], str] | None = None,
        finish_retired: Callable[[str, str, str], Awaitable[str | None]] | None = None,
    ) -> None:
        self._list_native_panes = list_native_panes
        self._is_busy = is_busy
        self._reap = reap
        self._idle_timeout_s = (
            idle_timeout_s if idle_timeout_s is not None else resolve_native_pane_idle_timeout_s()
        )
        self._reaper_interval_s = reaper_interval_s
        self._runtime_token = runtime_token
        self._finish_retired = finish_retired
        # conversation_id -> monotonic time it was last observed busy.
        self._last_busy_at: dict[str, float] = {}
        # Server-managed sessions use the configurable pool policy. Their
        # panes are still observed here, but the legacy per-pane TTL may not
        # close them independently.
        self._managed_conversations: dict[str, float] = {}
        self._pending_retire: dict[int, _RetireRecord] = {}
        self._task: asyncio.Task[None] | None = None
        self._started = False

    def manage(self, conversation_id: str) -> None:
        """Hand one conversation's automatic close decision to the Server pool."""
        self._managed_conversations[conversation_id] = time.monotonic() + RETENTION_LEASE_S

    def unmanage(self, conversation_id: str) -> None:
        """Return a conversation to the legacy timeout policy."""
        self._managed_conversations.pop(conversation_id, None)

    def _is_managed(self, conversation_id: str, now: float) -> bool:
        expiry = self._managed_conversations.get(conversation_id)
        if expiry is None:
            return False
        if expiry <= now:
            self._managed_conversations.pop(conversation_id, None)
            _logger.info(
                "native pane retention lease expired for conversation %s", conversation_id
            )
            return False
        return True

    def _record_pending(self, pane: PaneRef, instance: Any) -> None:
        self._pending_retire.setdefault(
            id(instance),
            _RetireRecord(
                instance,
                pane.conversation_id,
                pane.terminal_name,
                time.monotonic(),
                self._runtime_token(pane.conversation_id) if self._runtime_token else None,
            ),
        )

    def _owes_tail(self, conversation_id: str) -> bool:
        return any(
            record.conversation_id == conversation_id and record.tail_token is not None
            for record in self._pending_retire.values()
        )

    def note_activity(self, conversation_id: str) -> None:
        """Re-arm the idle clock when Runner lifecycle evidence observes work."""
        self._last_busy_at[conversation_id] = time.monotonic()

    def has_managed_panes(self) -> bool:
        """Return whether a retained pane should keep its Runner alive."""
        live = {pane.conversation_id for pane in self._list_native_panes()}
        for conversation_id in list(self._managed_conversations):
            if not self._is_managed(conversation_id, time.monotonic()):
                continue
            if conversation_id not in live:
                self._managed_conversations.pop(conversation_id, None)
        return bool(self._managed_conversations)

    def _pane_for_conversation(self, conversation_id: str) -> PaneRef | None:
        return next(
            (
                pane
                for pane in self._list_native_panes()
                if pane.conversation_id == conversation_id
            ),
            None,
        )

    async def retention_snapshot(
        self,
        conversation_id: str,
        *,
        idle_threshold_s: float,
    ) -> dict[str, str | float | bool] | None:
        """Describe one managed pane using the same busy evidence as teardown."""
        pane = self._pane_for_conversation(conversation_id)
        if pane is None:
            self._managed_conversations.pop(conversation_id, None)
            return None
        now = time.monotonic()
        busy = await self._is_busy(pane)
        if busy:
            self._last_busy_at[conversation_id] = now
        last_busy = self._last_busy_at.setdefault(conversation_id, now)
        idle_seconds = 0.0 if busy else max(0.0, now - last_busy)
        return {
            "family": pane.terminal_name,
            "busy": busy,
            "eligible": not busy and idle_seconds >= idle_threshold_s,
            "idle_seconds": idle_seconds,
            "activity_token": f"{last_busy:.9f}",
        }

    async def release_if_idle(
        self,
        conversation_id: str,
        *,
        idle_threshold_s: float,
        expected_activity_token: str,
    ) -> str:
        """Release one still-eligible pane selected from a prior snapshot."""
        pane = self._pane_for_conversation(conversation_id)
        if pane is None:
            if self._owes_tail(conversation_id):
                return "failed"
            # No pane to tear down, but the management and clock entries
            # still retire: a pane that vanished between selection and
            # release must read gone, not retained, like release_now.
            self._managed_conversations.pop(conversation_id, None)
            self._last_busy_at.pop(conversation_id, None)
            return "absent"
        now = time.monotonic()
        if await self._is_busy(pane):
            self._last_busy_at[conversation_id] = now
            return "busy"
        last_busy = self._last_busy_at.setdefault(conversation_id, now)
        if expected_activity_token != f"{last_busy:.9f}":
            return "stale"
        if now - last_busy < idle_threshold_s:
            return "not_eligible"
        # Close the selection→reap race using the same final busy probe as the
        # legacy scanner. A new turn/output/client re-arms the clock.
        if await self._is_busy(pane):
            self._last_busy_at[conversation_id] = time.monotonic()
            return "busy"
        saved_expiry = (
            self._managed_conversations.get(conversation_id)
            if self._is_managed(conversation_id, now)
            else None
        )
        self._managed_conversations.pop(conversation_id, None)
        try:
            await self._reap(pane)
        except NativePaneStillAlive as exc:
            self._record_pending(pane, exc.instance)
            if saved_expiry is not None:
                self._managed_conversations[conversation_id] = max(
                    saved_expiry, self._managed_conversations.get(conversation_id, saved_expiry)
                )
            return "failed"
        except Exception:
            if saved_expiry is not None:
                self._managed_conversations[conversation_id] = max(
                    saved_expiry, self._managed_conversations.get(conversation_id, saved_expiry)
                )
            _logger.exception(
                "native pane retention release failed for conversation %s",
                conversation_id,
            )
            return "failed"
        self._last_busy_at.pop(conversation_id, None)
        return "released"

    async def release_now(self, conversation_id: str) -> str:
        """Unconditionally close one pane for an explicit lifecycle action."""
        pane = self._pane_for_conversation(conversation_id)
        if pane is None:
            if self._owes_tail(conversation_id):
                return "failed"
            self._managed_conversations.pop(conversation_id, None)
            self._last_busy_at.pop(conversation_id, None)
            return "absent"
        saved_expiry = (
            self._managed_conversations.get(conversation_id)
            if self._is_managed(conversation_id, time.monotonic())
            else None
        )
        self._managed_conversations.pop(conversation_id, None)
        try:
            await self._reap(pane)
        except NativePaneStillAlive as exc:
            self._record_pending(pane, exc.instance)
            if saved_expiry is not None:
                self._managed_conversations[conversation_id] = max(
                    saved_expiry, self._managed_conversations.get(conversation_id, saved_expiry)
                )
            return "failed"
        except Exception:
            if saved_expiry is not None:
                self._managed_conversations[conversation_id] = max(
                    saved_expiry, self._managed_conversations.get(conversation_id, saved_expiry)
                )
            _logger.exception(
                "native pane explicit release failed for conversation %s",
                conversation_id,
            )
            return "failed"
        self._last_busy_at.pop(conversation_id, None)
        return "released"

    async def start(self) -> None:
        """Spawn the reaper loop (idempotent)."""
        if self._started:
            return
        self._started = True
        self._task = asyncio.create_task(self._reap_loop(), name="native-pane-idle-reaper")
        _logger.info(
            "native pane reaper started (idle_timeout=%ss, interval=%ss%s)",
            self._idle_timeout_s,
            self._reaper_interval_s,
            "; DISABLED" if self._idle_timeout_s <= 0 else "",
        )

    async def shutdown(self) -> None:
        """Cancel the reaper loop."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._started = False

    def _classify(self, now: float, panes: list[PaneRef], busy_convs: set[str]) -> list[PaneRef]:
        """Pure idle-clock decision: which panes are reapable right now.

        Given the set of conversation ids observed busy this scan, maintain the
        per-conversation idle clock and return the panes idle for at least
        ``idle_timeout_s``. A busy pane re-arms its clock; a newly-observed idle
        pane gets one full window of grace before it is eligible. No I/O, so it is
        unit-testable with an injected ``now`` and ``busy_convs``.
        """
        live: set[str] = set()
        reapable: list[PaneRef] = []
        for pane in panes:
            conv = pane.conversation_id
            live.add(conv)
            if conv in busy_convs:
                self._last_busy_at[conv] = now
                continue
            last = self._last_busy_at.get(conv)
            if last is None:
                self._last_busy_at[conv] = now
                continue
            if now - last >= self._idle_timeout_s:
                reapable.append(pane)
        # Forget conversations whose pane is gone so the clock map can't grow.
        for gone in self._last_busy_at.keys() - live:
            del self._last_busy_at[gone]
        return reapable

    async def _reap_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._reaper_interval_s)
            except asyncio.CancelledError:
                return
            try:
                await self._retire_pending()
            except Exception:
                _logger.exception("native pane reaper: pending retirement failed")
            # ``<= 0`` disables legacy idle reaping.
            if self._idle_timeout_s <= 0:
                continue
            try:
                await self._scan_once()
            except Exception:  # never let a scan error kill the loop
                _logger.exception("native pane reaper: scan failed")

    async def _scan_once(self) -> None:
        panes = self._list_native_panes()
        now = time.monotonic()
        busy_convs = {p.conversation_id for p in panes if await self._is_busy(p)}
        for pane in self._classify(now, panes, busy_convs):
            if self._is_managed(pane.conversation_id, time.monotonic()):
                continue
            # Re-check immediately before teardown: selection happened above with
            # possibly-stale signals, and a turn / client / autonomous run may
            # have started since (the select→reap race). Re-arm and skip if so.
            if await self._is_busy(pane):
                self._last_busy_at[pane.conversation_id] = time.monotonic()
                continue
            if self._is_managed(pane.conversation_id, time.monotonic()):
                continue
            _logger.info(
                "reaping idle native pane for conversation %s (%s; idle > %.0fs)",
                pane.conversation_id,
                pane.terminal_name,
                self._idle_timeout_s,
            )
            # Drop the clock entry up front: a reap failure then re-arms the grace
            # window next scan instead of permanently skipping the conversation.
            self._last_busy_at.pop(pane.conversation_id, None)
            try:
                await self._reap(pane)
            except NativePaneStillAlive as exc:
                self._record_pending(pane, exc.instance)
            except Exception:
                _logger.exception(
                    "native pane reaper: reap failed for conversation %s", pane.conversation_id
                )

    async def _retire_pending(self) -> None:
        listed = self._list_native_panes()
        for key, record in list(self._pending_retire.items()):
            if any(pane.instance is record.instance for pane in listed):
                self._pending_retire.pop(key, None)
                continue
            probe = await record.instance.probe_alive()
            if probe is not False:
                if time.monotonic() - record.first_seen < PENDING_RETIRE_MAX_AGE_S:
                    continue
                try:
                    await record.instance.kill_server()
                    probe = await record.instance.probe_alive()
                except Exception:
                    probe = None
                if probe is not False:
                    if not record.warned:
                        _logger.warning(
                            "native pane retirement could not confirm death for conversation %s",
                            record.conversation_id,
                        )
                        record.warned = True
                    continue
            try:
                await asyncio.wait_for(
                    record.instance.close(), timeout=PENDING_RETIRE_CLOSE_TIMEOUT_S
                )
            except Exception:
                if not record.cleanup_warned:
                    _logger.exception(
                        "native pane retirement cleanup failed for %s", record.conversation_id
                    )
                    record.cleanup_warned = True
            if record.tail_token is None or self._finish_retired is None:
                self._pending_retire.pop(key, None)
                continue
            new_token = await self._finish_retired(
                record.conversation_id, record.terminal_name, record.tail_token
            )
            if new_token is None:
                self._pending_retire.pop(key, None)
            else:
                record.tail_token = new_token
