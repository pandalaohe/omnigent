"""Cutover holds, turn admissions, receipts and the release CAS.

One owner-scoped book, the same tables the usage feed uses, and one lock order:
the hold row is taken before the feed row, never the other way round. See the
contract (spec §2, §5, §6, §11) for the shapes this module produces.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from omnigent.db.account_authority import require_active_account
from omnigent.db.db_models import (
    SqlUsageAccountBindingActivation,
    SqlUsageAccountBindingRevision,
    SqlUsageCutoverHold,
    SqlUsageCutoverReceipt,
    SqlUsageEvent,
    SqlUsageFeed,
    SqlUsageRouteAttestation,
    SqlUsageTurnAdmission,
    SqlUsageUnit,
    current_workspace_id,
)
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    run_write_transaction,
)
from omnigent.usage_export.models import AccountHint, thread_key
from omnigent.usage_export.store import UsageExportError

FEED_HARNESSES = frozenset({"claude-native", "claude-sdk", "codex-native", "codex-sdk"})
ACTIVE_HOLD_STATES = frozenset({"held", "drained", "invalidated"})
HOLD_STATES = frozenset({"none", "held", "drained", "released", "invalidated", "abandoned"})
RECEIPT_KINDS = frozenset({"drain", "release", "abandon", "late_violation"})
_MAX_READS_PER_MINUTE = 60
_MAX_RECEIPT_CALLS = 5000
_MAX_READ_LIMIT = 50


def _now() -> int:
    return int(time.time())


def _iso(epoch: int | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _epoch(value: str | None) -> int | None:
    if value is None:
        return None
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(owner_id: str, generation: str | None) -> bytes:
    return hashlib.sha256(_json([owner_id, generation or ""]).encode("utf-8")).digest()


def _canonical_digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _identifier(value: object, code: str = "invalid_domain") -> str:
    """A wire id: 1..256 UTF-8 bytes, never re-encoded."""
    if not isinstance(value, str) or not 1 <= len(value.encode("utf-8")) <= 256:
        raise UsageExportError(400, code)
    return value


def _harness(value: str) -> str:
    if value not in FEED_HARNESSES:
        raise UsageExportError(400, "invalid_domain")
    return value


def _terminal_state(threads: list[dict[str, Any]], zero_thread: bool) -> list[str]:
    kinds = {entry["kind"] for thread in threads for entry in thread.get("evidence", [])}
    if zero_thread:
        kinds.add("executor_end")
    return sorted(kinds)


def _admission_entry(row: SqlUsageTurnAdmission, *, linked_call: bool) -> dict[str, Any]:
    """Render one admission in the r4 §11.2 shape from its stored evidence."""
    evidence = json.loads(row.terminal_evidence or "{}")
    threads_by_key: dict[str, list[dict[str, Any]]] = evidence.get("threads", {})
    turn_ended_at: str | None = evidence.get("turn_ended_at")
    threads: list[dict[str, Any]] = []
    for key, entries in threads_by_key.items():
        physical_session_id, _, subagent = key.partition("/")
        threads.append(
            {
                "physical_session_id": physical_session_id,
                "physical_subagent_id": subagent or None,
                "evidence": sorted(
                    (
                        {
                            "kind": entry["kind"],
                            "reported_at": entry["reported_at"],
                            "source_ref": entry.get("source_ref"),
                        }
                        for entry in entries
                    ),
                    key=lambda item: (item["kind"], item["reported_at"]),
                ),
            }
        )
    threads.sort(
        key=lambda item: (
            item["physical_session_id"],
            item["physical_subagent_id"] is not None,
            item["physical_subagent_id"] or "",
        )
    )
    if threads:
        terminal = all(thread["evidence"] for thread in threads)
        states = sorted({entry["kind"] for thread in threads for entry in thread["evidence"]})
    else:
        terminal = turn_ended_at is not None and not linked_call
        states = ["executor_end"] if terminal else []
    return {
        "admission_id": row.admission_id,
        "session_id": row.session_id,
        "admitted_at": _iso(row.admitted_at),
        "binding_revision": row.binding_revision,
        "binding_digest": row.binding_digest.hex() if row.binding_digest else None,
        "terminal": terminal,
        "terminal_state": states,
        "turn_ended_at": turn_ended_at,
        "threads": threads,
    }


def append_receipt(
    session: Session,
    workspace_id: int,
    owner_digest: bytes,
    hold: SqlUsageCutoverHold,
    kind: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    """Append one receipt to the domain chain; the caller holds the locks."""
    previous = UsageCutoverStore.latest_receipt(session, workspace_id, owner_digest, hold)
    generation = hold.generation or 0
    receipt_seq = (
        session.execute(
            select(func.max(SqlUsageCutoverReceipt.receipt_seq)).where(
                SqlUsageCutoverReceipt.workspace_id == workspace_id,
                SqlUsageCutoverReceipt.owner_digest == owner_digest,
                SqlUsageCutoverReceipt.host_id == hold.host_id,
                SqlUsageCutoverReceipt.harness == hold.harness,
                SqlUsageCutoverReceipt.generation == generation,
            )
        ).scalar_one_or_none()
        or 0
    )
    feed = UsageCutoverStore.feed_row(session, workspace_id, owner_digest)
    receipt = {
        **body,
        "receipt_schema_version": 1,
        "receipt_id": uuid.uuid4().hex,
        "receipt_seq": receipt_seq + 1,
        "kind": kind,
        "owner_scope_id": feed.owner_scope_id,
        "feed_id": feed.feed_id,
        "feed_epoch": feed.feed_epoch,
        "domain": {"host_id": hold.host_id, "harness": hold.harness},
        "generation": generation,
        "hold_created_at": _iso(hold.created_at),
        "created_at": _iso(_now()),
        "previous_receipt_digest": previous.digest.hex() if previous is not None else None,
    }
    digest = _canonical_digest(receipt)
    session.add(
        SqlUsageCutoverReceipt(
            workspace_id=workspace_id,
            owner_digest=owner_digest,
            host_id=hold.host_id,
            harness=hold.harness,
            generation=generation,
            receipt_seq=receipt["receipt_seq"],
            receipt_id=receipt["receipt_id"],
            kind=kind,
            payload=_json(receipt),
            digest=bytes.fromhex(digest),
            created_at=_now(),
        )
    )
    return {**receipt, "digest": digest}


class UsageCutoverStore:
    """Owner book for holds, admissions, receipts and the release CAS."""

    def __init__(self, storage_location: str) -> None:
        engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.usage_cutover_store", immediate=True
        )

    # ---- plumbing -------------------------------------------------------

    def _write(self, owner_id: str, operation: str, callback: Callable[..., Any]) -> Any:
        if not isinstance(owner_id, str) or not owner_id:
            raise UsageExportError(400, "invalid_owner")
        workspace_id = current_workspace_id()

        def locked(session: Session) -> Any:
            generation = require_active_account(session, owner_id)
            return callback(session, workspace_id, _digest(owner_id, generation))

        return run_write_transaction(self._session, operation, locked)

    def _read(self, owner_id: str, operation: str, callback: Callable[..., Any]) -> Any:
        if not isinstance(owner_id, str) or not owner_id:
            raise UsageExportError(400, "invalid_owner")
        workspace_id = current_workspace_id()
        with self._session(operation) as session:
            generation = require_active_account(session, owner_id)
            return callback(session, workspace_id, _digest(owner_id, generation))

    @staticmethod
    def _hold(
        session: Session,
        workspace_id: int,
        host_id: str,
        harness: str,
        *,
        for_update: bool = False,
        for_share: bool = False,
    ) -> SqlUsageCutoverHold | None:
        statement = select(SqlUsageCutoverHold).where(
            SqlUsageCutoverHold.workspace_id == workspace_id,
            SqlUsageCutoverHold.host_id == host_id,
            SqlUsageCutoverHold.harness == harness,
        )
        if for_update:
            statement = statement.with_for_update()
        elif for_share:
            statement = statement.with_for_update(read=True)
        return session.execute(statement).scalar_one_or_none()

    @staticmethod
    def feed_row(session: Session, workspace_id: int, owner_digest: bytes) -> SqlUsageFeed:
        feed = session.execute(
            select(SqlUsageFeed)
            .where(
                SqlUsageFeed.workspace_id == workspace_id,
                SqlUsageFeed.owner_digest == owner_digest,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if feed is None:
            feed = SqlUsageFeed(
                workspace_id=workspace_id,
                owner_digest=owner_digest,
                owner_scope_id=uuid.uuid4().hex,
                feed_id=uuid.uuid4().hex,
                feed_epoch=1,
                signing_key=b"\0" * 32,
                next_sequence=1,
                read_timestamps="[]",
                progress="{}",
                grant_positions="{}",
            )
            session.add(feed)
            session.flush()
        return feed

    @staticmethod
    def _ensure_hold_row(
        session: Session, workspace_id: int, owner_digest: bytes, host_id: str, harness: str
    ) -> SqlUsageCutoverHold:
        row = UsageCutoverStore._hold(session, workspace_id, host_id, harness)
        if row is None:
            row = SqlUsageCutoverHold(
                workspace_id=workspace_id,
                host_id=host_id,
                harness=harness,
                owner_digest=owner_digest,
                state="none",
                refused_count=0,
                row_version=0,
            )
            session.add(row)
            session.flush()
        return row

    @staticmethod
    def _charge_read(feed: SqlUsageFeed) -> None:
        now = time.time()
        timestamps = [stamp for stamp in json.loads(feed.read_timestamps) if stamp > now - 60]
        if len(timestamps) >= _MAX_READS_PER_MINUTE:
            raise UsageExportError(
                429,
                "usage_read_rate_limited",
                retry_after=max(1, int(61 + timestamps[0] - now)),
            )
        timestamps.append(now)
        feed.read_timestamps = _json(timestamps)

    @staticmethod
    def _snapshot(row: SqlUsageCutoverHold) -> dict[str, Any]:
        return {
            "domain": {"host_id": row.host_id, "harness": row.harness},
            "state": row.state,
            "active": row.state in ACTIVE_HOLD_STATES,
            "generation": row.generation,
            "hold_created_at": _iso(row.created_at),
            "released_at": _iso(row.released_at),
            "state_changed_at": _iso(row.state_changed_at),
            "refused_count": row.refused_count,
            "row_version": row.row_version,
        }

    # ---- association and detection (observation append) -----------------

    @staticmethod
    def lock_domain_hold(
        session: Session, workspace_id: int, host_id: str, harness: str
    ) -> SqlUsageCutoverHold | None:
        """Take the domain's hold row before the feed row (§2.3 lock order)."""
        return UsageCutoverStore._hold(session, workspace_id, host_id, harness, for_share=True)

    @staticmethod
    def _merge_unit_evidence(
        row: SqlUsageTurnAdmission, observation: Any
    ) -> tuple[bool, str | None]:
        """Record the unit's terminal evidence on its admission.

        Returns ``(admission_terminal, turn_ended_at)``.
        """
        evidence = json.loads(row.terminal_evidence or "{}")
        threads: dict[str, list[dict[str, Any]]] = evidence.get("threads", {})
        now_iso = _iso(_now())
        own_key: str | None = None
        if observation.physical_session_id is not None:
            own_key = thread_key(observation.physical_session_id, observation.physical_subagent_id)
            threads.setdefault(own_key, [])
        for entry in observation.terminal:
            existing = {
                (item["kind"], item.get("source_ref"))
                for item in threads.setdefault(entry.key, [])
            }
            if (entry.evidence, None) in existing:
                continue
            threads[entry.key].append(
                {"kind": entry.evidence, "reported_at": now_iso, "source_ref": None}
            )
        evidence["threads"] = threads
        terminal = bool(threads) and all(entries for entries in threads.values())
        if terminal and evidence.get("turn_ended_at") is None:
            evidence["turn_ended_at"] = now_iso
        row.terminal_evidence = _json(evidence)
        if terminal:
            row.terminal_state = _json(
                sorted({item["kind"] for entries in threads.values() for item in entries})
            )
        return terminal, evidence.get("turn_ended_at")

    @staticmethod
    def associate_unit(
        session: Session,
        workspace_id: int,
        owner_digest: bytes,
        observation: Any,
        hold: SqlUsageCutoverHold | None,
        *,
        binding_session_id: str | None,
        margin_seconds: int = 60,
    ) -> dict[str, Any]:
        """Resolve the unit's admission and detect an after-hold unadmitted call.

        The runner proposes the admission; the server checks it here and never
        returns an error for a failed check (the producer's delivery loop would
        stop). A failed check leaves the unit unadmitted.
        """
        result: dict[str, Any] = {
            "admission": None,
            "association": "unadmitted",
            "call_started_not_before": None,
            "call_start_basis": None,
            "account_hint": None,
            "ended_at": None,
            "after_hold": False,
        }
        admission_id = observation.admission_id
        if admission_id is not None:
            row = session.get(SqlUsageTurnAdmission, (workspace_id, owner_digest, admission_id))
            failure: str | None = None
            if row is None:
                failure = "admission_missing"
            elif row.session_id != binding_session_id:
                failure = "admission_session_mismatch"
            elif row.host_id != observation.host_id or row.harness != observation.harness:
                failure = "admission_domain_mismatch"
            elif observation.occurred_at is not None and (
                (_epoch(observation.occurred_at) or 0) < row.admitted_at - margin_seconds
            ):
                failure = "admission_precedes_call"
            elif observation.thread_kind == "main":
                main_key = observation.turn_id or observation.producer_unit_id
                if row.main_turn_key is None:
                    row.main_turn_key = main_key
                elif row.main_turn_key != main_key:
                    failure = "admission_reused"
            if failure is None:
                assert row is not None
                if row.route_check is None and observation.route_check is not None:
                    row.route_check = observation.route_check
                    if row.route_check == "mismatch":
                        row.account_hint = None
                        row.binding_revision = None
                        row.binding_digest = None
                terminal, turn_ended_at = UsageCutoverStore._merge_unit_evidence(row, observation)
                del terminal
                ended_at = None
                if turn_ended_at is not None:
                    evidence = json.loads(row.terminal_evidence or "{}")
                    threads = evidence.get("threads", {})
                    if observation.physical_session_id is not None:
                        own_key = thread_key(
                            observation.physical_session_id,
                            observation.physical_subagent_id,
                        )
                        own = threads.get(own_key, [])
                        if own and all(entries for entries in threads.values()):
                            ended_at = _iso(_now())
                if ended_at is not None and row.terminal_state is not None:
                    result["ended_at"] = ended_at
                result["admission"] = row
                result["association"] = "linked"
                result["call_started_not_before"] = _iso(row.admitted_at)
                result["call_start_basis"] = "turn_admission"
                if row.route_check == "match" and row.account_hint:
                    result["account_hint"] = json.loads(row.account_hint)
            else:
                result["association"] = failure
        if hold is not None and result["admission"] is None:
            hold_time = hold.created_at
            after_hold = observation.occurred_at is None or (
                hold_time is not None
                and (_epoch(observation.occurred_at) or 0) >= hold_time - margin_seconds
            )
            if after_hold and hold.state in {"held", "drained"}:
                # Continuous detection (§4.2 step 2): the window stays closed.
                hold.state = "invalidated"
                hold.state_changed_at = _now()
                hold.row_version += 1
                result["after_hold"] = True
            elif after_hold and hold.state in {"released", "abandoned"}:
                released_at = hold.released_at
                if (
                    released_at is None
                    or (_epoch(observation.occurred_at) or 0) <= released_at + margin_seconds
                ):
                    result["after_hold"] = True
        return result

    # ---- hold machine ---------------------------------------------------

    def hold_status(self, owner_id: str, host_id: str, harness: str) -> dict[str, Any]:
        canonical_host = _identifier(host_id)
        feed_harness = _harness(harness)

        def read(session: Session, workspace_id: int, owner_digest: bytes) -> dict[str, Any]:
            row = self._hold(session, workspace_id, canonical_host, feed_harness)
            if row is None:
                return {
                    "domain": {"host_id": canonical_host, "harness": feed_harness},
                    "state": "none",
                    "active": False,
                    "generation": None,
                    "hold_created_at": None,
                    "released_at": None,
                    "state_changed_at": None,
                    "refused_count": 0,
                    "row_version": 0,
                    "latest_receipt": None,
                    "open_admissions": 0,
                }
            receipt = self.latest_receipt(session, workspace_id, owner_digest, row)
            open_admissions = session.execute(
                select(func.count())
                .select_from(SqlUsageTurnAdmission)
                .where(
                    SqlUsageTurnAdmission.workspace_id == workspace_id,
                    SqlUsageTurnAdmission.owner_digest == owner_digest,
                    SqlUsageTurnAdmission.host_id == canonical_host,
                    SqlUsageTurnAdmission.harness == feed_harness,
                    SqlUsageTurnAdmission.terminal_state.is_(None),
                )
            ).scalar_one()
            snapshot = self._snapshot(row)
            snapshot["latest_receipt"] = (
                None
                if receipt is None
                else {
                    "receipt_id": receipt.receipt_id,
                    "receipt_seq": receipt.receipt_seq,
                    "kind": receipt.kind,
                    "digest": receipt.digest.hex(),
                }
            )
            snapshot["open_admissions"] = int(open_admissions)
            return snapshot

        return self._read(owner_id, "read_usage_cutover_hold", read)

    def create_hold(
        self,
        owner_id: str,
        host_id: str,
        harness: str,
        generation: int,
        *,
        goal_active: bool = False,
    ) -> dict[str, Any]:
        canonical_host = _identifier(host_id)
        feed_harness = _harness(harness)
        if type(generation) is not int or generation < 1:
            raise UsageExportError(400, "invalid_generation")
        if goal_active:
            raise UsageExportError(409, "codex_goal_active")
        # The row exists before the hold transaction so an admission can take
        # its share lock and never miss a concurrent create (§2.3).
        self._write(
            owner_id,
            "ensure_usage_cutover_hold_row",
            lambda session, workspace_id, owner_digest: self._ensure_hold_row(
                session, workspace_id, owner_digest, canonical_host, feed_harness
            ),
        )

        def write(session: Session, workspace_id: int, _owner_digest: bytes) -> dict[str, Any]:
            row = self._hold(session, workspace_id, canonical_host, feed_harness, for_update=True)
            if row is None:
                raise UsageExportError(409, "hold_generation_conflict")
            now = _now()
            if row.state in ACTIVE_HOLD_STATES:
                if row.generation == generation:
                    return self._snapshot(row)
                raise UsageExportError(409, "hold_generation_conflict")
            if row.generation is not None and generation <= row.generation:
                raise UsageExportError(409, "hold_generation_conflict")
            row.generation = generation
            row.state = "held"
            row.created_at = now
            row.released_at = None
            row.state_changed_at = now
            row.row_version += 1
            return self._snapshot(row)

        return self._write(owner_id, "create_usage_cutover_hold", write)

    def active_hold(self, host_id: str, harness: str | None) -> dict[str, Any] | None:
        """Gate predicate: the active hold of a domain, or of any domain on a host."""
        workspace_id = current_workspace_id()
        statement = select(SqlUsageCutoverHold).where(
            SqlUsageCutoverHold.workspace_id == workspace_id,
            SqlUsageCutoverHold.host_id == host_id,
            SqlUsageCutoverHold.state.in_(ACTIVE_HOLD_STATES),
        )
        if harness is not None:
            statement = statement.where(SqlUsageCutoverHold.harness == harness)
        with self._session("read_active_usage_cutover_hold") as session:
            row = session.execute(statement).scalars().first()
            if row is None:
                return None
            return {
                "host_id": row.host_id,
                "harness": row.harness,
                "generation": row.generation,
                "state": row.state,
            }

    # ---- bindings and route attestation --------------------------------

    def load_binding_revision(self, owner_id: str, document: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(document, dict):
            raise UsageExportError(400, "invalid_binding_document")
        revision = document.get("config_revision")
        if document.get("schema_version") != 1 or type(revision) is not int or revision < 1:
            raise UsageExportError(400, "invalid_binding_document")
        encoded = _json(document)
        digest = hashlib.sha256(encoded.encode("utf-8")).digest()

        def write(session: Session, workspace_id: int, owner_digest: bytes) -> dict[str, Any]:
            row = session.get(
                SqlUsageAccountBindingRevision,
                (workspace_id, owner_digest, revision),
            )
            if row is None:
                row = SqlUsageAccountBindingRevision(
                    workspace_id=workspace_id,
                    owner_digest=owner_digest,
                    config_revision=revision,
                    document=encoded,
                    digest=digest,
                    created_at=_now(),
                )
                session.add(row)
            elif row.digest != digest:
                raise UsageExportError(409, "binding_revision_conflict")
            return {
                "config_revision": revision,
                "digest": row.digest.hex(),
                "active": self._activation(session, workspace_id, owner_digest)
                == (revision, row.digest),
            }

        return self._write(owner_id, "load_usage_binding_revision", write)

    @staticmethod
    def _activation(
        session: Session, workspace_id: int, owner_digest: bytes
    ) -> tuple[int, bytes] | None:
        row = session.get(SqlUsageAccountBindingActivation, (workspace_id, owner_digest))
        if row is None:
            return None
        return row.config_revision, row.digest

    def activate_binding_revision(
        self, owner_id: str, revision: int, digest: str
    ) -> dict[str, Any]:
        try:
            wanted = bytes.fromhex(digest)
        except (TypeError, ValueError) as exc:
            raise UsageExportError(400, "invalid_binding_digest") from exc
        if type(revision) is not int or revision < 1 or len(wanted) != 32:
            raise UsageExportError(400, "invalid_binding_revision")

        def write(session: Session, workspace_id: int, owner_digest: bytes) -> dict[str, Any]:
            row = session.get(
                SqlUsageAccountBindingRevision, (workspace_id, owner_digest, revision)
            )
            if row is None or row.digest != wanted:
                raise UsageExportError(404, "binding_revision_not_found")
            activation = session.get(
                SqlUsageAccountBindingActivation, (workspace_id, owner_digest)
            )
            if activation is None:
                session.add(
                    SqlUsageAccountBindingActivation(
                        workspace_id=workspace_id,
                        owner_digest=owner_digest,
                        config_revision=revision,
                        digest=wanted,
                        activated_at=_now(),
                    )
                )
            else:
                activation.config_revision = revision
                activation.digest = wanted
                activation.activated_at = _now()
            return {"config_revision": revision, "digest": wanted.hex(), "active": True}

        return self._write(owner_id, "activate_usage_binding_revision", write)

    def record_route_attestation(
        self,
        owner_id: str,
        host_id: str,
        harness: str,
        billing_route: str,
        route_ref: str,
    ) -> dict[str, Any]:
        canonical_host = _identifier(host_id)
        feed_harness = _harness(harness)
        if billing_route not in {"direct_provider", "gateway", "other"}:
            raise UsageExportError(400, "invalid_route_attestation")
        if not isinstance(route_ref, str) or not 1 <= len(route_ref.encode("utf-8")) <= 256:
            raise UsageExportError(400, "invalid_route_attestation")

        def write(session: Session, workspace_id: int, owner_digest: bytes) -> dict[str, Any]:
            row = session.get(
                SqlUsageRouteAttestation, (workspace_id, canonical_host, feed_harness)
            )
            if row is None:
                row = SqlUsageRouteAttestation(
                    workspace_id=workspace_id,
                    host_id=canonical_host,
                    harness=feed_harness,
                    owner_digest=owner_digest,
                    billing_route=billing_route,
                    route_ref=route_ref,
                    updated_at=_now(),
                )
                session.add(row)
            else:
                row.billing_route = billing_route
                row.route_ref = route_ref
                row.updated_at = _now()
            return {
                "host_id": canonical_host,
                "harness": feed_harness,
                "billing_route": billing_route,
                "route_ref": route_ref,
            }

        return self._write(owner_id, "record_usage_route_attestation", write)

    def route_attestation(self, owner_id: str, harness: str) -> dict[str, Any] | None:
        feed_harness = _harness(harness)
        workspace_id = current_workspace_id()
        with self._session("read_usage_route_attestation") as session:
            generation = require_active_account(session, owner_id)
            owner_digest = _digest(owner_id, generation)
            row = (
                session.execute(
                    select(SqlUsageRouteAttestation).where(
                        SqlUsageRouteAttestation.workspace_id == workspace_id,
                        SqlUsageRouteAttestation.owner_digest == owner_digest,
                        SqlUsageRouteAttestation.harness == feed_harness,
                    )
                )
                .scalars()
                .first()
            )
            if row is None:
                return None
            return {
                "host_id": row.host_id,
                "harness": row.harness,
                "billing_route": row.billing_route,
                "route_ref": row.route_ref,
            }

    @staticmethod
    def _hint_at(
        session: Session,
        workspace_id: int,
        owner_digest: bytes,
        host_id: str,
        harness: str,
        admitted_at: int,
    ) -> tuple[dict[str, Any] | None, int | None, bytes | None]:
        """Freeze the account hint for one admission at ``admitted_at``."""
        activation = UsageCutoverStore._activation(session, workspace_id, owner_digest)
        if activation is None:
            return None, None, None
        revision, digest = activation
        row = session.get(SqlUsageAccountBindingRevision, (workspace_id, owner_digest, revision))
        if row is None or row.digest != digest:
            return None, None, None
        attestation = (
            session.execute(
                select(SqlUsageRouteAttestation).where(
                    SqlUsageRouteAttestation.workspace_id == workspace_id,
                    SqlUsageRouteAttestation.host_id == host_id,
                    SqlUsageRouteAttestation.harness == harness,
                )
            )
            .scalars()
            .first()
        )
        if attestation is None:
            return None, revision, digest
        document = json.loads(row.document)
        revoked = {
            item.get("binding_version")
            for item in document.get("revocations", [])
            if item.get("revoked_at") is not None
            and (_epoch(item["revoked_at"]) or 0) <= admitted_at
        }
        applies = [host_id, harness, attestation.billing_route, attestation.route_ref]
        matches: list[dict[str, Any]] = []
        for binding in document.get("bindings", []):
            if binding.get("binding_version") in revoked:
                continue
            if any(list(entry) != applies for entry in binding.get("applies_to", [])):
                continue
            valid_from = _epoch(binding.get("valid_from"))
            valid_until = _epoch(binding.get("valid_until"))
            if valid_from is None or valid_from > admitted_at:
                continue
            if valid_until is not None and valid_until <= admitted_at:
                continue
            matches.append(binding)
        if len(matches) != 1:
            return None, revision, digest
        binding = matches[0]
        hint = {
            "provider": binding["provider"],
            "billing_kind": binding["billing_kind"],
            "billing_route": binding["billing_route"],
            "route_ref": binding["route_ref"],
            "opaque_account_ref": binding["opaque_account_ref"],
            "ref_generation": binding["ref_generation"],
            "basis": binding["basis"],
            "binding_version": binding["binding_version"],
        }
        try:
            AccountHint.model_validate(hint)
        except ValueError as exc:  # pragma: no cover - documents are validated on load
            raise UsageExportError(400, "invalid_binding_document") from exc
        return hint, revision, digest

    # ---- admissions -----------------------------------------------------

    def admit_turn(
        self,
        owner_id: str,
        *,
        admission_id: str,
        session_id: str | None,
        host_id: str | None,
        harness: str | None,
        source: str,
    ) -> dict[str, Any]:
        """Write the admission for a turn the gates just allowed.

        Refuses (and records nothing) while the domain, or any domain of the
        session's host when the harness is still undecided, is held.
        """
        canonical_admission = _identifier(admission_id, "invalid_admission_id")
        canonical_session = _identifier(session_id, "invalid_session_id") if session_id else None
        canonical_host = _identifier(host_id) if host_id else None
        feed_harness = _harness(harness) if harness else None
        if canonical_host is not None and feed_harness is not None:
            self._write(
                owner_id,
                "ensure_usage_cutover_hold_row",
                lambda session, workspace_id, owner_digest: self._ensure_hold_row(
                    session, workspace_id, owner_digest, canonical_host, feed_harness
                ),
            )

        def write(session: Session, workspace_id: int, owner_digest: bytes) -> dict[str, Any]:
            now = _now()
            if canonical_host is not None:
                statement = (
                    select(SqlUsageCutoverHold)
                    .where(
                        SqlUsageCutoverHold.workspace_id == workspace_id,
                        SqlUsageCutoverHold.host_id == canonical_host,
                        SqlUsageCutoverHold.state.in_(ACTIVE_HOLD_STATES),
                        *(
                            [SqlUsageCutoverHold.harness == feed_harness]
                            if feed_harness is not None
                            else []
                        ),
                    )
                    .with_for_update(read=True)
                )
                active = session.execute(statement).scalars().first()
                if active is not None:
                    raise UsageExportError(
                        409,
                        "usage_cutover_hold",
                        {
                            "host_id": active.host_id,
                            "harness": active.harness,
                            "generation": active.generation,
                        },
                    )
            hint = None
            revision = None
            digest = None
            if canonical_host is not None and feed_harness is not None:
                hint, revision, digest = self._hint_at(
                    session, workspace_id, owner_digest, canonical_host, feed_harness, now
                )
            row = SqlUsageTurnAdmission(
                workspace_id=workspace_id,
                owner_digest=owner_digest,
                admission_id=canonical_admission,
                session_id=canonical_session,
                host_id=canonical_host,
                harness=feed_harness,
                admitted_at=now,
                source=source,
                binding_revision=revision,
                binding_digest=digest,
                account_hint=_json(hint) if hint is not None else None,
                route_check=None,
                main_turn_key=None,
                terminal_state=None,
                terminal_evidence="{}",
            )
            session.add(row)
            return {
                "admission_id": canonical_admission,
                "admitted_at": _iso(now),
                "source": source,
                "account_hint": hint,
            }

        return self._write(owner_id, "admit_usage_turn", write)

    def admission_snapshot(self, owner_id: str, admission_id: str) -> dict[str, Any] | None:
        canonical = _identifier(admission_id, "invalid_admission_id")

        def read(session: Session, workspace_id: int, owner_digest: bytes) -> Any:
            row = session.get(SqlUsageTurnAdmission, (workspace_id, owner_digest, canonical))
            if row is None:
                return None
            return {
                "admission_id": row.admission_id,
                "session_id": row.session_id,
                "host_id": row.host_id,
                "harness": row.harness,
                "admitted_at": _iso(row.admitted_at),
                "source": row.source,
                "binding_revision": row.binding_revision,
                "account_hint": json.loads(row.account_hint) if row.account_hint else None,
                "route_check": row.route_check,
                "main_turn_key": row.main_turn_key,
                "terminal_state": json.loads(row.terminal_state) if row.terminal_state else None,
            }

        return self._read(owner_id, "read_usage_turn_admission", read)

    def record_route_check(
        self, owner_id: str, admission_id: str, route_check: str
    ) -> dict[str, Any]:
        canonical = _identifier(admission_id, "invalid_admission_id")
        if route_check not in {"match", "mismatch"}:
            raise UsageExportError(400, "invalid_route_check")

        def write(session: Session, workspace_id: int, owner_digest: bytes) -> dict[str, Any]:
            row = session.get(SqlUsageTurnAdmission, (workspace_id, owner_digest, canonical))
            if row is None:
                raise UsageExportError(404, "usage_admission_not_found")
            if row.route_check is not None:
                return {"admission_id": canonical, "route_check": row.route_check}
            row.route_check = route_check
            if route_check == "mismatch":
                row.account_hint = None
                row.binding_revision = None
                row.binding_digest = None
            return {"admission_id": canonical, "route_check": route_check}

        return self._write(owner_id, "record_usage_route_check", write)

    # ---- receipts -------------------------------------------------------

    @staticmethod
    def latest_receipt(
        session: Session,
        workspace_id: int,
        owner_digest: bytes,
        hold: SqlUsageCutoverHold,
    ) -> SqlUsageCutoverReceipt | None:
        return session.execute(
            select(SqlUsageCutoverReceipt)
            .where(
                SqlUsageCutoverReceipt.workspace_id == workspace_id,
                SqlUsageCutoverReceipt.owner_digest == owner_digest,
                SqlUsageCutoverReceipt.host_id == hold.host_id,
                SqlUsageCutoverReceipt.harness == hold.harness,
            )
            .order_by(
                SqlUsageCutoverReceipt.generation.desc(),
                SqlUsageCutoverReceipt.receipt_seq.desc(),
            )
            .limit(1)
        ).scalar_one_or_none()

    @staticmethod
    def _domain_sequence(
        session: Session,
        workspace_id: int,
        owner_digest: bytes,
        host: str,
        harness: str,
    ) -> int | None:
        return session.execute(
            select(func.max(SqlUsageEvent.sequence)).where(
                SqlUsageEvent.workspace_id == workspace_id,
                SqlUsageEvent.owner_digest == owner_digest,
                SqlUsageEvent.host_id == host,
                SqlUsageEvent.harness == harness,
            )
        ).scalar_one_or_none()

    def receipts_page(
        self,
        owner_id: str,
        host_id: str,
        harness: str,
        generation: int,
        after_seq: int,
        limit: int,
    ) -> dict[str, Any]:
        """The r4 §11.1 receipts page for one domain generation."""
        canonical_host = _identifier(host_id, "usage_cutover_bad_request")
        try:
            feed_harness = _harness(harness)
        except UsageExportError as exc:
            raise UsageExportError(400, "usage_cutover_bad_request") from exc
        if type(generation) is not int or generation < 1:
            raise UsageExportError(400, "usage_cutover_bad_request")
        if type(after_seq) is not int or after_seq < 0:
            raise UsageExportError(400, "usage_cutover_bad_request")
        if type(limit) is not int or not 1 <= limit <= _MAX_READ_LIMIT:
            raise UsageExportError(400, "usage_cutover_bad_request")

        def read(session: Session, workspace_id: int, owner_digest: bytes) -> dict[str, Any]:
            hold = self._hold(session, workspace_id, canonical_host, feed_harness)
            history = bool(
                hold is not None and (hold.generation is not None or hold.state != "none")
            )
            if not history:
                raise UsageExportError(404, "usage_cutover_domain_not_found")
            rows = (
                session.execute(
                    select(SqlUsageCutoverReceipt)
                    .where(
                        SqlUsageCutoverReceipt.workspace_id == workspace_id,
                        SqlUsageCutoverReceipt.owner_digest == owner_digest,
                        SqlUsageCutoverReceipt.host_id == canonical_host,
                        SqlUsageCutoverReceipt.harness == feed_harness,
                        SqlUsageCutoverReceipt.generation == generation,
                        SqlUsageCutoverReceipt.receipt_seq > after_seq,
                    )
                    .order_by(SqlUsageCutoverReceipt.receipt_seq)
                    .limit(limit + 1)
                )
                .scalars()
                .all()
            )
            if not rows and (hold is None or hold.generation != generation):
                raise UsageExportError(404, "usage_cutover_generation_not_found")
            feed = self.feed_row(session, workspace_id, owner_digest)
            self._charge_read(feed)
            page_rows = rows[:limit]
            return {
                "receipt_page_schema_version": 1,
                "domain": {"harness": feed_harness, "host_id": canonical_host},
                "generation": generation,
                "receipts": [
                    {**json.loads(row.payload), "digest": row.digest.hex()} for row in page_rows
                ],
                "has_more": len(rows) > limit,
                "next_after_seq": (page_rows[-1].receipt_seq if page_rows else after_seq),
            }

        return self._read(owner_id, "page_usage_cutover_receipts", read)

    def _append_receipt(
        self,
        session: Session,
        workspace_id: int,
        owner_digest: bytes,
        hold: SqlUsageCutoverHold,
        kind: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        return append_receipt(session, workspace_id, owner_digest, hold, kind, body)

    # ---- drain receipt --------------------------------------------------

    @staticmethod
    def _merge_thread_evidence(
        session: Session,
        workspace_id: int,
        owner_digest: bytes,
        host_id: str,
        harness: str,
        facts: list[dict[str, Any]],
    ) -> tuple[list[SqlUsageTurnAdmission], list[str]]:
        """Merge the host's reported thread evidence into the admission rows."""
        codes: list[str] = []
        rows: list[SqlUsageTurnAdmission] = []
        for entry in facts:
            admission_id = entry.get("admission_id")
            if not isinstance(admission_id, str):
                codes.append("host_facts_mismatch")
                continue
            try:
                canonical = _identifier(admission_id, "host_facts_mismatch")
            except UsageExportError:
                codes.append("host_facts_mismatch")
                continue
            row = session.get(SqlUsageTurnAdmission, (workspace_id, owner_digest, canonical))
            if row is None or row.host_id != host_id or row.harness != harness:
                codes.append("host_facts_mismatch")
                continue
            evidence = json.loads(row.terminal_evidence or "{}")
            threads: dict[str, list[dict[str, Any]]] = evidence.get("threads", {})
            for thread in entry.get("threads", []):
                physical_session_id = thread.get("physical_session_id")
                physical_subagent_id = thread.get("physical_subagent_id")
                if not isinstance(physical_session_id, str):
                    codes.append("host_facts_mismatch")
                    continue
                key = thread_key(physical_session_id, physical_subagent_id)
                reported: list[dict[str, Any]] = []
                for item in thread.get("evidence", []):
                    kind = item.get("kind")
                    if kind not in {
                        "terminal_record",
                        "stop_hook",
                        "executor_end",
                        "writer_exit",
                    }:
                        codes.append("host_facts_mismatch")
                        continue
                    source_ref = item.get("source_ref")
                    if source_ref is not None and (
                        not isinstance(source_ref, str)
                        or not 1 <= len(source_ref.encode("utf-8")) <= 256
                    ):
                        codes.append("host_facts_mismatch")
                        continue
                    reported.append(
                        {
                            "kind": kind,
                            "reported_at": _iso(_now()),
                            "source_ref": source_ref,
                        }
                    )
                existing = {
                    (item["kind"], item.get("source_ref")) for item in threads.get(key, [])
                }
                for item in reported:
                    if (item["kind"], item.get("source_ref")) in existing:
                        continue
                    threads.setdefault(key, []).append(item)
            turn_ended_at = entry.get("turn_ended_at")
            if turn_ended_at is not None and evidence.get("turn_ended_at") is None:
                evidence["turn_ended_at"] = turn_ended_at
            evidence["threads"] = threads
            row.terminal_evidence = _json(evidence)
            row.terminal_state = _json(
                _terminal_state(
                    [{"evidence": entries} for entries in threads.values()],
                    zero_thread=not threads,
                )
            )
            rows.append(row)
        return rows, codes

    def submit_drain(
        self,
        owner_id: str,
        host_id: str,
        harness: str,
        generation: int,
        facts: dict[str, Any],
    ) -> dict[str, Any]:
        """Verify the host's drain facts, decide the result and store the receipt."""
        canonical_host = _identifier(host_id)
        feed_harness = _harness(harness)
        if not isinstance(facts, dict):
            raise UsageExportError(400, "invalid_receipt_facts")

        def write(session: Session, workspace_id: int, owner_digest: bytes) -> dict[str, Any]:
            hold = self._hold(session, workspace_id, canonical_host, feed_harness, for_update=True)
            if hold is None or hold.generation != generation:
                raise UsageExportError(409, "generation_mismatch")
            if hold.state not in {"held", "drained"}:
                raise UsageExportError(409, "not_drained")
            feed = self.feed_row(session, workspace_id, owner_digest)
            del feed
            last_sequence = self._domain_sequence(
                session, workspace_id, owner_digest, canonical_host, feed_harness
            )
            calls = facts.get("calls", [])
            if not isinstance(calls, list):
                raise UsageExportError(400, "invalid_receipt_facts")
            codes = [code for code in facts.get("failure_codes", []) if isinstance(code, str)]
            if len(calls) > _MAX_RECEIPT_CALLS:
                codes.append("receipt_window_overflow")
            admission_rows, admission_codes = self._merge_thread_evidence(
                session,
                workspace_id,
                owner_digest,
                canonical_host,
                feed_harness,
                [entry for entry in facts.get("admissions", []) if isinstance(entry, dict)],
            )
            codes.extend(admission_codes)
            # r5: a call names the server-held association, and only a linked
            # call may name one.
            linked_admissions = {
                entry["admission_id"]
                for entry in calls
                if isinstance(entry, dict) and isinstance(entry.get("admission_id"), str)
            }
            known_ids = {
                row.admission_id
                for row in session.execute(
                    select(SqlUsageTurnAdmission).where(
                        SqlUsageTurnAdmission.workspace_id == workspace_id,
                        SqlUsageTurnAdmission.owner_digest == owner_digest,
                        SqlUsageTurnAdmission.host_id == canonical_host,
                        SqlUsageTurnAdmission.harness == feed_harness,
                    )
                )
                .scalars()
                .all()
            }
            verified_calls: list[dict[str, Any]] = []
            for entry in calls:
                if not isinstance(entry, dict):
                    codes.append("host_facts_mismatch")
                    continue
                call_key = entry.get("call_key") or {}
                meter_epoch = call_key.get("meter_epoch")
                producer_unit_id = call_key.get("producer_unit_id")
                if not isinstance(meter_epoch, str) or not isinstance(producer_unit_id, str):
                    codes.append("host_facts_mismatch")
                    continue
                unit_digest = hashlib.sha256(
                    _json([canonical_host, feed_harness, meter_epoch, producer_unit_id]).encode(
                        "utf-8"
                    )
                ).digest()
                unit = session.get(SqlUsageUnit, (workspace_id, owner_digest, unit_digest))
                state = entry.get("state")
                server_block = entry.get("server")
                admission = entry.get("admission")
                named_admission = entry.get("admission_id")
                linked = isinstance(admission, dict) and admission.get("state") == "linked"
                if linked != isinstance(named_admission, str):
                    codes.append("host_facts_mismatch")
                    continue
                if isinstance(named_admission, str) and named_admission not in known_ids:
                    codes.append("host_facts_mismatch")
                    continue
                if isinstance(server_block, dict):
                    if unit is None or (
                        server_block.get("original_event_id"),
                        server_block.get("latest_event_id"),
                        server_block.get("sequence"),
                        server_block.get("revision"),
                    ) != (
                        unit.original_event_id,
                        unit.latest_event_id,
                        unit.latest_sequence,
                        unit.revision,
                    ):
                        codes.append("host_facts_mismatch")
                        continue
                    # r5: a committed call equals its unit row's admission.
                    if unit.admission_id != named_admission:
                        codes.append("host_facts_mismatch")
                        continue
                    state = "terminal_acked"
                elif unit is not None and state in {"unsent", "path_pending"}:
                    # A lost ACK: the server already holds this observation.
                    state = "terminal_acked"
                if state == "unadmitted_after_hold":
                    codes.append("unadmitted_call_after_hold")
                verified_calls.append({**entry, "admission_id": named_admission, "state": state})

            admissions = [
                _admission_entry(row, linked_call=row.admission_id in linked_admissions)
                for row in admission_rows
            ]
            known = {
                row.admission_id: row
                for row in session.execute(
                    select(SqlUsageTurnAdmission).where(
                        SqlUsageTurnAdmission.workspace_id == workspace_id,
                        SqlUsageTurnAdmission.owner_digest == owner_digest,
                        SqlUsageTurnAdmission.host_id == canonical_host,
                        SqlUsageTurnAdmission.harness == feed_harness,
                    )
                )
                .scalars()
                .all()
            }
            for admission_id, row in known.items():
                if any(entry["admission_id"] == admission_id for entry in admissions):
                    continue
                admissions.append(
                    _admission_entry(row, linked_call=admission_id in linked_admissions)
                )
            admissions.sort(key=lambda entry: entry["admitted_at"] or "")
            counts = facts.get("counts", {}) if isinstance(facts.get("counts"), dict) else {}
            counts = {
                **counts,
                "admissions_open_at_hold": len(admissions),
                "admissions_terminal": sum(1 for entry in admissions if entry["terminal"]),
                "calls_window": len(verified_calls),
                "calls_unadmitted_after_hold": sum(
                    1 for entry in verified_calls if entry["state"] == "unadmitted_after_hold"
                ),
            }
            timed_out = facts.get("timed_out") is True
            drained = not codes and all(entry["terminal"] for entry in admissions)
            if counts.get("outbox_pending_observations"):
                drained = False
            if counts.get("open_threads"):
                drained = False
            if not drained and not codes:
                codes.append("calls_open")
            if drained:
                hold.state = "drained"
            elif "unadmitted_call_after_hold" in codes:
                hold.state = "invalidated"
            else:
                hold.state = "held"
            hold.state_changed_at = _now()
            hold.row_version += 1
            return self._append_receipt(
                session,
                workspace_id,
                owner_digest,
                hold,
                "drain",
                {
                    "result": "drained" if drained else "timeout" if timed_out else "failed",
                    "failure_codes": [] if drained else sorted(set(codes)),
                    "last_committed_sequence": last_sequence,
                    "detection": facts.get("detection"),
                    "admissions": admissions,
                    "calls": verified_calls,
                    "counts": counts,
                    "release": None,
                },
            )

        return self._write(owner_id, "submit_usage_drain_receipt", write)

    # ---- release and abandon -------------------------------------------

    def release(
        self,
        owner_id: str,
        host_id: str,
        harness: str,
        generation: int,
        consumer_receipt: dict[str, Any],
        cutover_at: str | None,
        rescan: dict[str, Any],
    ) -> dict[str, Any]:
        canonical_host = _identifier(host_id)
        feed_harness = _harness(harness)

        def write(session: Session, workspace_id: int, owner_digest: bytes) -> dict[str, Any]:
            hold = self._hold(session, workspace_id, canonical_host, feed_harness, for_update=True)
            if hold is None or hold.generation != generation:
                raise UsageExportError(409, "generation_mismatch")
            if hold.state == "invalidated":
                return {"__error__": "drain_invalidated"}
            if rescan.get("calls_unadmitted_after_hold") or rescan.get(
                "outbox_pending_observations"
            ):
                hold.state = "invalidated"
                hold.state_changed_at = _now()
                hold.row_version += 1
                return {"__error__": "drain_invalidated"}
            if hold.state != "drained":
                raise UsageExportError(409, "not_drained")
            latest = self.latest_receipt(session, workspace_id, owner_digest, hold)
            if (
                latest is None
                or latest.kind != "drain"
                or json.loads(latest.payload).get("result") != "drained"
            ):
                raise UsageExportError(409, "not_drained")
            drained = {**json.loads(latest.payload), "digest": latest.digest.hex()}
            raw_last = drained.get("last_committed_sequence")
            last_sequence: int | None = raw_last if isinstance(raw_last, int) else None
            current = self._domain_sequence(
                session, workspace_id, owner_digest, canonical_host, feed_harness
            )
            if last_sequence is not None and current is not None and current > last_sequence:
                hold.state = "invalidated"
                hold.state_changed_at = _now()
                hold.row_version += 1
                return {"__error__": "drain_invalidated"}
            if consumer_receipt.get("kind") != "drained":
                raise UsageExportError(409, "consumer_receipt_mismatch")
            if (
                consumer_receipt.get("owner_scope_id"),
                consumer_receipt.get("feed_id"),
                consumer_receipt.get("feed_epoch"),
                consumer_receipt.get("domain", {}).get("host_id"),
                consumer_receipt.get("domain", {}).get("harness"),
                consumer_receipt.get("generation"),
            ) != (
                drained["owner_scope_id"],
                drained["feed_id"],
                drained["feed_epoch"],
                canonical_host,
                feed_harness,
                generation,
            ):
                raise UsageExportError(409, "consumer_receipt_mismatch")
            if consumer_receipt.get("producer_receipt_digest") != drained["digest"]:
                raise UsageExportError(409, "consumer_receipt_mismatch")
            acked = consumer_receipt.get("acked_through_sequence")
            if last_sequence is not None and (not isinstance(acked, int) or acked < last_sequence):
                raise UsageExportError(409, "consumer_not_acked")
            if consumer_receipt.get("legacy_frozen") is not True:
                raise UsageExportError(409, "consumer_not_acked")
            if consumer_receipt.get("authority_published") is not True or (
                type(consumer_receipt.get("authority_revision")) is not int
                or consumer_receipt["authority_revision"] < 1
                or not isinstance(consumer_receipt.get("authority_digest"), str)
                or len(consumer_receipt["authority_digest"]) != 64
            ):
                raise UsageExportError(409, "consumer_not_acked")
            grant_id = consumer_receipt.get("usage_read_grant_id")
            if last_sequence is not None:
                positions = json.loads(
                    self.feed_row(session, workspace_id, owner_digest).grant_positions or "{}"
                )
                presented = positions.get(grant_id) if isinstance(grant_id, str) else None
                if not isinstance(presented, int) or presented < last_sequence:
                    raise UsageExportError(409, "consumer_not_acked")
            cutover_epoch = _epoch(cutover_at)
            if (
                cutover_epoch is None
                or cutover_epoch % 3600 != 0
                or _epoch(drained["created_at"]) is None
                or cutover_epoch < (_epoch(drained["created_at"]) or 0)
                or cutover_epoch > _now()
            ):
                raise UsageExportError(409, "cutover_time_invalid")
            hold.state = "released"
            hold.released_at = _now()
            hold.state_changed_at = hold.released_at
            hold.row_version += 1
            return self._append_receipt(
                session,
                workspace_id,
                owner_digest,
                hold,
                "release",
                {
                    "result": "released",
                    "failure_codes": [],
                    "last_committed_sequence": last_sequence,
                    "detection": drained.get("detection"),
                    "admissions": drained.get("admissions", []),
                    "calls": drained.get("calls", []),
                    "counts": drained.get("counts", {}),
                    "release": {
                        "cutover_at": cutover_at,
                        "consumer_receipt_digest": _canonical_digest(consumer_receipt),
                        "consumer_receipt": consumer_receipt,
                    },
                },
            )

        result = self._write(owner_id, "release_usage_cutover_hold", write)
        if isinstance(result, dict) and result.get("__error__"):
            # The invalidation above committed; only the refusal is reported.
            raise UsageExportError(409, result["__error__"])
        return result

    def abandon(
        self,
        owner_id: str,
        host_id: str,
        harness: str,
        generation: int,
        consumer_receipt: dict[str, Any],
        reason: str,
    ) -> dict[str, Any]:
        canonical_host = _identifier(host_id)
        feed_harness = _harness(harness)

        def write(session: Session, workspace_id: int, owner_digest: bytes) -> dict[str, Any]:
            hold = self._hold(session, workspace_id, canonical_host, feed_harness, for_update=True)
            if hold is None or hold.generation != generation:
                raise UsageExportError(409, "generation_mismatch")
            if hold.state not in ACTIVE_HOLD_STATES:
                raise UsageExportError(409, "not_drained")
            if (
                consumer_receipt.get("kind") != "abandon"
                or consumer_receipt.get("domain", {}).get("host_id") != canonical_host
                or consumer_receipt.get("domain", {}).get("harness") != feed_harness
                or consumer_receipt.get("generation") != generation
            ):
                raise UsageExportError(409, "consumer_receipt_mismatch")
            published = consumer_receipt.get("authority_published")
            restored_ok = isinstance(
                consumer_receipt.get("restored_authority_revision"), int
            ) and isinstance(consumer_receipt.get("restored_authority_digest"), str)
            if published is not False and not (published is True and restored_ok):
                raise UsageExportError(409, "consumer_receipt_mismatch")
            hold.state = "abandoned"
            hold.released_at = _now()
            hold.state_changed_at = hold.released_at
            hold.row_version += 1
            return self._append_receipt(
                session,
                workspace_id,
                owner_digest,
                hold,
                "abandon",
                {
                    "result": "abandoned",
                    "failure_codes": [],
                    "reason": reason,
                    "release": None,
                },
            )

        return self._write(owner_id, "abandon_usage_cutover_hold", write)
