"""Conversation store — manages conversations and their items."""

import hashlib
import math
import time
import unicodedata
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any, Literal, TypedDict

from omnigent.entities import (
    Agent,
    Conversation,
    ConversationItem,
    NewConversationItem,
    PagedList,
)
from omnigent.session_import import IMPORT_PROVENANCE_LABEL_KEYS

if TYPE_CHECKING:
    from omnigent.db.account_authority import AccountAuthority

# Label set on a fork of a session that had a working directory, or a
# runner-bound native session whose working-directory metadata was lost.
# Its value is the source session id. Presence marks the (unbound) clone
# as needing a host + working directory before it can run, so the
# online-dot reports it offline until bound and the UI opens the directory
# picker instead of silently dropping the first message. Forks of
# chat-only sources get no label and resume in-process like a brand-new
# chat session. Canonical home is the store layer; the server route and
# the SQLAlchemy store both import it.
FORK_SOURCE_LABEL_KEY = "omnigent.fork.source_id"

# One-shot fork directive: the SOURCE session's runtime-native session id
# (e.g. the source claude-native Claude Code session uuid), stamped on the
# clone at fork time when the source had one. A native harness launching
# the (still-unbound) clone uses it to locate the source's local transcript
# and clone it into the clone's OWN project dir under a freshly assigned
# uuid (rewriting sessionId/cwd), then launch plain ``--resume <our_uuid>``
# (see ``omnigent.harnesses.claude_native.main._clone_claude_transcript`` and the
# fork-resume branch in ``omnigent.runner.app``), so the clone opens with
# the prior history instead of a blank session. Once the clone captures its
# OWN native session id (``external_session_id`` set on first launch), this
# directive is inert — the launch path only consults it while
# ``external_session_id`` is still NULL. Cleared/ignored thereafter.
FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY = "omnigent.fork.source_external_session_id"

# Fork directive: set when the fork binds a NATIVE target harness
# (claude-native / codex-native) whose history should carry over. A native
# CLI ignores the Omnigent transcript, so the runner rebuilds the target's
# on-disk transcript before launch. Two rebuild paths share this directive:
# when the source was a SAME-FAMILY native session its captured
# ``external_session_id`` is also stamped (see
# FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY) and the runner clones that
# transcript; otherwise (an SDK or cross-family source) the runner builds
# the native transcript from the fork's copied Omnigent items
# (``_ensure_local_claude_resume_transcript`` /
# ``_ensure_local_codex_resume_rollout`` — the converters consume Omnigent's
# normalized item shape, so the source harness doesn't matter). Set by the
# route whenever the target is native. Inert once the clone captures its
# own native session id (the launch path consults it only while
# ``external_session_id`` is NULL).
FORK_CARRY_HISTORY_LABEL_KEY = "omnigent.fork.carry_history"

# Opt-in DANGEROUS launch directive for a codex-native session: when set to
# ``"1"`` the runner launches Codex with
# ``--dangerously-bypass-approvals-and-sandbox`` and puts the app-server
# threads into the matching no-approval / no-sandbox stance (see
# ``omnigent.runner.app._codex_native_launch_config`` and
# ``codex_native_app_server.build_codex_remote_args``). Stored as a plain
# conversation label (cheap thread metadata, like the fork directives above)
# so it survives reload without a schema migration. The web UI gates turning
# this on behind a typed confirmation + a persistent red warning banner; any
# value other than ``"1"`` (incl. absent) leaves the session in Codex's
# normal approval/sandbox stance.
CODEX_NATIVE_BYPASS_SANDBOX_LABEL_KEY = "omnigent.codex_native.bypass_sandbox"

# Reserved label key that stores a session's sidebar "project" membership
# (implicit collections — a project exists while ≥1 session carries this key).
# Namespaced so it never collides with the user-facing "project" term or other
# reserved keys, and is filtered out of generic label surfaces. Canonical home
# is the store layer; the SQLAlchemy store and the server route both import it,
# and the web client mirrors the literal as ``PROJECT_LABEL_KEY``.
PROJECT_LABEL_KEY = "omni_project"
ARCHIVE_LOCK_LABEL_KEY = "omnigent.archive_locked"
DELETION_CLAIM_STALE_AFTER_S = 15 * 60
DELETION_CLAIM_HEARTBEAT_INTERVAL_S = 60
ARCHIVE_CLOSE_CLAIM_STALE_AFTER_S = 15 * 60

DeletionClaimResult = Literal["claimed", "locked", "busy", "not_found"]
ArchiveLockWriteResult = Literal["updated", "busy", "not_found"]
ArchiveCloseClaimResult = Literal["claimed", "stale", "busy", "not_found"]
NativeSubagentReconcileWriteResult = Literal["corrected", "stale", "unsupported"]


@dataclass(frozen=True)
class NativeSubagentReconcileFingerprint:
    """Frozen server state used to guard one native sub-agent repair.

    The reconciliation endpoint obtains external terminal evidence from the
    already-running native runner. That round trip must not be allowed to
    overwrite a newer relay edge, transcript item, or failure detail that
    arrived while it was in flight. The store therefore compares this full
    fingerprint again inside the same transaction that applies the repair.

    ``label_states`` carries ``(key, value, updated_at)`` triples. Missing
    labels are represented by ``(key, None, None)`` so an insertion during the
    probe is observable too.
    """

    conversation_id: str
    parent_conversation_id: str | None
    runner_id: str | None
    host_id: str | None
    external_session_id: str | None
    live_status: str | None
    latest_item_id: str | None
    label_states: tuple[tuple[str, str | None, int | None], ...]


class DailyCostState(TypedDict):
    """Daily cost state record returned by list_daily_cost_states.

    :param cost_usd: Cumulative spend for this user on this day.
    :param ask_approved_usd: Highest soft-limit checkpoint the user approved.
    :param day_utc: The UTC day as "YYYY-MM-DD".
    :param user_id: The user this record belongs to.
    """

    cost_usd: float
    ask_approved_usd: float
    day_utc: str
    user_id: str


# Reserved label-key PREFIX that records whether a session is "pinned" in the
# sidebar. Pins are PER-USER: the stored key is ``omnigent.pinned.<user_id>``
# (see :func:`pinned_label_key`), so pinning a session shared with others does
# not pin it for them, and either party can pin/unpin independently — matching
# the prior per-user localStorage behaviour. The value is the epoch-ms pin time
# (any non-empty value means pinned; the row is deleted on unpin), which lets
# the sidebar order the Pinned section by pin recency (stable under a new
# message bumping ``updated_at``) and keeps the Cmd+1..0 hotkey slots consistent
# across a user's devices. Server-side persistence lets a pin follow the user
# across devices.
#
# The bare ``omnigent.pinned`` (no user suffix) is the CANONICAL key the web
# client reads/writes; the server rewrites it to the caller's per-user key on
# write and collapses the caller's per-user key back to it on read (see
# ``_build_session_list_item``), so the per-user dimension never crosses the
# API boundary — a viewer never sees another user's pin key. The web client
# mirrors the canonical key as ``PINNED_LABEL_KEY``.
PINNED_LABEL_KEY = "omnigent.pinned"

# Marks a top-level fork created as a side chat. A side chat surfaces only as a
# Workspace-rail tab, so a conversation carrying this label is hidden from the
# left sidebar (the ``GET /v1/sessions`` list filters it out). The fork
# keeps its own transcript and may share its parent's runner.
SIDE_CHAT_LABEL_KEY = "omnigent.side_chat"

# Server-owned routing ancestry; it does not require a workspace or own a runner.
SIDE_CHAT_SOURCE_LABEL_KEY = "omnigent.side_chat.source_id"

# Single-user / no-auth sentinel for per-user label-key suffixes, mirroring the
# reserved ``"local"`` identity used elsewhere (see ``RESERVED_USER_LOCAL``).
_PER_USER_LABEL_LOCAL_USER = "local"


def _per_user_label_suffix(user_id: str | None, prefix: str) -> str:
    """
    The user suffix for a per-user label key, safe for the key column width.

    ``conversation_labels.key`` is ``String(128)``. User ids are
    ``String(128)`` elsewhere (SSO subject ids can be long), so a raw suffix
    could overflow the key column — Postgres errors, MySQL silently truncates
    (and two long ids could then collide on the truncated key). Normal ids are
    used verbatim for DB readability; an id that does not fit under *prefix*
    is replaced with a fixed-width hash suffix.

    :param user_id: Authenticated user id, e.g. ``"alice@example.com"``, or
        ``None`` in single-user / no-auth mode (→ the ``local`` sentinel).
    :param prefix: The bare label key the suffix is appended to, e.g.
        ``"omnigent.pinned"``.
    :returns: The suffix (the id, or its hash when the id is too long).
    """
    suffix = user_id if user_id is not None else _PER_USER_LABEL_LOCAL_USER
    if len(suffix) > 128 - len(prefix) - 1:  # minus the "." joiner
        # 64 hex chars — well within the budget and collision-safe.
        suffix = "h:" + hashlib.sha256(suffix.encode("utf-8")).hexdigest()
    return suffix


def pinned_label_key(user_id: str | None) -> str:
    """
    The per-user pinned-label key for ``user_id``.

    Deterministic in ``user_id`` (the write path and the ``pinned=True`` filter
    derive the key the same way, so they always match). Normal ids are used
    verbatim for DB readability; an id too long to fit the ``String(128)`` key
    column is replaced with a fixed-width ``sha256`` suffix so it can never
    overflow or collide via silent truncation.

    :param user_id: Authenticated user id, e.g. ``"alice@example.com"``, or
        ``None`` in single-user / no-auth mode (→ the ``local`` sentinel).
    :returns: ``"omnigent.pinned.<suffix>"`` (suffix = the id, or its hash when
        the id is too long).
    """
    return f"{PINNED_LABEL_KEY}.{_per_user_label_suffix(user_id, PINNED_LABEL_KEY)}"


# Reserved label-key PREFIX recording when a user last interacted with a
# session — the "Recent sessions" sidebar section's order. Like pins it is
# PER-USER (``omnigent.touched.<user_id>``, same suffix rule), and the value is
# the interaction time in epoch milliseconds, zero-padded to 13 digits so
# string order is time order. The server writes it on the session's ROOT for
# human-origin user messages, approvals and elicitation resolves; it never
# leaves the server (``drop_server_secret_labels``) and a fork never copies it.
TOUCHED_LABEL_KEY = "omnigent.touched"


def touched_label_key(user_id: str | None) -> str:
    """
    The per-user touched-label key for ``user_id``.

    Deterministic in ``user_id`` (the write path and the recent-sessions
    filter derive the key the same way, so they always match). Uses the same
    suffix rule as :func:`pinned_label_key`: normal ids verbatim, an id too
    long to fit the ``String(128)`` key column replaced with a fixed-width
    ``sha256`` suffix.

    :param user_id: Authenticated user id, e.g. ``"alice@example.com"``, or
        ``None`` in single-user / no-auth mode (→ the ``local`` sentinel).
    :returns: ``"omnigent.touched.<suffix>"``.
    """
    return f"{TOUCHED_LABEL_KEY}.{_per_user_label_suffix(user_id, TOUCHED_LABEL_KEY)}"


# Epoch-SECONDS time a session was archived, written on archive and deleted on
# unarchive. A label rather than a ``conversations`` column, so ageing out old
# archived sessions needs no schema migration; readers fall back to
# ``updated_at`` when it is absent. Seconds, not the pin key's epoch-ms, to
# match that fallback's unit.
ARCHIVED_AT_LABEL_KEY = "omnigent.archived_at"

# Server-reserved label whose value is the archive revision whose teardown
# waits for the whole session tree to go idle before it is torn down. It
# matches only its own revision, so unarchive or re-archive voids it.
ARCHIVE_STOP_WHEN_IDLE_LABEL_KEY = "omnigent.archive_stop_when_idle"

# Server-reserved label whose value is the archive revision whose teardown
# should also remove the root's server-created worktree. Keyed to the
# revision so unarchive or re-archive voids it.
ARCHIVE_DELETE_WORKTREE_LABEL_KEY = "omnigent.archive_delete_worktree"
ARCHIVE_KEEP_WORKTREE_LABEL_KEY = "omnigent.archive_keep_worktree"
ARCHIVE_REMOVED_WORKTREE_LABEL_KEY = "omnigent.archive_removed_worktree"
ARCHIVE_WORKTREE_ADMISSION_FENCE_LABEL_KEY = "omnigent.archive_worktree_admission_fence"


def worktree_admission_fingerprint(host_id: str, path: str) -> str:
    """Hash a host/root binding without placing its path in a label."""
    parsed = PureWindowsPath(path) if PureWindowsPath(path).is_absolute() else PurePosixPath(path)
    normalized = (
        str(parsed).replace("\\", "/").lower()
        if isinstance(parsed, PureWindowsPath)
        else str(parsed)
    )
    return hashlib.sha256(f"{host_id}\0{normalized.rstrip('/')}".encode()).hexdigest()


def worktree_admission_ancestor_fingerprints(host_id: str, path: str) -> list[str]:
    """Return removal-fence fingerprints for a path and its parents."""
    parsed = PureWindowsPath(path) if PureWindowsPath(path).is_absolute() else PurePosixPath(path)
    return [
        worktree_admission_fingerprint(host_id, str(ancestor))
        for ancestor in (parsed, *parsed.parents)
    ]


