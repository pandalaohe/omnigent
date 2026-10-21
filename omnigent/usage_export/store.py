"""Durable, owner-scoped usage feed with authenticated position cursors."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from omnigent.db.account_authority import require_active_account
from omnigent.db.db_models import (
    SqlUsageCallAllocation,
    SqlUsageEvent,
    SqlUsageFeed,
    SqlUsageProducer,
    SqlUsageUnit,
    current_workspace_id,
)
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    run_write_transaction,
)
from omnigent.usage_export.models import (
    HostProgress,
    UsageEvent,
    UsageObservation,
    thread_key,
)

_MAX_EVENT_BYTES = 32 * 1024
# Producer association evidence stays on the unit row; it is not part of
# the committed event the consumer reads.
_EVIDENCE_FIELDS = ("admission_id", "route_check", "terminal")
_MAX_PAGE_BYTES = 1024 * 1024
_IMMUTABLE_FIELDS = (
    "producer_unit_id",
    "meter_epoch",
    "host_id",
    "harness",
    "agent_id",
    "session_id",
    "measurement_kind",
    "harness_family",
    "identity_namespace",
    "physical_session_id",
    "transcript_session_id",
    "physical_subagent_id",
)


class UsageExportError(Exception):
    """Safe error contract for the HTTP route."""

    def __init__(
        self,
        status_code: int,
        code: str,
        detail: str | dict[str, Any] | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(detail or code)
        self.status_code = status_code
        self.code = code
        self.detail = detail
        self.retry_after = retry_after


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(*parts: str) -> bytes:
    return hashlib.sha256(_json(parts).encode("utf-8")).digest()


def _encoded_size(value: Any) -> int:
    return len(_json(value).encode("utf-8"))


def _event_from_payload(payload: str) -> dict[str, Any]:
    if len(payload.encode("utf-8")) > _MAX_EVENT_BYTES:
        raise UsageExportError(422, "export_record_too_large")
    try:
        event = UsageEvent.model_validate_json(payload).model_dump(mode="json")
        if _json(event) != payload:
            raise ValueError("noncanonical usage event")
    except (ValidationError, ValueError, TypeError) as exc:
        raise UsageExportError(422, "corrupt_usage_event") from exc
    return event


def _progress_from_json(raw: str) -> dict[str, dict[str, Any]]:
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict) or len(parsed) > 32:
            raise ValueError("invalid progress size")
        progress = {
            host_id: HostProgress.model_validate(value).model_dump(mode="json")
            for host_id, value in parsed.items()
        }
        if (
            any(
                not isinstance(host_id, str) or value["host_id"] != host_id
                for host_id, value in progress.items()
            )
            or _json(progress) != raw
        ):
            raise ValueError("invalid progress host")
    except (ValidationError, ValueError, TypeError) as exc:
        raise UsageExportError(422, "corrupt_usage_progress") from exc
    return progress


def _advance_progress(raw: str, event: dict[str, Any]) -> str:
    progress = _progress_from_json(raw)
    host_id = event["host_id"]
    previous = progress.get(host_id)
    last_sealed = previous["last_sealed_turn_at"] if previous is not None else None
    ended_at = event["ended_at"]
    if ended_at is not None and (
        last_sealed is None
        or datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
        > datetime.fromisoformat(last_sealed.replace("Z", "+00:00"))
    ):
        last_sealed = ended_at
    progress[host_id] = {
        "host_id": host_id,
        "last_sealed_turn_at": last_sealed,
        "state": "unknown",
        "observed_at": event["recorded_at"],
    }
    if len(progress) > 32:
        raise UsageExportError(422, "usage_host_progress_limit")
    return _json(progress)


def _url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _url_decode(value: str) -> bytes:
    if not value or len(value) > 4096:
        raise ValueError("invalid cursor length")
    decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    if _url_encode(decoded) != value:
        raise ValueError("noncanonical cursor encoding")
    return decoded


class UsageEventStore:
    """Append complete unit revisions and page a committed feed prefix."""

    def __init__(self, storage_location: str) -> None:
        engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.usage_event_store", immediate=True
        )

    @staticmethod
    def _feed(session: Session, workspace_id: int, owner_digest: bytes) -> SqlUsageFeed:
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
                signing_key=secrets.token_bytes(32),
                next_sequence=1,
                read_timestamps="[]",
                progress="{}",
            )
            session.add(feed)
            session.flush()
        return feed

    def _write(self, owner_id: str, operation: str, callback: Any) -> Any:
        self._validate_owner(owner_id)
        workspace_id = current_workspace_id()

        def locked(session: Session) -> Any:
            generation = require_active_account(session, owner_id)
            owner_digest = self._owner(owner_id, generation)
            feed = self._feed(session, workspace_id, owner_digest)
            return callback(session, feed, workspace_id, owner_digest)

        # On row creation PostgreSQL/MySQL may race; only the losing insertion
        # is replayed. Existing feed mutations use its row lock.
        for attempt in range(3):
            try:
                return run_write_transaction(self._session, operation, locked)
            except IntegrityError:
                if attempt == 2:
                    raise
        raise AssertionError("unreachable")

    @staticmethod
    def _validate_owner(owner_id: str) -> None:
        if not isinstance(owner_id, str) or not owner_id:
            raise UsageExportError(400, "invalid_owner")

    @staticmethod
    def _owner(owner_id: str, generation: str | None) -> bytes:
        return _digest(owner_id, generation or "")

    @staticmethod
    def _producer_id(producer_id: str) -> str:
        if not isinstance(producer_id, str) or len(producer_id.encode("utf-8")) > 256:
            raise UsageExportError(400, "invalid_producer_id")
        try:
            parsed = uuid.UUID(producer_id)
        except (ValueError, AttributeError) as exc:
            raise UsageExportError(400, "invalid_producer_id") from exc
        if parsed.version != 4:
            raise UsageExportError(400, "invalid_producer_id")
        return parsed.hex

    @staticmethod
    def _binding(binding: dict[str, Any]) -> dict[str, str | None]:
        required = {"session_id", "agent_id", "host_id", "harness", "parent_session_id"}
        if not isinstance(binding, dict) or set(binding) != required:
            raise UsageExportError(400, "invalid_producer_binding")
        result: dict[str, str | None] = {}
        for name in required:
            value = binding[name]
            if name == "parent_session_id" and value is None:
                result[name] = None
            elif not isinstance(value, str) or not value or len(value.encode("utf-8")) > 256:
                raise UsageExportError(400, "invalid_producer_binding")
            else:
                result[name] = value
        if result["harness"] not in {"codex-native", "codex-sdk", "claude-native", "claude-sdk"}:
            raise UsageExportError(400, "invalid_producer_binding")
        return result

    def register_producer(
        self, owner_id: str, producer_id: str, binding: dict[str, Any]
    ) -> dict[str, Any]:
        """Persist an immutable producer binding across session deletion."""
        self._validate_owner(owner_id)
        canonical_id = self._producer_id(producer_id)
        validated = self._binding(binding)
        producer_digest = _digest(canonical_id)
        encoded = _json(validated)

        def write(
            session: Session, feed: SqlUsageFeed, workspace_id: int, owner_digest: bytes
        ) -> dict[str, Any]:
            del feed
            row = session.get(SqlUsageProducer, (workspace_id, owner_digest, producer_digest))
            if row is None:
                session.add(
                    SqlUsageProducer(
                        workspace_id=workspace_id,
                        owner_digest=owner_digest,
                        producer_digest=producer_digest,
                        producer_id=canonical_id,
                        binding=encoded,
                    )
                )
            elif row.binding != encoded:
                raise UsageExportError(409, "producer_binding_conflict")
            return {"producer_id": canonical_id, **validated}

        return self._write(owner_id, "register_usage_producer", write)

    def get_producer(self, owner_id: str, producer_id: str) -> dict[str, Any] | None:
        """Look up a registered producer without consulting the session table."""
        self._validate_owner(owner_id)
        canonical_id = self._producer_id(producer_id)
        producer_digest = _digest(canonical_id)
        workspace_id = current_workspace_id()
        with self._session("get_usage_producer") as session:
            generation = require_active_account(session, owner_id)
            owner_digest = self._owner(owner_id, generation)
            row = session.get(SqlUsageProducer, (workspace_id, owner_digest, producer_digest))
            if row is None:
                return None
            return {"producer_id": row.producer_id, **json.loads(row.binding)}

    @staticmethod
    def _call_digest(observation: UsageObservation) -> bytes | None:
        if observation.logical_call_id is None:
            return None
        expected = (
            ("claude-code/message-id/v1", "claude-code/request-id/v1")
            if observation.harness_family == "claude"
            else ("codex-responses/response-id/v1",)
        )
        namespace = next(
            namespace
            for namespace in expected
            if any(
                ref.kind == "response"
                and ref.namespace == namespace
                and ref.id == observation.logical_call_id
                for ref in observation.source_refs
            )
        )
        return _digest(
            observation.host_id,
            observation.harness_family,
            namespace,
            observation.logical_call_id,
        )

    def append(self, owner_id: str, observation: UsageObservation) -> dict[str, Any]:
        """Append a new unit or its full replacement; identical replay is stable."""
        self._validate_owner(owner_id)
        unit_digest = _digest(
            observation.host_id,
            observation.harness,
            observation.meter_epoch,
            observation.producer_unit_id,
        )
        measurement = observation.model_dump(mode="json")
        measurement_json = _json(measurement)
        call_digest = self._call_digest(observation)

        def write(
            session: Session, feed: SqlUsageFeed, workspace_id: int, owner_digest: bytes
        ) -> dict[str, Any]:
            unit = session.get(SqlUsageUnit, (workspace_id, owner_digest, unit_digest))
            if unit is not None:
                old = json.loads(unit.observation)
                if any(old[field] != measurement[field] for field in _IMMUTABLE_FIELDS):
                    raise UsageExportError(409, "unit_identity_conflict")
                if old["logical_call_id"] is not None and (
                    measurement["logical_call_id"] != old["logical_call_id"]
                ):
                    raise UsageExportError(409, "call_identity_conflict")
                if unit.observation == measurement_json:
                    latest = session.get(
                        SqlUsageEvent, (workspace_id, owner_digest, unit.latest_sequence)
                    )
                    if latest is not None:
                        candidate = _event_from_payload(latest.payload)
                        if candidate["event_id"] == unit.latest_event_id:
                            return candidate
                    raise UsageExportError(422, "corrupt_usage_feed")
            recorded_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            previous_ended_at = None
            if unit is not None:
                previous = session.get(
                    SqlUsageEvent, (workspace_id, owner_digest, unit.latest_sequence)
                )
                if previous is not None:
                    previous_ended_at = _event_from_payload(previous.payload)["ended_at"]
            ended_at = previous_ended_at
            if ended_at is None and observation.physical_session_id is not None:
                own_thread = thread_key(
                    observation.physical_session_id, observation.physical_subagent_id
                )
                if any(entry.key == own_thread for entry in observation.terminal):
                    ended_at = recorded_at
            allocation = None
            if call_digest is not None:
                allocation = session.get(
                    SqlUsageCallAllocation, (workspace_id, owner_digest, call_digest)
                )
                if allocation is None:
                    allocation = SqlUsageCallAllocation(
                        workspace_id=workspace_id,
                        owner_digest=owner_digest,
                        call_digest=call_digest,
                        first_unit_digest=unit_digest,
                    )
                    session.add(allocation)
            request_count = (
                None if allocation is None else int(allocation.first_unit_digest == unit_digest)
            )
            if unit is not None and unit.request_count is not None and call_digest is None:
                raise UsageExportError(409, "call_identity_conflict")
            event_id = uuid.uuid4().hex
            original_id = unit.original_event_id if unit is not None else event_id
            sequence = feed.next_sequence
            event = {
                **{
                    key: value for key, value in measurement.items() if key not in _EVIDENCE_FIELDS
                },
                "event_id": event_id,
                "original_event_id": original_id,
                "kind": "correction" if unit is not None else "usage",
                "correction_of": original_id if unit is not None else None,
                "revision": unit.revision + 1 if unit is not None else 1,
                "sequence": sequence,
                "recorded_at": recorded_at,
                "request_count": request_count,
                "call_started_not_before": None,
                "call_start_basis": None,
                "ended_at": ended_at,
                "account_hint": None,
            }
            payload = _json(event)
            if len(payload.encode("utf-8")) > _MAX_EVENT_BYTES:
                raise UsageExportError(422, "export_record_too_large")
            snapshot = _advance_progress(feed.progress, event)
            feed.next_sequence += 1
            feed.progress = snapshot
            session.add(
                SqlUsageEvent(
                    workspace_id=workspace_id,
                    owner_digest=owner_digest,
                    sequence=sequence,
                    payload=payload,
                    snapshot=snapshot,
                    host_id=observation.host_id,
                    harness=observation.harness,
                )
            )
            if unit is None:
                session.add(
                    SqlUsageUnit(
                        workspace_id=workspace_id,
                        owner_digest=owner_digest,
                        unit_digest=unit_digest,
                        observation=measurement_json,
                        original_event_id=original_id,
                        latest_event_id=event_id,
                        latest_sequence=sequence,
                        revision=1,
                        request_count=request_count,
                    )
                )
            else:
                unit.observation = measurement_json
                unit.latest_event_id = event_id
                unit.latest_sequence = sequence
                unit.revision += 1
                unit.request_count = request_count
            return event

        return self._write(owner_id, "append_usage_observation", write)

    @staticmethod
    def _cursor(
        feed: SqlUsageFeed,
        workspace_id: int,
        owner_digest: bytes,
        position: int,
        fence: int | None,
    ) -> str:
        body = _json(
            {
                "w": workspace_id,
                "o": owner_digest.hex(),
                "i": feed.feed_id,
                "e": feed.feed_epoch,
                "p": position,
                "f": fence,
                "v": 1,
                "endpoint": "usage-events",
            }
        ).encode("utf-8")
        return _url_encode(body) + "." + _url_encode(hmac.digest(feed.signing_key, body, "sha256"))

    @staticmethod
    def _verify_cursor(
        session: Session, cursor: str, workspace_id: int, owner_digest: bytes, feed: SqlUsageFeed
    ) -> tuple[int, int | None]:
        try:
            encoded_body, encoded_mac = cursor.split(".")
            body = _url_decode(encoded_body)
            supplied_mac = _url_decode(encoded_mac)
            data = json.loads(body)
            cursor_workspace = data["w"]
            cursor_owner = bytes.fromhex(data["o"])
            if type(cursor_workspace) is not int or len(cursor_owner) != 32:
                raise ValueError("invalid scope")
            signing_feed = session.get(SqlUsageFeed, (cursor_workspace, cursor_owner))
            if signing_feed is None or not hmac.compare_digest(
                supplied_mac, hmac.digest(signing_feed.signing_key, body, "sha256")
            ):
                raise ValueError("invalid signature")
            if data["endpoint"] != "usage-events" or data["v"] != 1:
                raise ValueError("invalid endpoint")
        except (ValueError, KeyError, TypeError, AttributeError, UnicodeDecodeError) as exc:
            raise UsageExportError(400, "invalid_usage_cursor") from exc
        if cursor_workspace != workspace_id or cursor_owner != owner_digest:
            raise UsageExportError(403, "usage_cursor_scope_forbidden")
        position, fence = data.get("p"), data.get("f")
        if (
            data.get("i") != feed.feed_id
            or data.get("e") != feed.feed_epoch
            or type(position) is not int
            or position < 0
            or (fence is not None and (type(fence) is not int or fence < position))
            or position >= feed.next_sequence
            or (fence is not None and fence >= feed.next_sequence)
        ):
            raise UsageExportError(
                410,
                "usage_cursor_expired",
                {
                    "code": "usage_cursor_expired",
                    "owner_scope_id": feed.owner_scope_id,
                    "feed_id": feed.feed_id,
                    "feed_epoch": feed.feed_epoch,
                    "replay_from_cursor": UsageEventStore._cursor(
                        feed, workspace_id, owner_digest, 0, None
                    ),
                    "replay_coverage_from": None,
                    "gap_ranges": [],
                    "recovery_state": "genesis_replay_available",
                },
            )
        return position, fence

    def page(
        self,
        owner_id: str,
        cursor: str | None = None,
        limit: int = 500,
        grant_id: str | None = None,
    ) -> dict[str, Any]:
        """Read a fixed committed fence, charging the durable owner read budget."""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise UsageExportError(422, "invalid_usage_limit")
        self._validate_owner(owner_id)

        def read(
            session: Session, feed: SqlUsageFeed, workspace_id: int, owner_digest: bytes
        ) -> dict[str, Any]:
            position, cursor_fence = (
                self._verify_cursor(session, cursor, workspace_id, owner_digest, feed)
                if cursor is not None
                else (0, None)
            )
            now = time.time()
            timestamps = [t for t in json.loads(feed.read_timestamps) if t > now - 60]
            if len(timestamps) >= 60:
                raise UsageExportError(
                    429,
                    "usage_read_rate_limited",
                    retry_after=max(1, int(61 + timestamps[0] - now)),
                )
            timestamps.append(now)
            feed.read_timestamps = _json(timestamps)
            if grant_id is not None:
                positions = json.loads(feed.grant_positions or "{}")
                positions[grant_id] = max(int(positions.get(grant_id, 0)), position)
                feed.grant_positions = _json(positions)
            fence = cursor_fence if cursor_fence is not None else feed.next_sequence - 1
            rows = (
                session.execute(
                    select(SqlUsageEvent)
                    .where(
                        SqlUsageEvent.workspace_id == workspace_id,
                        SqlUsageEvent.owner_digest == owner_digest,
                        SqlUsageEvent.sequence > position,
                        SqlUsageEvent.sequence <= fence,
                    )
                    .order_by(SqlUsageEvent.sequence)
                    .limit(limit + 1)
                )
                .scalars()
                .all()
            )
            events: list[dict[str, Any]] = []
            last_sequence = position
            event_bytes = 0
            prefix_progress: dict[str, dict[str, Any]] | None = None
            for row in rows[:limit]:
                if row.sequence != last_sequence + 1:
                    raise UsageExportError(422, "corrupt_usage_event")
                event = _event_from_payload(row.payload)
                size = _encoded_size(event)
                if event_bytes + size > 950 * 1024:
                    break
                prefix_progress = _progress_from_json(row.snapshot)
                events.append(event)
                last_sequence = row.sequence
                event_bytes += size
            if not events and position < fence:
                raise UsageExportError(422, "corrupt_usage_event")
            if prefix_progress is None:
                if position == 0:
                    prefix_progress = {}
                else:
                    tail = session.get(SqlUsageEvent, (workspace_id, owner_digest, position))
                    if tail is None:
                        raise UsageExportError(422, "corrupt_usage_event")
                    _event_from_payload(tail.payload)
                    prefix_progress = _progress_from_json(tail.snapshot)
            has_more = last_sequence < fence
            next_cursor = self._cursor(
                feed, workspace_id, owner_digest, last_sequence, fence if has_more else None
            )
            result = {
                "schema_version": 2,
                "owner_scope_id": feed.owner_scope_id,
                "feed_id": feed.feed_id,
                "feed_epoch": feed.feed_epoch,
                "events": events,
                "next_cursor": next_cursor,
                "has_more": has_more,
                "replay_from_cursor": self._cursor(feed, workspace_id, owner_digest, 0, None),
                "replay_coverage_from": None,
                "retained_from_cursor": self._cursor(feed, workspace_id, owner_digest, 0, None),
                "host_progress": sorted(
                    prefix_progress.values(), key=lambda item: item["host_id"]
                ),
                "replaces_feed_id": None,
                "replaces_feed_epoch": None,
            }
            if _encoded_size(result) > _MAX_PAGE_BYTES:
                raise UsageExportError(422, "usage_page_too_large")
            return result

        return self._write(owner_id, "page_usage_events", read)