# New-session id an archived session was continued into (``POST
# /v1/sessions/{sid}/continue``). The archived row keeps the pointer so a
# repeat continue returns the same session instead of minting another one.
CONTINUED_TO_LABEL_KEY = "omnigent.continued_to"

# Succession operation labels. The move transaction writes ``succeeded_by``
# (old → new) and ``succeeds`` (new → old); ``handover_item`` and
# ``rotate_requested`` are per-rotation facts a session records for itself.
# They describe ONE operation, but rotation clients copy the old session's
# labels wholesale into every replacement, so the create route strips them
# from any create body instead of letting a copy read as a fresh fact.
SUCCEEDED_BY_LABEL_KEY = "omnigent.succeeded_by"
SUCCEEDS_LABEL_KEY = "omnigent.succeeds"
HANDOVER_ITEM_LABEL_KEY = "omnigent.handover_item"
ROTATE_REQUESTED_LABEL_KEY = "omnigent.rotate_requested"
SUCCESSION_OPERATION_LABEL_KEYS = frozenset(
    {
        SUCCEEDED_BY_LABEL_KEY,
        SUCCEEDS_LABEL_KEY,
        HANDOVER_ITEM_LABEL_KEY,
        ROTATE_REQUESTED_LABEL_KEY,
    }
)

# Per-session secret keying artifact-link tokens; server-internal (never
# client-settable, never shown to viewers). Deleting or rotating it revokes
# every link of the session.
ARTIFACT_LINK_KEY_LABEL = "omnigent.artifact_link_key"


def is_artifact_link_key(key: str) -> bool:
    """
    Return whether ``key`` is the server-reserved artifact-link key.

    :param key: A label key to test.
    :returns: ``True`` for :data:`ARTIFACT_LINK_KEY_LABEL` and every
        collation look-alike.
    """
    return _may_collate_to(key, ARTIFACT_LINK_KEY_LABEL, prefix=False)


def _may_collate_to(key: str, target: str, *, prefix: bool) -> bool:
    # MySQL's accent-insensitive collation equates characters a skeleton
    # cannot list. Over-approximating only refuses a look-alike key on a
    # false positive.
    positions = {0}
    for ch in unicodedata.normalize("NFKD", key.strip()).casefold():
        if prefix and len(target) in positions:
            return True
        if ch.isascii() and ch.isprintable():
            positions = {pos + 1 for pos in positions if pos < len(target) and target[pos] == ch}
        else:
            positions = {
                pos + advance
                for pos in positions
                for advance in range(4)
                if pos + advance <= len(target)
            }
        if not positions:
            return False
    return len(target) in positions


def is_touched_label_key(key: str) -> bool:
    """
    Return whether ``key`` belongs to the per-user ``omnigent.touched`` family.

    :param key: A label key to test.
    :returns: ``True`` for the bare prefix, every ``omnigent.touched.<user>``
        key, and every collation look-alike.
    """
    return _may_collate_to(key, TOUCHED_LABEL_KEY, prefix=False) or _may_collate_to(
        key, TOUCHED_LABEL_KEY + ".", prefix=True
    )


def is_server_secret_label_key(key: str) -> bool:
    """
    Return whether ``key`` is a server-owned label that must not leave the server.

    The reserved set is the artifact-link secret plus the per-user
    ``omnigent.touched.<user>`` interaction times (bare prefix and suffixed).

    :param key: A label key to test.
    :returns: ``True`` for every server-secret key.
    """
    return is_artifact_link_key(key) or is_touched_label_key(key)


def drop_server_secret_labels(labels: dict[str, str]) -> dict[str, str]:
    """
    Remove server-secret label keys from a label map headed elsewhere.

    ``ARTIFACT_LINK_KEY_LABEL`` is the per-session secret that signs artifact
    capability URLs: any holder of the value can forge links for the session,
    so it must never leave the server — not in a session payload, a label
    response, a runner init snapshot, or a policy's label view. Per-user
    ``omnigent.touched.<user>`` interaction times are private the same way.

    :param labels: The stored conversation labels.
    :returns: A copy with every server-secret key removed.
    """
    return {key: value for key, value in labels.items() if not is_server_secret_label_key(key)}


# Labels that must NOT cross into a new session context — deliberately
# dropped both when forking (not copied to the clone) and on an in-place
# agent switch (deleted from the switched session). Two distinct reasons
# put a key here:
#
#   * Runtime state bound to ONE running instance — the native bridge-id
#     labels would route the new context's terminal + web injection to the
#     SOURCE's claude/codex bridge (whose active-session marker isn't the
#     clone → "session no longer active"); the context-size metrics would
#     display the source's last usage. The bridge-id literals mirror the
#     harness modules' ``*_BRIDGE_ID_LABEL_KEY`` constants; a store test
#     cross-checks them so a rename in those modules fails loudly here.
#
#   * Per-context safety opt-in — the DANGEROUS codex full-bypass directive
#     (:data:`CODEX_NATIVE_BYPASS_SANDBOX_LABEL_KEY`). Letting it ride into a
#     fork (a new session + workspace) or survive an agent switch would
#     silently re-arm ``--dangerously-bypass-approvals-and-sandbox`` with no
#     typed re-confirmation and no banner, violating the "impossible to
#     enable accidentally" contract (#657). Dropping it forces each session
#     that runs bypass to make its own explicit opt-in.
#
#   * Per-session secret — :data:`ARTIFACT_LINK_KEY_LABEL`. Inheriting it
#     would keep the source's links valid against a new session context whose
#     viewers never held them; the clone mints its own key on first use.
_INSTANCE_SCOPED_LABEL_KEYS = frozenset(
    {
        "omnigent.claude_native.bridge_id",
        "omnigent.codex_native.bridge_id",
        "omnigent.last_auto_compact_token_limit",
        "omnigent.last_context_tokens",
        "omnigent.last_context_window",
        "omnigent.goal_state",
        "omnigent.last_provider_usage_limits",
        CODEX_NATIVE_BYPASS_SANDBOX_LABEL_KEY,
        ARTIFACT_LINK_KEY_LABEL,
    }
)

# Source identity belongs only to the original imported session, and a fork is
# born unarchived so it must not inherit its parent's archive time. The sandbox
# repository records what THIS session's sandbox was built from and a relaunch
# re-clones from it, so a fork that asked for an empty sandbox would otherwise
# have the source's repo re-cloned into it on the first relaunch; the fork's own
# managed launch re-stamps whatever repository it resolves. Unlike runtime
# instance labels, these survive an in-place agent switch but never a fork.
#
# Recorded one label PER repo (``omnigent.sandbox.repo.<index>``), a
# dynamic-suffix family like the per-user pins, so a fork drops the whole family
# by prefix in ``fork_conversation`` (this bare base key still covers any legacy
# single-label session). The literal mirrors the server's
# ``MANAGED_REPO_LABEL_KEY``; a store test cross-checks it so a rename there
# fails loudly here.
_SANDBOX_REPO_LABEL_KEY = "omnigent.sandbox.repo"
_FORK_ONLY_DROPPED_LABEL_KEYS = IMPORT_PROVENANCE_LABEL_KEYS | {
    ARCHIVED_AT_LABEL_KEY,
    SIDE_CHAT_LABEL_KEY,
    SIDE_CHAT_SOURCE_LABEL_KEY,
    _SANDBOX_REPO_LABEL_KEY,
}


@dataclass(frozen=True)
class CreatedSession:
    """
    Result of atomic session and agent creation.

    :param conversation: Newly created session/conversation row.
    :param agent: Newly created session-scoped agent row with
        ``session_id`` pointing at ``conversation.id``.
    """

    conversation: Conversation
    agent: Agent


@dataclass(frozen=True)
class ConversationUpdateResult:
    """Result of updating a conversation and its requested model settings.

    :param conversation: The conversation after the update has been persisted.
    :param reasoning_effort_changed: Whether a requested reasoning-effort
        value changed from the value on the locked AP row.
    :param model_override_changed: Whether a requested model override changed
        from the value on the locked AP row.
    """

    conversation: Conversation
    reasoning_effort_changed: bool
    model_override_changed: bool


@dataclass(frozen=True)
class SessionConnectivity:
    """
    The minimal session fields the sidebar's online-dot needs.

    Returned by :meth:`ConversationStore.get_session_connectivity` so
    the ``/health`` batch path can decide reachability without an N+1
    fan-out of :meth:`get_conversation` (each of which also fetches
    labels). One row per existing conversation.

    :param runner_id: Runner the session is pinned to, or ``None``
        when no runner has claimed it yet (in-process executor),
        e.g. ``"runner_token_abc123"``.
    :param host_id: Host the session is bound to, or ``None`` for
        CLI-launched / runner-only sessions, e.g. ``"host_abc123"``.
    :param needs_workspace: ``True`` when this is an unbound fork of a
        session that had a working directory (the
        ``omnigent.fork.source_id`` label is set). Forces the online
        dot off while ``runner_id``/``host_id`` are still ``None`` so
        the UI prompts for a host + directory before the clone can run,
        rather than treating it as an in-process session.
    :param imported: ``True`` when this session was imported from a local
        harness transcript (the ``omnigent.import.source`` label is set).
        Like ``needs_workspace``, forces the online dot off while unbound:
        an imported transcript has no live executor anywhere, so it must
        launch a runner on a host before it can run — reporting it offline
        routes the first message into the resume picker.
    :param runner_last_seen: Epoch seconds the bound runner's tunnel was
        last observed alive, written by the replica holding the tunnel.
        ``None`` when never observed (or cleared on graceful disconnect).
        Lets a replica that does NOT hold the tunnel derive
        ``runner_online`` from freshness (see
        :func:`runner_seen_is_fresh`) instead of its own empty registry.
    """

    runner_id: str | None
    host_id: str | None
    needs_workspace: bool
    imported: bool = False
    runner_last_seen: int | None = None


@dataclass(frozen=True)
class ArchivedConversationFacets:
    """Distinct filter values for the caller-visible archived session set."""

    projects: list[str]
    host_ids: list[str]
    agent_ids: list[str]


@dataclass(frozen=True)
class SessionSuccession:
    """One succession receipt with its JSON payloads decoded.

    :param direct_ids: Direct children of the old session moved.
    :param moved_ids: Every session moved — ``direct_ids`` plus all their
        descendants.
    :param opening: Prepared opening payload, or ``None`` before it is built.
    :param opening_item_id: Id of the posted opening item, or ``None``.
    :param dropped: Old session's runtime work that could not move, or ``None``.
    :param questions: Open questions snapshotted before the old card closed,
        or ``None``.
    """

    id: str
    old_id: str
    new_id: str
    phase: str
    direct_ids: list[str]
    moved_ids: list[str]
    opening: dict[str, Any] | None
    opening_item_id: str | None
    dropped: list[dict[str, Any]] | None
    questions: list[dict[str, Any]] | None
    error: str | None
    created_at: int
    updated_at: int


@dataclass(frozen=True)
class DetachedCard:
    """One detached card's durable record with its JSON payloads decoded.

    :param kind: ``"approval"`` or ``"question"``.
    :param state: ``"pending"``, ``"answered"`` or ``"settled"``.
    :param mirror: Whether the card is mirrored into ancestor streams.
    :param params: The published ``ElicitationRequestParams`` dump.
    :param payload: What rebuilds the card's verdict text, e.g.
        ``{"aid": "a1b2c3", "action": "Bash(...)"}``.
    :param grant_key: Match key of the re-issued call (approvals only).
    :param verdict: The ``ElicitationResult`` dump once answered.
    :param delivery_text: The ``[System: …]`` message owed to the agent.
    :param expires_at: Epoch seconds the card (or its grant) lapses.
    """

    elicitation_id: str
    session_id: str
    kind: str
    state: str
    mirror: bool
    params: dict[str, Any]
    payload: dict[str, Any]
    grant_key: list[str] | None
    verdict: dict[str, Any] | None
    delivery_text: str | None
    created_at: int
    expires_at: int


# Freshness window for ``omnigent_conversation_metadata.runner_last_seen``. The tunnel
# replica refreshes live runners every ~30s (the tunnel ping interval),
# so 3 missed refreshes = offline — the same budget the tunnel's own
# keepalive uses and the same shape as ``host_store.HOST_LIVENESS_TTL_S``.
# Level-triggered on purpose: if the runner, its host, or the server
# replica holding the tunnel dies without a graceful disconnect, the
# stale value self-corrects after this window.
RUNNER_LIVENESS_TTL_S = 90


def runner_seen_is_fresh(last_seen: int | None, now: int | None = None) -> bool:
    """
    Return whether a ``runner_last_seen`` stamp is within the liveness TTL.

    :param last_seen: Epoch seconds from ``SessionConnectivity``, or
        ``None`` when the runner was never observed / was cleared.
    :param now: Epoch seconds to measure against; defaults to the
        current time. Pass an explicit value to classify many rows
        against one consistent clock.
    :returns: ``True`` when the stamp exists and is fresh.
    """
    if last_seen is None:
        return False
    ref = now if now is not None else int(time.time())
    return last_seen >= ref - RUNNER_LIVENESS_TTL_S


class NativeReplayConflictError(Exception):
    """A native transcript does not match the next persisted historical item."""


class NativeRecoveryItemSkipped(Exception):
    """Old history omitted this reasoning item; no row or cursor was changed."""


class ConversationNotFoundError(Exception):
    """
    Raised when a required conversation row is missing.

    Store methods use this when absence is not a benign
    no-op and the route layer must return a typed 404.
    """


class ConversationAlreadyExistsError(Exception):
    """Raised when a caller-supplied conversation id is already in use."""


class ConversationArchiveClosingError(Exception):
    """Raised when a child create loses to its root archive-close transition."""


class NameAlreadyExistsError(Exception):
    """
    Raised by ``create_conversation`` when the requested
    ``(parent_conversation_id, title)`` pair already exists.

    Phase 4: the conversations table has a partial unique index
    that enforces sub-agent name uniqueness within a parent.
    SqlAlchemy's ``IntegrityError`` is translated to this exception
    so callers (the ``sys_session_send`` and ``sys_session_send``
    builtins) can surface a clean ``name_already_exists`` tool
    error to the LLM.
    """


class SuccessionRefusedError(ValueError):
    """A child move was refused before any state changed.

    ``code`` names the refusal — ``same_session``, ``not_found``,
    ``not_top_level``, ``target_archived`` or ``title_clash`` — so the
    caller can map it without parsing the message.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _is_addable_usage_increment(value: Any) -> bool:
    """
    Return whether *value* is a safe additive ``session_usage`` increment.

    Every production caller (the relay ``_accumulate_session_usage`` path)
    only ever adds a finite, non-negative count or cost. A negative or
    non-finite increment is therefore corruption — applied additively it
    would drive a cumulative counter backwards or poison it with ``NaN`` /
    ``inf``, and the relay cost-budget gate reads these very totals — so
    :func:`apply_session_usage_delta` drops it rather than merging it.

    :param value: A flat or ``by_model`` sub-key increment from a delta.
    :returns: ``True`` when *value* is a finite, non-negative, non-bool
        number; ``False`` otherwise.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value) and value >= 0


def apply_session_usage_delta(current: dict[str, Any], delta: dict[str, Any]) -> None:
    """
    Apply a usage *delta* to *current* in place (add semantics, nested-aware).

    Flat numeric keys are summed; ``"by_model"`` sub-dicts are merged by
    model id, summing each model's sub-keys independently. Used by
    :meth:`ConversationStore.increment_session_usage` implementations to
    keep the merge logic in one place.

    Negative and non-finite increments are dropped (see
    :func:`_is_addable_usage_increment`): they can only come from a forged
    runner usage frame and would corrupt the cumulative totals the relay
    cost-budget gate enforces on.

    :param current: Existing ``session_usage`` dict (mutated in place).
    :param delta: Increments to apply (same layout as ``session_usage``).
    """
    for key, value in delta.items():
        if key == "by_model":
            by_model = current.setdefault("by_model", {})
            for model_id, model_delta in value.items():
                bucket = by_model.setdefault(model_id, {})
                for sub_key, sub_value in model_delta.items():
                    if not _is_addable_usage_increment(sub_value):
                        continue
                    bucket[sub_key] = bucket.get(sub_key, 0) + sub_value
        elif _is_addable_usage_increment(value):
            current[key] = current.get(key, 0) + value


class ConversationStore(ABC):
    """
    Abstract base for conversation persistence.

    Manages conversations and their items: creation, lookup,
    paginated listing, appending items, full-text search,
    updates, and deletion.
    """

    def __init__(
        self, storage_location: str, conversation_storage_location: str | None = None
    ) -> None:
        """
        Initialize the conversation store.

        :param storage_location: Backend-specific storage URI for the
            Omnigent operational DB, e.g. ``"sqlite:///conversations.db"``.
        :param conversation_storage_location: Optional URI for the Agent Platform DB.
            When ``None`` (default), the AP tables live in the same DB as
            the Omnigent tables.
        """
        self.storage_location = storage_location
        self.conversation_storage_location = conversation_storage_location

    @abstractmethod
    def create_conversation(
        self,
        kind: str = "default",
        title: str | None = None,
        parent_conversation_id: str | None = None,
        agent_id: str | None = None,
        runner_id: str | None = None,
        sub_agent_name: str | None = None,
        host_id: str | None = None,
        workspace: str | None = None,
        git_branch: str | None = None,
        worktree: str | None = None,
        terminal_launch_args: list[str] | None = None,
        conversation_id: str | None = None,
        project_id: str | None = None,
        inference_snapshot: dict[str, Any] | None = None,
        labels: dict[str, str] | None = None,
        reasoning_effort: str | None = None,
        model_override: str | None = None,
        cost_control_mode_override: str | None = None,
        subagent_routing_override: str | None = None,
        harness_override: str | None = None,
    ) -> Conversation:
        """
        Create a new conversation. Generates a unique
        conversation_id.

        ``root_conversation_id`` is set automatically: for
        top-level conversations (no ``parent_conversation_id``)
        it equals the new ``id``; for child conversations it is
        inherited from the parent's ``root_conversation_id``
        (which itself ultimately resolves to the top-level
        conversation in the spawn tree).

        :param kind: Conversation type. ``"default"`` for
            user-initiated, ``"sub_agent"`` for sub-agent
            execution conversations.
        :param title: Optional title. Phase 4 named sub-agents
            store ``"<type>:<name>"`` so the partial unique index
            can enforce ``(parent_conversation_id, title)``
            uniqueness within a parent.
        :param parent_conversation_id: Phase 4 — for child
            sub-agent conversations, the owning parent's id.
            ``None`` for top-level conversations.
        :param agent_id: Agent to bind at creation time, e.g.
            ``"ag_abc123"``. ``None`` only for legacy rows or
            callers that cannot bind a conversation.
        :param runner_id: Optional runner binding to persist at
            creation time, e.g. ``"runner_abc123"``. Used when
            creating child sub-agent conversations so they inherit
            the parent session's current runner affinity.
        :param sub_agent_name: For sub-agent sessions, the
            sub-agent type name within the parent's spec tree,
            e.g. ``"summarizer"``. ``None`` for top-level.
        :param host_id: Host that should launch the runner for
            this session, e.g. ``"host_a1b2c3d4..."``. ``None``
            for CLI-initiated sessions.
        :param workspace: Absolute path on disk where the runner
            should start, e.g. ``"/Users/corey/universe/src/foo"``.
            Required when ``host_id`` is set (a DB check constraint
            enforces this); optional otherwise. The caller passes
            the canonicalized realpath returned by ``host.stat``;
            this method does no path expansion. When a git worktree
            was created, this is the worktree directory path.
        :param git_branch: Git branch checked out in the session's
            worktree, e.g. ``"feature/login"``. Set only when the
            session was created with a server-created worktree;
            ``None`` otherwise. See designs/SESSION_GIT_WORKTREE.md.
        :param worktree: The session's working tree when it differs from
            ``workspace`` (its launch directory), e.g. a worktree placed
            inside the project entry. ``None`` for sessions whose launch
            directory is their working tree (the common case).
        :param terminal_launch_args: Optional pass-through CLI args
            for a native terminal wrapper (claude / codex), e.g.
            ``["--dangerously-skip-permissions"]``. ``None`` leaves
            the column NULL; a list (including ``[]``) is persisted
            so the runner applies it when it auto-launches the
            terminal.
        :param conversation_id: Optional caller-supplied identifier.
            ``None`` generates a new random id. Reserved for flows that
            require database-enforced idempotency.
        :param labels: Initial conversation labels to persist with the
            conversation.
        :param reasoning_effort: Optional per-session reasoning effort.
        :param model_override: Optional per-session model override.
        :param cost_control_mode_override: Optional per-session cost-control mode.
        :param subagent_routing_override: Optional per-session sub-agent routing mode.
        :param harness_override: Optional per-session harness override.
        :returns: The newly created :class:`Conversation`.
        :raises NameAlreadyExistsError: If
            ``parent_conversation_id`` is not ``None`` and a
            sibling with the same ``title`` already exists
            (Phase 4 partial unique index violation).
        :raises ConversationNotFoundError: If
            ``parent_conversation_id`` is set but the parent
            row does not exist (root id can't be inherited).
        :raises ConversationAlreadyExistsError: If a caller-supplied
            ``conversation_id`` is already in use.
        """
        ...

    @abstractmethod
    def get_conversation(self, conversation_id: str) -> Conversation | None:
        """
        Return the conversation, or ``None`` if it does not exist.

        :param conversation_id: Unique conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: The :class:`Conversation` if found, otherwise
            ``None``.
        """
        ...

    @abstractmethod
    def find_conversation_by_external_session_id(
        self,
        external_session_id: str,
    ) -> Conversation | None:
        """Find an existing conversation wrapping one external (harness) session id.

        Both an imported transcript and a natively-run session record the
        external id, so import dedup resolves against either through this one
        lookup. When several rows share the id, the earliest-created wins.

        :param external_session_id: Source harness session id.
        :returns: The matching conversation, or ``None``.
        """
        ...

    @abstractmethod
    def get_runner_ids(self, conversation_ids: list[str]) -> dict[str, str | None]:
        """
        Return ``conversation_id -> runner_id`` for a batch of sessions.

        Bulk variant for the sidebar runner-online dot path. Missing
        ids are omitted; ids without a bound runner map to ``None``.
        """
        ...

    @abstractmethod
    def get_runner_liveness(self, conversation_id: str) -> tuple[str | None, int | None] | None:
        """Return the bound runner ID and heartbeat from the metadata database.

        Reads neither conversation data nor labels, so an unrelated
        conversation backend outage cannot hide a healthy runner.

        :param conversation_id: Session/conversation ID to look up.
        :returns: ``(runner_id, runner_last_seen)``, or ``None`` if the
            metadata row is missing. Either field may be ``None``.
        """
        ...

    @abstractmethod
    def get_session_connectivity(
        self, conversation_ids: list[str]
    ) -> dict[str, SessionConnectivity]:
        """
        Return connectivity fields for a batch of sessions in one query.

        Powers the sidebar's online-dot batch check (``GET /health``)
        without the N+1 fan-out of calling :meth:`get_conversation`
        per id (each of which also issues a second labels query). One
        ``SELECT`` over the conversations table plus one over the
        connectivity-label rows (the fork-source marker). Missing ids
        are omitted from the result.

        :param conversation_ids: Session/conversation IDs to look up,
            e.g. ``["conv_abc123", "conv_def456"]``. Duplicates are
            tolerated.
        :returns: Mapping ``conversation_id -> SessionConnectivity``.
            Ids with no conversation row are absent (callers treat a
            missing id as reachable, mirroring the single-row path).
        """
        ...

    @abstractmethod
    def get_conversations(self, conversation_ids: list[str]) -> dict[str, Conversation]:
        """
        Fetch a batch of conversations by id in a single round-trip.

        Bulk variant of :meth:`get_conversation` for callers that hold
        a known id set and would otherwise fan out one read per id —
        e.g. the ``WS /v1/sessions/updates`` stream rescanning its
        watch-set every interval. Labels are batched too, so the whole
        call is a small constant number of queries regardless of the id
        count.

        :param conversation_ids: Conversation ids to fetch,
            e.g. ``["conv_abc123", "conv_def456"]``. Duplicates are
            tolerated. Empty input returns an empty map without
            touching the database.
        :returns: Mapping ``{conversation_id: Conversation}``. Ids that
            don't resolve to a row are omitted (the caller decides
            whether a missing id is an error), so the result may be
            smaller than the input.
        """
        ...

    @abstractmethod
    def list_child_conversation_ids_by_parent(
        self,
        parent_conversation_ids: list[str],
    ) -> dict[str, list[str]]:
        """
        Return direct sub-agent child ids grouped by parent conversation.

        Batched counterpart to calling :meth:`list_conversations` with
        ``kind="sub_agent"`` and one ``parent_conversation_id`` at a
        time. Callers use this when they only need child identity (for
        example, rolling live child status into parent session rows) and
        should not pay for full child entities or one query per parent.

        :param parent_conversation_ids: Parent conversation ids to
            inspect, e.g. ``["conv_parent1", "conv_parent2"]``.
            Duplicates are tolerated.
        :returns: Mapping from every unique input parent id to the
            matching direct child ids. Parents with no direct sub-agent
            children, or ids that do not exist, map to an empty list.
        """
        ...

    @abstractmethod
    def get_item(self, conversation_id: str, item_id: str) -> ConversationItem | None:
        """
        Fetch one persisted item by id, or ``None`` when absent.

        A bounded point lookup on the item's key, never a scan. Lets the native
        mirror path recognise a forwarder retry of an item it has already
        persisted before it touches the pending-input queue.

        :param conversation_id: The conversation to look in, e.g. ``"conv_abc123"``.
        :param item_id: The item id, e.g. a source-derived ``stable_id``.
        :returns: The item, or ``None``.
        """
        ...

    @abstractmethod
    def list_items(
        self,
        conversation_id: str,
        limit: int = 100,
        after: str | None = None,
        before: str | None = None,
        order: str = "asc",
        type: str | None = None,
    ) -> PagedList[ConversationItem]:
        """
        Return items in a conversation with cursor-based pagination.

        ``order`` controls the sort direction on ``position``
        (``"asc"`` = chronological, ``"desc"`` = reverse).

        Both ``after`` and ``before`` can be used together to
        select a window. Used by the agent loop
        (``after=last_seen``) to poll for steering items.

        :param conversation_id: Unique conversation identifier,
            e.g. ``"conv_abc123"``.
        :param limit: Maximum number of items to return.
        :param after: Cursor item ID; only return items after
            this item in sort order, e.g. ``"msg_xyz789"``.
        :param before: Cursor item ID; only return items before
            this item in sort order.
        :param order: Sort direction, ``"asc"`` or ``"desc"``.
        :param type: Optional item type filter. When provided, only items
            with this type are returned, e.g. ``"compaction"``. ``None``
            means return all types.
        :returns: A :class:`PagedList` of
            :class:`ConversationItem` objects.
        :raises omnigent.errors.StaleCursorError: If the ``after``/``before``
            item no longer exists in this conversation (e.g. deleted
            between two page fetches) — its position is unknowable, and an
            empty page would be indistinguishable from a completed
            enumeration.
        """
        ...

    @abstractmethod
    def list_latest_message_items_for_conversations(
        self,
        conversation_ids: list[str],
        per_conversation_limit: int = 10,
    ) -> dict[str, list[ConversationItem]]:
        """
        Return newest message items for multiple conversations.

        This is the batched counterpart to calling
        ``list_items(conversation_id, type="message", order="desc")`` once
        per conversation. It preserves newest-first order within each
        conversation and returns at most ``per_conversation_limit`` items per
        conversation.

        :param conversation_ids: Conversation ids to fetch messages for,
            e.g. ``["conv_child1", "conv_child2"]``. Duplicates are tolerated.
        :param per_conversation_limit: Maximum number of message items to
            return per conversation, e.g. ``10``.
        :returns: Mapping ``{conversation_id: [ConversationItem, ...]}``.
            Input ids with no matching messages map to an empty list.
        """
        ...

    @abstractmethod
    def find_idempotent_item(self, conversation_id: str, key: str) -> ConversationItem | None:
        """Find a prior append with this conversation-scoped source key."""
        ...

    @abstractmethod
    def append(
        self,
        conversation_id: str,
        items: list[NewConversationItem],
    ) -> list[ConversationItem]:
        """
        Append items to a conversation. Assigns a globally unique
        ID and timestamp to each item.

        An item carrying ``stable_id`` appends idempotently: its id is the
        stable id, and when an item with that id already exists the stored
        item is returned in its place — flagged ``deduplicated`` — instead
        of inserting a duplicate. The existence check rides the append's
        own transaction, so idempotency costs no extra query.

        :param conversation_id: Unique conversation identifier,
            e.g. ``"conv_abc123"``.
        :param items: List of :class:`NewConversationItem` objects
            to persist.
        :returns: The persisted :class:`ConversationItem` list
            with store-assigned IDs and timestamps.
        :raises NativeRecoveryItemSkipped: One historical reasoning item was
            omitted by the old projection; no row or recovery cursor changed.
        :raises NativeReplayConflictError: Native history or its source order
            differs from the stored prefix.
        """
        ...

    @abstractmethod
    def list_conversations(
        self,
        limit: int = 20,
        after: str | None = None,
        before: str | None = None,
        kind: str | None = "default",
        parent_conversation_id: str | None = None,
        root_conversation_id: str | None = None,
        agent_id: str | None = None,
        agent_name: str | None = None,
        has_agent_id: bool | None = None,
        order: str = "desc",
        sort_by: str = "created_at",
        search_query: str | None = None,
        search_scope: str = "all",
        include_search_match: bool = True,
        host_id: str | None = None,
        created_after: int | None = None,
        created_before: int | None = None,
        updated_after: int | None = None,
        updated_before: int | None = None,
        active_after: int | None = None,
        active_before: int | None = None,
        archived_after: int | None = None,
        archived_before: int | None = None,
        accessible_by: str | None = None,
        owned_by: str | None = None,
        shared_only: bool = False,
        include_archived: bool = False,
        archived_only: bool = False,
        project: str | None = None,
        pinned: bool = False,
        pinned_owner: str | None = None,
        title: str | None = None,
        exclude_labels: Mapping[str, Sequence[str]] | None = None,
        touched_label_key: str | None = None,
    ) -> PagedList[Conversation]:
        """
        List conversations with cursor-based pagination.

        ``order`` controls the sort direction on the column
        selected by ``sort_by`` (``"desc"`` = newest-first,
        ``"asc"`` = oldest-first).

        :param limit: Maximum number of conversations to return.
        :param after: Cursor conversation ID; return conversations
            appearing after this one in sort order,
            e.g. ``"conv_abc123"``.
        :param before: Cursor conversation ID; return conversations
            appearing before this one in sort order.
        :param kind: Filter to conversations of this kind. Exact
            match. ``"default"`` returns only user-initiated.
            ``"sub_agent"`` returns only sub-agent conversations.
            ``None`` disables the filter and returns all.
        :param parent_conversation_id: Phase 4 — when set, only
            return conversations whose
            ``parent_conversation_id == parent_conversation_id``
            (named sub-agents under the given parent). When
            ``None`` (default), the filter is disabled and all
            parent pointers are accepted. Powers the
            ``sys_session_list`` builtin and the ambient-hint
            injection.
        :param root_conversation_id: When set, only return
            conversations whose
            ``root_conversation_id == root_conversation_id``
            (every conversation in the same spawn tree). Powers
            the tree-scoped guard for ``sys_session_get_history`` /
            ``sys_session_close`` so any agent in a tree can
            address any other by ``conversation_id``. ``None``
            disables the filter.
        :param agent_id: When set, only return conversations
            that have at least one task whose ``agent_id``
            matches. Implementation joins through the ``tasks``
            table on the existing indexed FK. Ordering still
            follows ``sort_by`` + ``order`` on the
            *conversation* columns (``created_at`` /
            ``updated_at``); the agent_id filter does NOT
            re-sort by task timestamps. This matters for
            ``--continue``: "most recent" means the
            conversation whose own ``updated_at`` is newest
            (bumped on every item append), which lines up
            with what users expect from "the conversation I
            most recently *did anything in*". Powers the
            Omnigent mode ``--continue`` flag (resume the
            most-recent conversation for the agent that
            *this YAML* registers as) — see
            ``designs/RUN_OMNIGENT_SESSION_RESUMPTION.md``. ``None``
            disables the filter.
        :param agent_name: When set, only return conversations
            whose bound ``conversations.agent_id`` points at an
            agent row with this name. This is intentionally name-
            based so session-scoped agents created by multipart
            ``POST /v1/sessions`` remain resumable without sharing
            a template ``agent_id``. ``None`` disables the filter.
        :param has_agent_id: When ``True``, only return
            conversations whose ``agent_id`` column is not
            ``None`` — i.e. sessions created via
            ``POST /v1/sessions``. When ``None`` (default), the
            filter is disabled. Powers the ``GET /v1/sessions``
            list endpoint.
        :param order: Sort direction, ``"desc"`` or ``"asc"``.
        :param sort_by: Column to sort on, ``"created_at"``,
            ``"updated_at"``, or ``"archived_at"``.
        :param search_query: Case-insensitive substring filter on
            the conversation title OR conversation item content
            (``search_text``). ``None`` or empty string disables
            the filter. A conversation matches if its title
            contains the query OR any of its items' search text
            does. Powers the sidebar's session search on
            ``GET /v1/sessions?search_query=...``.
        :param active_after: Keep conversations whose activity interval ends
            on or after this timestamp. Activity starts at session creation
            and ends at the newest committed conversation item.
        :param active_before: Keep conversations whose activity interval starts
            before this exclusive timestamp.
        :param accessible_by: When set, filter to sessions the
            user has a direct grant on in ``session_permissions``.
            Public (``"__public__"``) grants are deliberately NOT
            included — a public-only session does not appear in the
            user's own list. ``None`` disables the filter (returns
            all sessions).
        :param owned_by: When set, filter to sessions the user
            *owns* (an ``owner``-level grant), a stricter form of
            ``accessible_by`` that excludes sessions merely shared
            with them. Powers the per-project folder fetch, since
            projects only ever hold the owner's own sessions.
            ``None`` disables the filter.
        :param include_archived: When ``False`` (default), archived
            conversations are excluded. When ``True``, archived and
            non-archived conversations are both returned (the caller
            groups them). Powers the sidebar's "Show archived" toggle.
        :param project: Filter by project NAME, dual-reading the
            first-class projects entity and the legacy ``omni_project``
            label (the sidebar's per-project folder fetch). A non-empty
            string returns sessions that EITHER have a first-class
            membership (``metadata.project_id`` → ``owned_by``'s project of
            this name) OR carry the ``omni_project`` label with this value.
            ``""`` returns sessions with NEITHER (unfiled). ``None`` disables
            the filter. The name→id resolution is owner-scoped (projects are
            owner-private), so pass ``owned_by`` alongside a specific name.
            See ``designs/PROJECTS_PRD.md``.
        :param pinned: When ``True``, only return sessions ``pinned_owner`` has
            pinned (their per-user ``omnigent.pinned.<user>`` label — the
            sidebar's Pinned section). ``False`` (default) disables the filter.
        :param pinned_owner: The user whose pins ``pinned=True`` filters to
            (their per-user key). ``None`` → the single-user ``local`` sentinel.
            Ignored unless ``pinned`` is ``True``.
        :param title: When set, only return conversations whose
            ``title`` matches exactly. ``None`` disables the filter.
            Powers the ``(agent, title)`` child-session lookup in
            ``sys_session_send`` so the server can resolve the target
            in a single indexed query instead of fetching all children.
        :param exclude_labels: When set, drop conversations carrying any
            of the given labels: a conversation is excluded when it has a
            ``key``/``value`` pair matching one of the mapping's entries
            (values are matched exactly, any one match suffices).
            ``None`` or empty disables the filter. Lets callers hide a
            category of children (e.g. harness sub-agent mirrors) without
            the store knowing what the label means.
        :param touched_label_key: When set, restrict to conversations
            carrying this label key (the caller's per-user
            ``omnigent.touched.<user>`` key) and order by that label's value
            — the epoch-ms last-interaction time — descending, with the
            store's standard tiebreaker. ``None`` disables the filter.
            Cannot be combined with ``after`` / ``before`` (the cursor is
            defined on the ``sort_by`` column, not the label value), which
            raises ``ValueError``.
        :returns: A :class:`PagedList` of :class:`Conversation`
            objects.
        :raises omnigent.errors.StaleCursorError: If the ``after``/``before``
            conversation no longer exists (e.g. deleted between two page
            fetches) — its sort position is unknowable, and an empty page
            would be indistinguishable from a completed enumeration.
        :raises ValueError: If ``touched_label_key`` is set together with
            ``after`` or ``before``.
        """
        ...

    @abstractmethod
    def search(
        self,
        query: str,
        conversation_id: str | None = None,
        limit: int = 20,
    ) -> list[ConversationItem]:
        """
        Full-text search over conversation items.

        Returns items whose search_text matches the query,
        optionally scoped to a single conversation. Results are
        ranked by relevance.

        :param query: The search query string,
            e.g. ``"deployment error"``.
        :param conversation_id: Optional conversation to scope
            the search to, e.g. ``"conv_abc123"``.
        :param limit: Maximum number of results to return.
        :returns: A list of matching :class:`ConversationItem`
            objects ranked by relevance.
        """
        ...

    @abstractmethod
    def search_visible_items_literal(
        self,
        conversation_id: str,
        query: str,
        limit: int = 20,
    ) -> list[ConversationItem]:
        """Search one transcript by literal user-visible text."""
        ...

    @abstractmethod
    def update_conversation(
        self,
        conversation_id: str,
        title: str | None = None,
        reasoning_effort: str | None = None,
        _unset_reasoning_effort: bool = False,
        model_override: str | None = None,
        _unset_model_override: bool = False,
        cost_control_mode_override: str | None = None,
        _unset_cost_control_mode_override: bool = False,
        subagent_routing_override: str | None = None,
        _unset_subagent_routing_override: bool = False,
        harness_override: str | None = None,
        _unset_harness_override: bool = False,
        share_workspace_files: bool | None = None,
        terminal_launch_args: list[str] | None = None,
        archived: bool | None = None,
        close_cli_on_archive: bool = False,
        archive_stop_when_idle: bool = False,
        delete_worktree: bool = False,
        keep_worktree: bool = False,
        reported_model: str | None = None,
    ) -> Conversation | None:
        """
        Update mutable fields on a conversation.

        For ``reasoning_effort``, ``model_override``,
        ``cost_control_mode_override``, ``subagent_routing_override``,
        and ``harness_override``,
        ``None`` means "leave unchanged". To explicitly clear them
        back to ``None``, pass
        the matching ``_unset_*`` flag. ``reported_model`` (the model
        the harness last reported, verbatim) has no ``_unset`` variant:
        reports only ever move forward.

        :param conversation_id: Unique conversation identifier,
            e.g. ``"conv_abc123"``.
        :param title: New title for the conversation, or ``None``
            to leave unchanged.
        :param reasoning_effort: Per-session reasoning effort hint,
            e.g. ``"high"``. ``None`` leaves unchanged.
        :param _unset_reasoning_effort: When ``True``, set
            ``reasoning_effort`` to ``None`` regardless of the
            ``reasoning_effort`` param value.
        :param model_override: Per-session LLM model override,
            e.g. ``"claude-opus-4-7"``. ``None`` leaves unchanged.
        :param _unset_model_override: When ``True``, set
            ``model_override`` to ``None`` regardless of the
            ``model_override`` param value.
        :param cost_control_mode_override: Per-session cost-control
            switch, ``"on"`` or ``"off"``. ``None`` leaves unchanged.
        :param _unset_cost_control_mode_override: When ``True``, set
            ``cost_control_mode_override`` to ``None`` regardless of
            the ``cost_control_mode_override`` param value.
        :param subagent_routing_override: Per-session subagent-routing
            switch, ``"on"`` or ``"off"``. ``None`` leaves unchanged.
        :param _unset_subagent_routing_override: When ``True``, set
            ``subagent_routing_override`` to ``None`` regardless of the
            ``subagent_routing_override`` param value. Unset reads as
            Default (the switch is two-state; nothing is inherited).
        :param harness_override: Per-session brain-harness override,
            e.g. ``"pi"``. ``None`` leaves unchanged. No ``_unset``
            variant — the override is set once at session create and
            immutable thereafter (the harness process is spawned on
            the first turn).
        :param share_workspace_files: Whether view-level collaborators may
            browse the workspace. ``True`` stores the share, ``False``
            clears it (edit-only again), ``None`` leaves it unchanged.
        :param terminal_launch_args: Per-session native-terminal
            pass-through args, e.g.
            ``["--dangerously-skip-permissions"]``. ``None`` leaves
            unchanged; a list (including ``[]``) replaces the stored
            value wholesale (resume is last-write-wins, never an
            append).
        :param archived: New archived state. ``True`` archives
            (hides from the default listing), ``False`` unarchives,
            ``None`` leaves unchanged.
        :param close_cli_on_archive: Atomically create a durable teardown
            request when this call transitions ``archived`` to ``True``.
        :param archive_stop_when_idle: When ``True`` alongside
            ``close_cli_on_archive`` on an archive transition, atomically
            stamp the server-reserved idle-deferral label naming the new
            archive revision, so the teardown waits for the tree to settle.
            Deletes any prior label on a transition without it (including
            unarchive). No effect outside a transition.
        :param delete_worktree: When ``True`` alongside
            ``close_cli_on_archive``, atomically stamp the server-reserved
            worktree-delete label naming the archive revision the durable
            teardown must remove the root's recorded worktree for. On a
            transition that is the new revision; on an already-archived
            session it is the current revision and the same call re-opens
            the close (clearing any completed revision) so a delete-only
            teardown runs. Deletes any prior label on a transition without
            it (including unarchive).
        :returns: The updated :class:`Conversation`, or ``None``
            if the conversation does not exist.
        """
        ...

    @abstractmethod
    def reassign_live_children(
        self,
        old_id: str,
        new_id: str,
        receipt_id: str,
        *,
        reverse_of: bool = False,
    ) -> tuple[list[str], list[str]]:
        """Move old's still-unarchived direct children and their subtrees under new.

        One write transaction: lock ``new`` then ``old``, re-read each direct
        child of ``old`` under its own lock and keep only the unarchived ones,
        reparent them to ``new``, rewrite ``root_conversation_id`` across
        their subtrees, write the succession labels and the receipt with
        phase ``moved``. When no child is kept nothing is written and
        ``([], [])`` is returned; refusals raise before any write.

        :param old_id: Top-level session whose children move.
        :param new_id: Top-level successor session.
        :param receipt_id: Pre-generated id for the receipt row.
        :param reverse_of: When ``True`` this move reverses an existing link
            (``new_id.succeeded_by == old_id``); the forward pair is removed
            in the same transaction, and only once the move is known to
            proceed, so a refusal leaves it intact.
        :returns: ``(direct_ids, moved_ids)`` — the kept direct children and
            every moved session (kept children plus their descendants).
        :raises SuccessionRefusedError: With ``code`` ``same_session``,
            ``not_found``, ``not_top_level``, ``target_archived`` or
            ``title_clash``.
        """
        ...

    @abstractmethod
    def get_succession(self, old_id: str, new_id: str) -> SessionSuccession | None:
        """Return the receipt for one ``(old_id, new_id)`` pair, or ``None``."""
        ...

    @abstractmethod
    def get_succession_by_id(self, receipt_id: str) -> SessionSuccession | None:
        """Return the receipt by id, or ``None``."""
        ...

    @abstractmethod
    def list_unfinished_successions(self, limit: int = 100) -> list[SessionSuccession]:
        """Return receipts whose phase is not ``done``, oldest first."""
        ...

    @abstractmethod
    def update_succession(
        self,
        receipt_id: str,
        *,
        expected_phase: str,
        **fields: Any,
    ) -> bool:
        """Compare-and-set the receipt's phase and patch the given fields.

        The update applies only while the stored phase still equals
        ``expected_phase``, so two resumers cannot both advance one receipt.
        ``phase`` (the new value), ``opening_item_id`` and ``error`` are
        stored verbatim; ``opening``, ``dropped`` and ``questions`` accept
        Python dicts/lists and are JSON-serialized. ``updated_at`` is always
        refreshed.

        :param receipt_id: Receipt row id.
        :param expected_phase: Phase the receipt must still be in.
        :param fields: Column updates, e.g. ``phase="rekeyed"``.
        :returns: ``True`` when the row was updated, ``False`` when the
            phase no longer matched or the receipt is missing.
        """
        ...

    @abstractmethod
    def add_detached_card(self, card: DetachedCard) -> None:
        """Insert one detached card record; expired records are dropped first."""
        ...

    @abstractmethod
    def list_detached_cards(self) -> list[DetachedCard]:
        """Return every detached card record in the workspace, oldest first."""
        ...

    @abstractmethod
    def update_detached_card(self, elicitation_id: str, **fields: Any) -> None:
        """Patch one record: ``state`` and ``delivery_text`` verbatim,
        ``verdict`` as a dict, ``expires_at`` in epoch seconds."""
        ...

    @abstractmethod
    def delete_detached_card(self, elicitation_id: str) -> None:
        """Delete one record; a missing record is a no-op."""
        ...

    @abstractmethod
    def delete_detached_grants(self, session_id: str, grant_key: list[str]) -> None:
        """Delete the session's answered or settled records carrying *grant_key*."""
        ...

    @abstractmethod
    def update_conversation_with_changes(
        self,
        conversation_id: str,
        title: str | None = None,
        reasoning_effort: str | None = None,
        _unset_reasoning_effort: bool = False,
        model_override: str | None = None,
        _unset_model_override: bool = False,
        cost_control_mode_override: str | None = None,
        _unset_cost_control_mode_override: bool = False,
        subagent_routing_override: str | None = None,
        _unset_subagent_routing_override: bool = False,
        harness_override: str | None = None,
        _unset_harness_override: bool = False,
        share_workspace_files: bool | None = None,
        terminal_launch_args: list[str] | None = None,
        archived: bool | None = None,
        close_cli_on_archive: bool = False,
        archive_stop_when_idle: bool = False,
        delete_worktree: bool = False,
        keep_worktree: bool = False,
        reported_model: str | None = None,
    ) -> ConversationUpdateResult | None:
        """Update a conversation and report requested model-setting changes.

        The returned change flags describe only the explicitly requested
        ``reasoning_effort`` and ``model_override`` updates. A request that
        writes the value already stored, including an explicit clear of an
        already-``None`` value, reports ``False``.

        :param close_cli_on_archive: Atomically create a durable teardown
            request when this call transitions ``archived`` to ``True``.
        :param archive_stop_when_idle: When ``True`` alongside
            ``close_cli_on_archive`` on an archive transition, atomically
            stamp the server-reserved idle-deferral label naming the new
            archive revision, so the teardown waits for the tree to settle.
            Deletes any prior label on a transition without it (including
            unarchive). No effect outside a transition.
        :param delete_worktree: When ``True`` alongside
            ``close_cli_on_archive``, atomically stamp the server-reserved
            worktree-delete label naming the archive revision the durable
            teardown must remove the root's recorded worktree for. On a
            transition that is the new revision; on an already-archived
            session it is the current revision and the same call re-opens
            the close (clearing any completed revision) so a delete-only
            teardown runs. Deletes any prior label on a transition without
            it (including unarchive).
        """
        ...

    @abstractmethod
    def restore_session_settings_if_matches(
        self,
        conversation_id: str,
        *,
        previous: Conversation,
        attempted: Conversation,
        restore_effort: bool = True,
        restore_model: bool = False,
    ) -> None:
        """Restore a refused effort update without overwriting a newer selection.

        Each setting is compared and restored atomically. When ``restore_model``
        is true, also undo the model selection from a combined PATCH that was
        aborted before forwarding its model change. Preserve other overrides.

        :param conversation_id: Conversation whose settings were refused.
        :param previous: Snapshot before persisting the requested settings.
        :param attempted: Snapshot returned by that persistence operation.
        :param restore_effort: Whether the refused effort needs rollback; false when
            a newer write already replaced it.
        :param restore_model: Whether the unforwarded model change also needs rollback.
        """
        ...

    @abstractmethod
    def clear_model_override_if_matches(
        self,
        conversation_id: str,
        expected_model_override: str,
    ) -> bool:
        """Clear a model selection only while the stored settings still match.

        :param conversation_id: Conversation to update.
        :param expected_model_override: Model selection that must still be stored.
        :returns: ``True`` when cleared; ``False`` when missing, mismatched,
            or any session override changed concurrently. Other settings and
            metadata remain unchanged.
        """
        ...

    @abstractmethod
    def list_pending_archive_closes(self, *, limit: int = 200) -> list[Conversation]:
        """List archived roots whose durable CLI teardown is incomplete."""
        ...

    @abstractmethod
    def pending_archive_close_workspaces(self) -> set[int]:
        """Return workspace ids containing incomplete archive close requests."""
        ...

    @abstractmethod
    def list_workspace_ids_with_archived_before(self, *, archived_before: int) -> list[int]:
        """Return every workspace id holding a conversation archived before a cutoff.

        Privileged cross-workspace scan for the archive cleanup job, which runs
        in the lifespan's default workspace and must reach every tenant
        partition. Ids are ordered so a sweep is deterministic.

        :param archived_before: Exclusive epoch-seconds cutoff on ``archived_at``.
        :returns: Workspace ids in ascending order.
        """
        ...

    @abstractmethod
    def claim_archive_close(
        self,
        conversation_id: str,
        revision: int,
        token: str,
        *,
        claimed_at: int,
        stale_before: int,
    ) -> ArchiveCloseClaimResult:
        """Lease one still-current archive teardown request across replicas."""
        ...

    @abstractmethod
    def complete_archive_close(
        self,
        conversation_id: str,
        revision: int,
        token: str,
    ) -> bool:
        """Complete only the claimed, still-current archive revision."""
        ...

    @abstractmethod
    def renew_archive_close_claim(
        self,
        conversation_id: str,
        token: str,
        *,
        claimed_at: int,
    ) -> bool:
        """Refresh one archive-close lease while the opaque token still owns it."""
        ...

    @abstractmethod
    def release_archive_close_claim(
        self,
        conversation_id: str,
        revision: int,
        token: str,
        *,
        error: str | None = None,
    ) -> bool:
        """Release a retryable claim and retain a bounded diagnostic."""
        ...

    @abstractmethod
    def clear_stale_archive_close_claim(
        self,
        conversation_id: str,
        *,
        stale_before: int,
    ) -> bool:
        """Release a dead worker's lease whose close request is no longer current."""
        ...

    @abstractmethod
    def finalize_archive_close(self, conversation_id: str, revision: int) -> bool:
        """Mark a current root request complete after every target completed."""
        ...

    @abstractmethod
    def rename_conversation_if_title_matches(
        self,
        conversation_id: str,
        expected_title: str,
        title: str,
    ) -> Conversation | None:
        """Rename a conversation only while its current title matches.

        :param conversation_id: Conversation to update.
        :param expected_title: Title that must still be stored.
        :param title: Replacement title.
        :returns: The updated conversation, or ``None`` when the row is
            missing or its title changed before this call.
        """
        ...

    @abstractmethod
    def set_task_summary(
        self,
        conversation_id: str,
        task_summary: str,
    ) -> Conversation | None:
        """Set a human-readable task summary on a sub-agent conversation.

        :param conversation_id: Conversation to update.
        :param task_summary: Short task-derived label, e.g.
            ``"Investigate auth token refresh"``.
        :returns: The updated conversation, or ``None`` when the row
            does not exist.
        """
        ...

    @abstractmethod
    def set_labels(
        self,
        conversation_id: str,
        updates: dict[str, str],
        updated_at: int | None = None,
    ) -> None:
        """
        Upsert guardrails labels on a conversation.

        Atomic batched UPSERT: either every key in *updates*
        lands, or none of them do (matches POLICIES.md §6.3).
        Overwrites existing rows for the same keys;
        non-mentioned keys are left untouched. The caller is
        responsible for schema validation (``values`` /
        ``monotonic``) — the store persists whatever it's
        given (see POLICIES.md §9.2 + §13 where that
        validation lives in ``PolicyEngine.apply_label_writes``).

        Callers that need "insert only if missing" semantics
        (initial-value seeding — POLICIES.md §10) should check
        ``conversation.labels`` first and filter the updates
        to keys not already present; this method always
        overwrites.

        :param conversation_id: The conversation to update,
            e.g. ``"conv_abc123"``. If the conversation does
            not exist, behavior is implementation-defined
            (typically raises via the FK constraint).
        :param updates: Mapping from label key to new value.
            Both keys and values must already be strings
            (string coercion happens upstream at spec load).
            Example: ``{"integrity": "0", "sensitivity": "confidential"}``.
            Empty dict is a no-op.
        :param updated_at: Unix epoch seconds to stamp on the
            affected rows. ``None`` (default) → the store
            records the current time. The caller-supplied form
            is there for the policy engine to pass its
            evaluation timestamp (POLICIES.md §6.3), keeping
            audit trails aligned with the enforcement site
            rather than wall-clock drift between evaluate()
            and the actual DB write.
        """
        ...

    @abstractmethod
    def insert_label_if_absent(
        self,
        conversation_id: str,
        key: str,
        value: str,
    ) -> str:
        """
        Insert a label only while its key is absent; return the stored value.

        First-writer-wins creation of a per-session secret (the
        artifact-link key): concurrent first creators each attempt the
        insert, exactly one wins, and every caller signs with the value
        actually stored. An upsert would let the losers overwrite the
        winner, minting links against a key another caller never sees.

        :param conversation_id: The conversation to update,
            e.g. ``"conv_abc123"``.
        :param key: The label key to create, e.g.
            ``"omnigent.artifact_link_key"``.
        :param value: Value to store when the key is absent.
        :returns: The stored value — the pre-existing one when another
            writer won the race.
        """
        ...

    @abstractmethod
    def claim_conversation_deletion(
        self,
        conversation_id: str,
        token: str,
        *,
        claimed_at: int,
        stale_before: int,
    ) -> DeletionClaimResult:
        """Atomically claim an unlocked conversation for destructive cleanup.

        A claim blocks archive-lock changes across processes. Claims older than
        ``stale_before`` may be replaced so a process crash cannot strand a
        session permanently.
        """
        ...

    @abstractmethod
    def release_conversation_deletion(self, conversation_id: str, token: str) -> bool:
        """Release only the deletion claim owned by ``token``."""
        ...

    @abstractmethod
    def renew_conversation_deletion(
        self,
        conversation_id: str,
        token: str,
        *,
        claimed_at: int,
    ) -> bool:
        """Refresh an active claim only while ``token`` still owns it."""
        ...

    @abstractmethod
    def set_archive_lock(
        self,
        conversation_id: str,
        locked: bool,
        *,
        updated_at: int,
        stale_before: int,
    ) -> ArchiveLockWriteResult:
        """Atomically change archive protection unless deletion is active."""
        ...

    @abstractmethod
    def delete_label(
        self,
        conversation_id: str,
        key: str,
    ) -> None:
        """
        Delete a single label key from a conversation.

        No-op if the label does not exist. Counterpart to
        :meth:`set_labels` for clearing one key — e.g. removing a
        session from its sidebar project (deleting the
        ``omni_project`` label).

        :param conversation_id: The conversation to update,
            e.g. ``"conv_abc123"``.
        :param key: The label key to remove, e.g. ``"omni_project"``.
        """
        ...

    @abstractmethod
    def list_projects(
        self,
        accessible_by: str | None = None,
        owned_by: str | None = None,
    ) -> list[str]:
        """
        Return all distinct sidebar "project" names, ordered ascending.

        Projects are implicit: a project exists while at least one
        *non-archived* conversation carries a
        ``conversation_labels`` row with ``key="omni_project"``
        naming it. Archived sessions keep their project label, but a
        project whose every member is archived drops out of this list
        (so "Delete project" — which archives all members — removes the
        folder, while unarchiving a member restores it).

        :param accessible_by: When set, restrict to projects on
            sessions the user has a permission row for (mirrors the
            ``list_conversations`` ACL filter). ``None`` returns
            projects across all sessions.
        :param owned_by: When set, restrict to projects that contain at
            least one session the user owns (an ``owner``-level grant).
            Projects are a "My sessions"-only surface, so this keeps a
            project owned by someone else — but with a session shared to
            the user — from appearing as one of the user's own folders.
        :returns: List of project names ordered alphabetically.
        """
        ...

    @abstractmethod
    def list_archived_facets(
        self,
        accessible_by: str | None = None,
        *,
        search_query: str | None = None,
        search_scope: str = "title",
        project: str | None = None,
        host_id: str | None = None,
        agent_name: str | None = None,
        created_after: int | None = None,
        created_before: int | None = None,
        active_after: int | None = None,
        active_before: int | None = None,
        archived_after: int | None = None,
        archived_before: int | None = None,
    ) -> ArchivedConversationFacets:
        """Aggregate linked Archive facet values in the storage layer."""
        ...

    @abstractmethod
    def set_session_state(
        self,
        conversation_id: str,
        state: dict[str, Any],
    ) -> None:
        """
        Persist the full session-state snapshot for a conversation.

        Replaces policy-visible state while preserving the internal Plan key
        in the existing conversation metadata JSON. Called by
        :meth:`PolicyEngine.apply_state_updates` after applying
        structured :class:`StateUpdate` operations to the hot
        cache.

        :param conversation_id: The conversation to update,
            e.g. ``"conv_abc123"``.
        :param state: The complete session-state dict to persist.
            Serialized as JSON. Empty dict is stored as ``"{}"``.
        """
        ...

    @abstractmethod
    def set_session_usage(
        self,
        conversation_id: str,
        usage: dict[str, Any],
    ) -> None:
        """
        Persist the cumulative LLM token usage for a conversation.

        Overwrites the existing ``session_usage`` JSON column with
        the serialized *usage* dict. Called by
        :meth:`PolicyEngine.record_usage` after incrementing the
        in-memory counters.

        :param conversation_id: The conversation to update,
            e.g. ``"conv_abc123"``.
        :param usage: The complete usage dict to persist, e.g.
            ``{"input_tokens": 1500, "output_tokens": 350,
            "total_tokens": 1850}``. May carry a nested ``"by_model"``
            sub-dict (per-model token/cost buckets), hence ``Any``.
        """
        ...

    def set_provider_usage_limits(
        self,
        conversation_id: str,
        snapshot: dict[str, Any] | None,
    ) -> None:
        """Persist the latest provider allowance snapshot for a session.

        Backends that support terminal/native sessions should override this.
        It is intentionally a first-class metadata value: serialized snapshots
        can exceed the 256-character conversation-label limit.

        :param conversation_id: Conversation to update.
        :param snapshot: Sanitized snapshot, or ``None`` to clear it.
        """
        raise NotImplementedError

    def set_session_todos(
        self,
        conversation_id: str,
        todos: list[dict[str, Any]],
    ) -> bool:
        """Persist the native Plan snapshot; empty clears, missing metadata returns false."""
        raise NotImplementedError

    @abstractmethod
    def set_conversation_project(
        self,
        conversation_id: str,
        project_id: str | None,
    ) -> bool:
        """
        File a conversation into a first-class project (or unfile it).

        Sets ``omnigent_conversation_metadata.project_id``; ``None`` unfiles
        the session. The first-class counterpart to moving a session between
        ``omni_project`` labels (see ``designs/PROJECTS_PRD.md``).

        :param conversation_id: The conversation to update, e.g. ``"conv_abc"``.
        :param project_id: The project id to file under, or ``None`` to unfile.
        :returns: ``True`` if a metadata row was updated; ``False`` if the
            conversation has no metadata row.
        """
        ...

    @abstractmethod
    def increment_session_usage(
        self,
        conversation_id: str,
        delta: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Atomically apply a usage delta to a conversation's ``session_usage``.

        Reads the current JSON, applies *delta* (adding each key's value to the
        existing value, with ``by_model`` merged recursively), and writes back —
        all within a single database transaction. Concurrent writers are
        serialised via dialect-appropriate locking: ``SELECT FOR UPDATE`` on
        PostgreSQL / MySQL / MariaDB; ``BEGIN IMMEDIATE`` (write lock before
        the first read) on SQLite, which avoids ``SQLITE_BUSY_SNAPSHOT`` that
        a plain deferred ``SELECT``-then-``UPDATE`` would raise under concurrent
        writers. This prevents the read-modify-write
        race that caused concurrent relay completions to silently drop each
        other's cost / token deltas (#9).

        *delta* uses the same key layout as ``session_usage``:
        - flat numeric keys (``"input_tokens"``, ``"total_cost_usd"``, …) are
          added to the existing value (``0`` when absent).
        - ``"by_model"`` is a nested dict ``{model_id: {sub_key: value}}``; each
          model's sub-keys are added independently, creating the bucket on first
          use.

        :param conversation_id: The conversation to update,
            e.g. ``"conv_abc123"``.
        :param delta: Usage increments to apply, e.g.
            ``{"input_tokens": 1000, "total_cost_usd": 0.05,
            "by_model": {"claude-sonnet-4-6": {"input_tokens": 1000,
            "total_cost_usd": 0.05}}}``.
        :returns: The updated ``session_usage`` dict after the increment.
        """
        ...

    @abstractmethod
    def add_daily_cost(self, user_id: str, day_utc: str, delta_usd: float) -> None:
        """
        Atomically add *delta_usd* to a user's spend for one UTC day.

        UPSERTs the ``user_daily_cost`` row keyed by
        ``(user_id, day_utc)``: inserts ``delta_usd`` when no row
        exists, otherwise increments the existing ``cost_usd`` by
        ``delta_usd`` in a single atomic statement (no
        read-modify-write, so concurrent turns and replicas don't lose
        updates). Records every priced turn and powers per-user daily
        budget reads, including sessions without a budget policy.

        :param user_id: The user the cost is attributed to (the session
            creator), e.g. ``"alice@example.com"``.
        :param day_utc: UTC calendar day as an ISO date string
            ``"YYYY-MM-DD"``, e.g. ``"2026-06-05"``.
        :param delta_usd: USD amount to add. A no-op when ``<= 0`` so a
            zero-cost turn (e.g. pricing unavailable) never creates a
            row.
        """
        ...

    @abstractmethod
    def get_daily_cost(self, user_id: str, day_utc: str) -> float:
        """
        Return a user's accumulated LLM spend for one UTC day.

        :param user_id: The user to read, e.g. ``"alice@example.com"``.
        :param day_utc: UTC calendar day as an ISO date string
            ``"YYYY-MM-DD"``, e.g. ``"2026-06-05"``.
        :returns: The accumulated ``cost_usd`` for that
            ``(user_id, day_utc)``, or ``0.0`` when no row exists.
        """
        ...

    @abstractmethod
    def sum_daily_cost(self, user_id: str, since_day_utc: str) -> float:
        """
        Sum a user's LLM spend over all UTC days ``>= since_day_utc``.

        Backs the ``omni usage`` rolling-window summary: the daily rollup
        is time-attributed per calendar day, so summing the days in a
        window gives spend that actually happened in that window (a
        weeks-old session touched today no longer dumps its whole cost
        into "today"). Day strings sort lexicographically because they are
        zero-padded ``"YYYY-MM-DD"``, so the ``>=`` range works as a plain
        string comparison.

        :param user_id: The user to read, e.g. ``"alice@example.com"``.
        :param since_day_utc: Inclusive lower-bound UTC day as an ISO date
            string ``"YYYY-MM-DD"``, e.g. ``"2026-06-05"``.
        :returns: The summed ``cost_usd`` across matching days, or ``0.0``
            when no rows fall in the range.
        """
        ...

    @abstractmethod
    def list_daily_costs(self, user_id: str, since_day_utc: str) -> list[tuple[str, float]]:
        """
        Return per-day cost rows for a user from ``since_day_utc`` onward.

        :param user_id: The user to read, e.g. ``"alice@example.com"``.
        :param since_day_utc: Inclusive lower-bound UTC day as ``"YYYY-MM-DD"``.
        :returns: List of ``(day_utc, cost_usd)`` tuples, ascending by day.
            Days with no spend are omitted.
        """
        ...

    @abstractmethod
    def get_daily_cost_state(self, user_id: str, day_utc: str) -> dict[str, float]:
        """
        Return a user's daily cost rollup state for one UTC day.

        Reads both the accumulated spend and the highest soft
        checkpoint already approved that day, in one lookup — what the
        per-user daily cost-budget policy needs.

        :param user_id: The user to read, e.g. ``"alice@example.com"``.
        :param day_utc: UTC calendar day as an ISO date string
            ``"YYYY-MM-DD"``, e.g. ``"2026-06-05"``.
        :returns: ``{"cost_usd": <float>, "ask_approved_usd": <float>}``;
            both ``0.0`` when no row exists for ``(user_id, day_utc)``.
        """
        ...

    @abstractmethod
    def set_daily_ask_approved(self, user_id: str, day_utc: str, ask_approved_usd: float) -> None:
        """
        Record the highest approved soft checkpoint for a user+day.

        Sets ``ask_approved_usd`` without altering ``cost_usd`` (insert
        with ``cost_usd = 0`` when no row exists, else update only the
        approval field). Called when a per-user daily cost-budget ASK is
        approved, so an approved checkpoint does not re-prompt that user
        again the same day — including from other sessions.

        :param user_id: The user the approval is for, e.g.
            ``"alice@example.com"``.
        :param day_utc: UTC calendar day as ``"YYYY-MM-DD"``, e.g.
            ``"2026-06-05"``.
        :param ask_approved_usd: The crossed checkpoint value (USD) the
            user approved continuing past, e.g. ``0.05``.
        """
        ...

    @abstractmethod
    def list_daily_cost_states(
        self,
        user_id: str,
        since_day_utc: str,
    ) -> list[DailyCostState]:
        """
        Return daily cost states for a user from since_day_utc onward.

        Reads the full state (cost_usd, ask_approved_usd, day_utc) for
        each day with recorded cost >= since_day_utc. Used by both daily
        and period-based cost-budget policies.

        :param user_id: The user to read, e.g. ``"alice@example.com"``.
        :param since_day_utc: Inclusive lower-bound UTC day as ``"YYYY-MM-DD"``.
        :returns: List of :class:`DailyCostState` dicts. Days with no spend
            are omitted. Sorted ascending by day_utc.
        """
        ...

    @abstractmethod
    def get_session_owner(self, conversation_id: str, *, owner_only: bool = False) -> str | None:
        """
        Return the highest-privilege non-public grantee of a session.

        By default, lower-level grants are a fallback when no owner grant exists,
        preserving cost attribution for shared sessions. Use ``owner_only=True``
        for ownership checks; sharing alone does not establish ownership.

        :param conversation_id: The session to look up, e.g. ``"conv_abc123"``.
        :param owner_only: Require an explicit owner-level grant.
        :returns: The grantee's user id, or ``None`` if no qualifying grant exists.
        """
        ...

    @abstractmethod
    def get_session_owner_authority(
        self, conversation_id: str, *, owner_only: bool = False
    ) -> "AccountAuthority | None":
        """Capture the session owner's username and registration in one read.

        Session-attributed writes must retain this snapshot in a target account
        scope until the write validates it under the account lock. A null
        generation represents an external identity, not a future registration.
        """
        ...

    @abstractmethod
    def set_runner_id(
        self,
        conversation_id: str,
        runner_id: str,
        *,
        admission_host_id: str | None = None,
        admission_workspace: str | None = None,
    ) -> bool:
        """
        Atomically pin ``conversations.runner_id`` only if currently NULL.

        Implemented as ``UPDATE ... WHERE id = :id AND runner_id IS NULL``
        so concurrent binders race safely: exactly one transitions the
        row from NULL → ``runner_id`` and gets ``True``; others (or an
        already-bound / missing row) get ``False``. Closes the TOCTOU on
        host-launch binding (see ``resolve_host_launch``).

        :param conversation_id: Conversation to pin, e.g.
            ``"conv_abc123"``.
        :param runner_id: Runner id to bind to, e.g.
            ``"runner_abc123"``.
        :param admission_host_id: Host selected for this launch when it differs
            from the row's old binding.
        :param admission_workspace: Directory selected for this launch before
            the row's worktree binding is persisted.
        :returns: ``True`` if this call won the bind (NULL → runner_id);
            ``False`` if already bound or the row doesn't exist.
        """
        ...

    @abstractmethod
    def touch_runner_liveness(self, runner_ids: list[str], now: int) -> None:
        """
        Stamp ``runner_last_seen`` for every session bound to these runners.

        Called by the replica holding the runner tunnels (on connect and
        on a periodic sweep of the live registry) so any replica can
        derive ``runner_online`` from freshness. One bulk ``UPDATE``;
        must NOT bump ``updated_at`` (it drives sidebar ordering).

        :param runner_ids: Runner ids with a live tunnel,
            e.g. ``["runner_token_abc123"]``. Empty is a no-op.
        :param now: Epoch seconds to stamp.
        """
        ...

    @abstractmethod
    def clear_runner_liveness(self, runner_id: str, not_after: int | None = None) -> None:
        """
        Clear ``runner_last_seen`` for every session bound to a runner.

        Called on a graceful tunnel disconnect so the sidebar flips
        offline immediately instead of waiting out
        :data:`RUNNER_LIVENESS_TTL_S`. Must NOT bump ``updated_at``.

        :param runner_id: The disconnected runner's id.
        :param not_after: When given, only clear a row whose
            ``runner_last_seen`` is ``NULL`` or ``<= not_after`` — the
            runner may have re-tunnelled to another replica, which
            stamps a newer value this clear must not erase. ``None``
            clears unconditionally (the pre-cross-replica behavior).
        """
        ...

    @abstractmethod
    def set_session_live_status(self, conversation_id: str, status: str) -> None:
        """
        Persist the relay-observed turn status for one session.

        Written by the replica whose SSE relay observed the transition
        (idle/running/waiting/failed) so any replica's session list can
        serve it. Must NOT bump ``updated_at``.

        :param conversation_id: Session/conversation identifier.
        :param status: One of ``enum_codecs.SESSION_LIVE_STATUS``.
        """
        ...

    @abstractmethod
    def settle_intentionally_stopped_session(self, conversation_id: str, runner_id: str) -> bool:
        """Set idle only while this runner still owns a non-failed session.

        Match the current runner binding in the update, without changing labels
        or ``updated_at``. A failed status must survive teardown reconciliation.

        :param conversation_id: Session whose runner was intentionally stopped.
        :param runner_id: The stopped runner, which may have been replaced.
        :returns: Whether the conditional update matched the session.
        """
        ...

    @abstractmethod
    def settle_orphaned_live_status(self, conversation_id: str, stale_before: int) -> bool:
        """Atomically settle a stale running session to idle.

        The update must require a bound runner, ``running``/``waiting`` live
        status, and a missing or older ``runner_last_seen`` stamp. It must not
        bump ``updated_at``.

        :param conversation_id: Session/conversation identifier.
        :param stale_before: Runner stamps at or after this epoch are fresh.
        :returns: Whether this call performed the transition.
        """
        ...

    @abstractmethod
    def get_native_subagent_reconcile_fingerprint(
        self,
        conversation_id: str,
        label_keys: tuple[str, ...],
    ) -> NativeSubagentReconcileFingerprint | None:
        """Freeze the state needed for a read-only native child probe.

        :param conversation_id: Direct child conversation to inspect.
        :param label_keys: Identity, terminal, unverified, and failure-label
            keys whose values and write timestamps must remain unchanged.
        :returns: A fingerprint, or ``None`` when the child does not exist.
        """
        ...

    @abstractmethod
    def reconcile_native_subagent_status(
        self,
        expected: NativeSubagentReconcileFingerprint,
        *,
        expected_parent: NativeSubagentReconcileFingerprint | None = None,
        live_status: str | None,
        label_updates: dict[str, str],
    ) -> NativeSubagentReconcileWriteResult:
        """Apply a terminal repair only while *expected* still matches.

        The child comparison, optional parent runtime comparison, and writes
        are one transaction. ``None`` preserves the frozen live status.
        Implementations that
        split conversations/labels and Omnigent metadata across independent
        databases must return ``"unsupported"`` instead of weakening the
        compare-and-set guarantee.
        """
        ...

    @abstractmethod
    def set_pending_elicitation_count(self, conversation_id: str, count: int) -> None:
        """
        Persist the outstanding elicitation count for one session.

        Written on every pending-elicitation publish/resolve so any
        replica's session list shows parked approvals. Must NOT bump
        ``updated_at``.

        :param conversation_id: Session/conversation identifier.
        :param count: Outstanding elicitations, ``>= 0``.
        """
        ...

    @abstractmethod
    def replace_runner_id(
        self, conversation_id: str, runner_id: str, *, expected_runner_id: str | None = None
    ) -> Conversation:
        """
        Replace ``conversations.runner_id`` for a conversation.

        Atomic last-write-wins write. Public session binding routes
        validate session-scoped agent ownership before calling this
        method; internal sub-agent code also uses it to keep child
        conversations on the parent's current runner.

        Runner/host binding is live state, not conversation activity, so
        this must NOT bump ``updated_at`` (it drives sidebar ordering
        and the unread dot).

        :param conversation_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param runner_id: Runner identifier to bind to,
            e.g. ``"runner_abc123"``. Online-ness is validated
            by the route before calling the store.
        :param expected_runner_id: Update only while the old binding matches; otherwise
            return the current conversation unchanged. None means unconditional.
        :returns: The updated :class:`Conversation`.
        :raises ConversationNotFoundError: If no conversation row
            with ``conversation_id`` exists.
        """
        ...

    @abstractmethod
    def clear_runner_id(self, conversation_id: str) -> Conversation:
        """
        Null out ``conversations.runner_id``.

        Counterpart to :meth:`replace_runner_id` for the 1:1
        session↔runner invariant — /clear and /switch unbind the old
        session before binding the runner to the new one.

        Runner/host binding is live state, not conversation activity, so
        this must NOT bump ``updated_at`` (it drives sidebar ordering
        and the unread dot).

        :param conversation_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: The updated :class:`Conversation`.
        :raises ConversationNotFoundError: If no conversation row
            with ``conversation_id`` exists.
        """
        ...

    @abstractmethod
    def clear_host_binding(self, conversation_id: str) -> Conversation:
        """
        Revert a session to fully unbound: NULL ``host_id``,
        ``workspace``, ``worktree``, ``git_branch``, and ``runner_id``
        together.

        Used to undo a failed per-session bind (``POST
        /v1/hosts/{id}/runners``) after the runner was atomically
        bound and the binding fields persisted, but the launch
        failed and any worktree was rolled back. Clearing all five
        fields in one transaction keeps the row consistent with the
        host's actual state (no runner, no worktree) and lets a later
        rebind start from a clean slate. Nulling ``host_id`` and
        ``workspace`` together never violates
        ``ck_conversations_workspace_required_for_host`` (workspace
        is only required while ``host_id`` is set).

        Runner/host binding is live state, not conversation activity, so
        this must NOT bump ``updated_at`` (it drives sidebar ordering
        and the unread dot).

        :param conversation_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: The updated :class:`Conversation`.
        :raises ConversationNotFoundError: If no conversation row
            with ``conversation_id`` exists.
        """
        ...

    @abstractmethod
    def list_runner_session_statuses(
        self, runner_id: str, *, after: str | None = None, limit: int = 200
    ) -> list[tuple[str, str | None]]:
        """Read a bounded page of session IDs and live statuses for runner teardown.

        Include archived sessions. Read bindings consistently with runner writes,
        in ascending session-ID order; use the last ID as the next page's cursor.
        A short page ends iteration. Do not hydrate conversation content or labels.

        :param runner_id: The runner being stopped.
        :param after: Exclusive session-ID cursor, or ``None`` for the first page.
        :param limit: Maximum number of rows, between 1 and 1000.
        :returns: ``(session_id, live_status)`` pairs; status can be unknown (``None``).
        """
        ...

    @abstractmethod
    def list_conversations_by_runner_id(
        self,
        runner_id: str,
    ) -> list[Conversation]:
        """
        Return all conversations bound to the given ``runner_id``.

        Used by the runner tunnel's connect/disconnect callbacks to
        find the sessions pinned to a specific runner. Implementations
        must be read-after-write consistent with ``set_runner_id`` /
        ``replace_runner_id``: the connect callback fires seconds after
        a session is bound, so an eventually-consistent source (e.g. a
        search index) misses just-created sessions and the runner's
        claude-native terminal bootstrap silently never fires.

        :param runner_id: Runner identifier, e.g.
            ``"runner_token_a1b2c3d4..."``.
        :returns: List of :class:`Conversation` entities with
            ``runner_id`` matching the given value.
        """
        ...

    @abstractmethod
    def set_host_id(
        self,
        conversation_id: str,
        host_id: str,
        workspace: str | None = None,
        git_branch: str | None = None,
        worktree: str | None = None,
    ) -> Conversation:
        """
        Set the host that launched (or should launch) the runner.

        Used when the server asks a host to spawn a runner for
        an existing session. Last-write-wins semantics (like
        :meth:`replace_runner_id`).

        Runner/host binding is live state, not conversation activity, so
        this must NOT bump ``updated_at`` (it drives sidebar ordering
        and the unread dot).

        :param conversation_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param host_id: Host identifier, e.g.
            ``"host_a1b2c3d4..."``.
        :param workspace: Optional canonical absolute workspace
            path to set together with ``host_id``, e.g.
            ``"/Users/corey/projects/myapp"``. Required when the
            existing row has ``workspace=NULL`` (DB constraint
            ``ck_conversations_workspace_required_for_host``).
            ``None`` leaves the workspace untouched.
        :param git_branch: Optional git branch checked out in a
            server-created worktree, e.g. ``"feature/login"``. Set
            when binding an existing session to a freshly created
            worktree (the fork resume path). ``None`` preserves the
            branch on the same host/workspace, but clears it when
            the host or an explicitly supplied workspace changes.
        :param worktree: Optional session working tree when it differs
            from ``workspace``, e.g. a worktree placed inside the
            project entry. ``None`` follows ``git_branch``: kept on the
            same host/workspace, cleared when the binding moves.
        :returns: The updated :class:`Conversation`.
        :raises ConversationNotFoundError: If no conversation row
            with ``conversation_id`` exists.
        """
        ...

    @abstractmethod
    def set_worktree(
        self,
        conversation_id: str,
        worktree: str | None,
    ) -> Conversation:
        """
        Set (or clear) a session's recorded working tree.

        Used by host-less paths that copy a placement field without a
        host binding — a terminal transfer onto an unbound replacement
        row. A ``host_id``-changing bind goes through
        :meth:`set_host_id` instead.

        :param conversation_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param worktree: Session working tree path, or ``None`` to
            clear it.
        :returns: The updated :class:`Conversation`.
        :raises ConversationNotFoundError: If no conversation row
            with ``conversation_id`` exists.
        """
        ...

    @abstractmethod
    def set_external_session_id(
        self,
        conversation_id: str,
        value: str,
    ) -> Conversation:
        """
        Set the runtime-native session id this conversation wraps.

        Captured by the wrapper bridge from the underlying runtime
        (Claude Code's session uuid today; Codex / Pi tomorrow) and
        recorded once per conversation so ``--resume`` can recover
        the external session's prior transcript on a fresh runner.

        Idempotent: setting the same value as the existing one is a
        no-op (the wrapper bridge may observe the value across
        multiple hook events). Setting a different value when the
        field is already populated raises ``ValueError`` —
        wrappers should observe exactly one runtime-native session
        id per conversation, and a divergent write signals a bug
        worth surfacing loudly rather than silently overwriting.

        :param conversation_id: Conversation to update, e.g.
            ``"conv_abc123"``.
        :param value: Runtime-native session id captured by the
            wrapper bridge, e.g. a Claude Code session uuid
            ``"a1b2c3d4-..."``.
        :returns: The updated :class:`Conversation`.
        :raises ConversationNotFoundError: If no conversation row
            with ``conversation_id`` exists.
        :raises ValueError: If
            ``conversation.external_session_id`` is already set
            to a different value.
        """
        ...

    @abstractmethod
    def create_session_with_agent(
        self,
        *,
        agent_id: str,
        agent_name: str,
        agent_bundle_location: str,
        agent_description: str | None,
        title: str | None = None,
        labels: dict[str, str] | None = None,
        reasoning_effort: str | None = None,
        model_override: str | None = None,
        workspace: str | None = None,
        worktree: str | None = None,
        terminal_launch_args: list[str] | None = None,
        parent_conversation_id: str | None = None,
        runner_id: str | None = None,
        project_id: str | None = None,
        host_id: str | None = None,
        inference_snapshot: dict[str, Any] | None = None,
        created_by: str | None = None,
    ) -> CreatedSession:
        """
        Atomically create a session and its session-scoped agent.

        The conversation row and agent row are written in one
        database transaction. If either insert or any label write
        fails, none of the database rows are committed.

        :param agent_id: Pre-generated agent id, e.g.
            ``"ag_abc123"``.
        :param agent_name: Human-readable agent name from the
            uploaded spec, e.g. ``"code-assistant"``.
        :param agent_bundle_location: Artifact-store key for the
            uploaded bundle, e.g. ``"ag_abc123/a1b2c3d4"``.
        :param agent_description: Optional spec description.
            ``None`` when the spec omits it.
        :param title: Optional session title, e.g.
            ``"debugging auth flow"``.
        :param labels: Optional initial guardrails labels,
            e.g. ``{"env": "test"}``. ``None`` writes no labels.
        :param reasoning_effort: Optional per-session
            reasoning-effort hint, e.g. ``"high"``. ``None``
            means use the agent default.
        :param workspace: Optional starting cwd to record on the
            session, e.g. ``"/Users/corey/projects/myapp"``.
            ``None`` leaves the column NULL.
        :param worktree: Optional working tree when it differs from
            ``workspace``, e.g. a worktree placed inside the project
            entry. ``None`` leaves the column NULL.
        :param terminal_launch_args: Optional pass-through CLI args
            for a native terminal wrapper (claude / codex), e.g.
            ``["--dangerously-skip-permissions"]``. ``None`` leaves
            the column NULL.
        :param parent_conversation_id: Optional parent conversation
            id, e.g. ``"conv_parent1"``. When set, the new session
            is a sub-agent child of that conversation
            (``kind="sub_agent"``) and inherits its spawn-tree root.
            ``None`` creates a top-level session.
        :param runner_id: Optional runner binding to persist at
            creation time, e.g. ``"runner_abc123"``. Child sessions
            inherit the parent's binding through this field.
        :param host_id: Optional external host the session binds to,
            e.g. ``"host_a1b2c3d4..."``. Requires a non-``None``
            ``workspace``. ``None`` leaves the session unbound.
        :param created_by: Identity of the creating user, recorded on the
            session-scoped agent so its code can only be mutated by the
            owner. ``None`` in single-user mode.
        :returns: The committed conversation and agent entities.
        :raises ConversationNotFoundError: If
            ``parent_conversation_id`` is set but no such
            conversation exists.
        :raises Exception: Backend errors propagate after rollback.
        """
        ...

    @abstractmethod
    def fork_conversation(
        self,
        source_conversation_id: str,
        *,
        title: str | None = None,
        agent_id: str | None = None,
        cloned_agent_name: str | None = None,
        cloned_agent_bundle_location: str | None = None,
        cloned_agent_description: str | None = None,
        copy_model_settings: bool = True,
        copy_terminal_launch_args: bool = True,
        override_model_override: str | None = None,
        override_model_override_set: bool = False,
        override_reasoning_effort: str | None = None,
        override_reasoning_effort_set: bool = False,
        override_terminal_launch_args: list[str] | None = None,
        override_terminal_launch_args_set: bool = False,
        dropped_label_keys: frozenset[str] = frozenset(),
        extra_labels: dict[str, str] | None = None,
        carry_history_into_native: bool = False,
        resume_source_native_session: bool = True,
        presentation_labels: dict[str, str] | None = None,
        up_to_response_id: str | None = None,
        project_id: str | None = None,
        file_id_map: Mapping[str, str] | None = None,
        created_by: str | None = None,
    ) -> Conversation:
        """
        Deep-copy a conversation and its items into a new conversation.

        Creates a new top-level conversation
        (``kind="default"``, ``parent_conversation_id=None``)
        with the source's ``reasoning_effort``, then copies every
        item (with fresh IDs) preserving position order,
        ``response_id``, ``type``, ``status``, and ``data``. FTS
        records are inserted for each copied item. The entire
        operation runs in a single transaction for atomicity.

        :param source_conversation_id: ID of the conversation to
            fork, e.g. ``"conv_abc123"``.
        :param title: Title for the new conversation. When
            ``None``, defaults to ``"Fork of <source_title>"``
            (or ``"Fork of <source_id>"`` when the source has no
            title).
        :param agent_id: Agent ID to bind the fork to. When ``None``,
            the fork inherits the source's ``agent_id``. With
            ``cloned_agent_bundle_location`` set, a fresh agent row is
            created with this id; otherwise it must name an existing
            agent, whose ``session_id`` is repointed at the fork.
        :param cloned_agent_name: Name for the cloned agent row.
            Required when ``cloned_agent_bundle_location`` is set.
        :param cloned_agent_bundle_location: When set, clone this
            bundle into a new session-scoped agent row created
            atomically in the fork transaction, so a fork failure rolls
            it back instead of orphaning a ``session_id IS NULL``
            built-in. ``None`` keeps the legacy bind-existing behavior.
        :param cloned_agent_description: Optional description for the
            cloned agent row. Ignored unless
            ``cloned_agent_bundle_location`` is set.
        :param copy_model_settings: When ``True`` (default), copy the
            source's ``model_override`` / ``reasoning_effort``. When
            ``False``, both are left ``None`` so the fork falls back to
            the bound agent's defaults — used when the fork switches to
            an agent in a different provider family, where the source's
            model id is meaningless (a model is provider-bound).
        :param copy_terminal_launch_args: When ``True`` (default), copy the
            source's ``terminal_launch_args``. When ``False``, the fork starts
            with none — used when the fork switches to a different CLI, where
            the source's flags are meaningless or rejected (e.g. Claude Code's
            ``--permission-mode`` would make ``pi`` exit at launch).
        :param override_model_override: Explicit ``model_override`` for the
            fork, applied only when ``override_model_override_set`` — then it
            supersedes the ``copy_model_settings`` copy.
        :param override_model_override_set: Whether the caller chose an
            explicit ``model_override`` (fork dialog's model picker).
        :param override_reasoning_effort: Explicit ``reasoning_effort`` for
            the fork, applied only when ``override_reasoning_effort_set``.
        :param override_reasoning_effort_set: Whether the caller chose an
            explicit ``reasoning_effort``.
        :param override_terminal_launch_args: Explicit
            ``terminal_launch_args`` for the fork (the permission-mode
            selector), applied only when ``override_terminal_launch_args_set``
            — then it supersedes the ``copy_terminal_launch_args`` copy.
        :param override_terminal_launch_args_set: Whether the caller chose
            explicit launch args.
        :param dropped_label_keys: Source labels to NOT copy onto the fork,
            beyond the always-dropped instance-scoped set (e.g. the
            permission-mode / codex-bypass label keys when the dialog picks
            explicit launch args, so a stale copied label can't shadow the
            freshly chosen mode).
        :param extra_labels: Labels stamped on the fork AFTER copy/drop, so a
            deliberate opt-in beats the always-drop rule — e.g. the fork
            dialog re-arming the DANGEROUS ``codex_native.bypass_sandbox``
            label (the only path that sets it; the source's is always dropped).
        :param carry_history_into_native: When ``True``, stamp
            :data:`FORK_CARRY_HISTORY_LABEL_KEY` on the fork so a native
            target harness rebuilds its transcript (clone the source's
            native transcript, or build from the copied Omnigent items) instead
            of starting fresh. Set by the route only for native targets whose
            harness can replay fork history.
        :param resume_source_native_session: When ``True`` (default), a
            full fork of a source with a native session stamps
            :data:`FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY` so the runner
            clones the source's local native transcript. ``False`` when the
            fork switches to an agent in a DIFFERENT provider family: the
            source's native transcript is the wrong format for the target
            harness, so the directive is skipped and the runner builds the
            native transcript from the copied Omnigent items instead.
        :param presentation_labels: When not ``None``, replace the source's
            harness-presentation labels (``omnigent.ui`` /
            ``omnigent.wrapper``) on the clone with these. Used when the
            fork switches agents so the clone's UI mode matches the TARGET
            harness: a native target supplies ``{ui: terminal, wrapper:
            ...}``; an SDK target supplies ``{}`` (drop them → chat mode).
            ``None`` (default, same-agent fork) keeps the copied labels.
        :param up_to_response_id: When set, copy only the items up to and
            including the last item of this response (by position), e.g.
            ``"resp_abc123"`` — a "fork from this response" truncation.
            A truncated fork drops the source's external-session fork
            directive so a native target rebuilds its transcript from the
            truncated items instead of resuming the full source
            transcript; when the response is the source's last one, the
            copy is equivalent to a full fork and the directive is kept.
            ``None`` (default) copies the full history.
        :param project_id: First-class project to file the fork into
            (``metadata.project_id``), or ``None`` (default) to leave it
            unfiled. The caller resolves whether the fork keeps the
            source's project — projects are owner-private, so the route
            passes the source's id only when the forker owns it.
        :param file_id_map: Source file id → fork-owned file id for the
            session-scoped file resources the caller copies into the fork.
            Copied items that reference a mapped id (message attachment
            blocks, file resource events) are rewritten to the fork's copy,
            so the fork never references files it does not own. ``None`` or
            empty leaves every copied payload verbatim.
        :param created_by: Identity of the forking user, recorded on the
            cloned session-scoped agent so its code can only be mutated by
            the owner. ``None`` in single-user mode or when no clone is made.
        :returns: The newly created :class:`Conversation`.
        :raises LookupError: If no conversation with
            *source_conversation_id* exists.
        :raises ValueError: If *up_to_response_id* is set but no item in
            the source conversation has that ``response_id``.
        """
        ...

    def list_session_roots_for_agent(self, agent_id: str, limit: int) -> list[str]:
        """
        Up to *limit* distinct spawn-tree roots of sessions that use *agent_id*.

        Forks of one user's sessions share an agent row, so several roots can
        use it. Lets a caller be authorized by READ on any of them. Reads a
        bounded number of rows, so an agent used very widely may return fewer.
        Default: none (stores without the lookup authorize against one root only).

        :param agent_id: Agent id, e.g. ``"0f1a2b3c..."``.
        :param limit: Most roots to return, e.g. ``50``.
        :returns: Distinct root conversation ids, at most *limit*.
        """
        del agent_id, limit
        return []

    def count_sessions_for_agent(self, agent_id: str, cap: int) -> int:
        """
        Count sessions that use *agent_id*, reading at most *cap* of them.

        Any kind, archived included. Default: one bounded conversation page.

        :param agent_id: Agent id, e.g. ``"0f1a2b3c..."``.
        :param cap: Most sessions to count, e.g. ``101``.
        :returns: The count, at most *cap*.
        """
        page = self.list_conversations(
            limit=cap, kind=None, agent_id=agent_id, include_archived=True
        )
        return len(page.data)

    @abstractmethod
    def has_other_live_session_in_workspace(
        self,
        *,
        host_id: str,
        workspace: str,
        exclude_conversation_id: str,
        include_subdirectories: bool = False,
        include_unclosed_archived: bool = False,
    ) -> bool:
        """
        Is another non-archived conversation working in this ``(host_id, workspace)``?

        Sessions routinely share one directory: a fork reusing the source's
        worktree, or several sessions attached to the same existing worktree
        via the picker. Worktree cleanup must not remove a directory a live
        session still runs in, so this is the "is it in use?" gate.

        Each other row is compared by its effective worktree
        ``worktree ?? workspace``: an entry-started session's launch directory
        is the project entry, not the directory being cleaned up, so counting
        it as a sharer would block every worktree cleanup.

        When ``include_unclosed_archived`` is true, an archived session
        counts until its current revision's CLI release is complete. A Host
        policy may keep an archived CLI running.

        :param host_id: Host owning the worktree, e.g. ``"host_a1b2..."``.
        :param workspace: Absolute worktree path, e.g. ``"/w/feature-login"``.
        :param exclude_conversation_id: The conversation being deleted or
            archived — its own row must not count as "another session".
        :param include_subdirectories: Also protect sessions inside this worktree root.
        :param include_unclosed_archived: Protect archived peers whose CLI
            teardown has not completed for the current revision.
        :returns: ``True`` when at least one other live conversation
            references the pair, else ``False``.
        """
        ...

    @abstractmethod
    async def delete_conversation(self, conversation_id: str) -> bool:
        """
        Delete a conversation and all its items.

        Async because it may need to cancel in-flight responses
        in the conversation first.

        :param conversation_id: Unique conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: ``True`` if the conversation existed,
            ``False`` otherwise.
        """
        ...
