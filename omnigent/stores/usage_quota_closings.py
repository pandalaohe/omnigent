"""Retain source observations closest to each real reset boundary."""

from __future__ import annotations

import json
from decimal import Decimal
from fractions import Fraction

from sqlalchemy import select
from sqlalchemy.orm import Session

from omnigent.db.account_authority import require_active_account
from omnigent.db.db_models import SqlUsageQuotaClosing, current_workspace_id
from omnigent.db.utils import run_write_transaction
from omnigent.stores.usage_snapshot_store import UsageSnapshotStore
from omnigent.usage.contract import UsageWindow
from omnigent.usage.value import Value, endpoint


class UsageQuotaClosingStore:
    def __init__(self, storage_location: str) -> None:
        self.sessions = UsageSnapshotStore(storage_location)

    @staticmethod
    def record_in_session(
        session: Session,
        owner: str,
        account: str,
        windows: list[UsageWindow],
        *,
        observed_at: int,
        source: str | None,
        polling_seconds: int | None = None,
    ) -> None:
        require_active_account(session, owner)
        UsageSnapshotStore.write_in_session(
            session, owner, "usage.quota_closings.lock", {"schema_version": 1}
        )
        for window in windows:
            if (
                window.resets_at is None
                or int(window.resets_at) != window.resets_at
                or observed_at >= window.resets_at
            ):
                continue
            key = (current_workspace_id(), owner, account, window.id, int(window.resets_at))
            old = session.get(SqlUsageQuotaClosing, key)
            if old is not None and old.observed_at > observed_at:
                continue
            remaining = None
            if window.limit_value is not None and window.used_value is not None:
                remaining = max(
                    Fraction(0),
                    Fraction(str(window.limit_value)) - Fraction(str(window.used_value)),
                )
            payload = dict(
                window.model_dump(mode="json"),
                remaining_value=None if remaining is None else endpoint(remaining),
                source=source,
                polling_seconds=polling_seconds,
                basis="pre_reset_observation",
                complete=False,
            )
            session.merge(
                SqlUsageQuotaClosing(
                    workspace_id=key[0],
                    owner_id=owner,
                    account_id=account,
                    window_id=window.id,
                    resets_at=key[4],
                    observed_at=observed_at,
                    payload=json.dumps(payload),
                )
            )

    def record(self, owner: str, account: str, windows: list[UsageWindow], **options) -> None:
        run_write_transaction(
            self.sessions._write_session(),
            "retain_quota_observation",
            lambda session: self.record_in_session(session, owner, account, windows, **options),
        )

    def read(self, owner: str, start: int, until: int, *, as_of: int) -> list[dict]:
        with self.sessions._read_session()("read_quota_closings") as session:
            rows = session.scalars(
                select(SqlUsageQuotaClosing)
                .where(
                    SqlUsageQuotaClosing.workspace_id == current_workspace_id(),
                    SqlUsageQuotaClosing.owner_id == owner,
                    SqlUsageQuotaClosing.resets_at >= start,
                    SqlUsageQuotaClosing.resets_at <= until,
                )
                .order_by(SqlUsageQuotaClosing.account_id, SqlUsageQuotaClosing.resets_at)
            ).all()
            result = []
            for row in rows:
                payload = json.loads(row.payload)
                value = Value(
                    unit="percent",
                    cohort_id=f"{row.account_id}:{row.window_id}:{row.resets_at}",
                    as_of=as_of,
                    reason="source_poll_interval_not_reported",
                )
                poll = payload.get("polling_seconds")
                percent = payload.get("percent_used")
                if (
                    percent is None
                    and payload.get("limit_value")
                    and payload.get("used_value") is not None
                ):
                    percent = endpoint(
                        Fraction(str(payload["used_value"]))
                        / Fraction(str(payload["limit_value"]))
                        * 100
                    )
                if poll and row.resets_at - row.observed_at <= 3 * poll and percent is not None:
                    value = value.model_copy(
                        update={
                            "state": "range",
                            "low": "0",
                            "high": endpoint(
                                max(Decimal(0), Decimal(100) - Decimal(str(percent)))
                            ),
                            "reason": "pre_reset_upper_bound",
                        }
                    )
                elif poll:
                    value.reason = "stale_or_incompatible_quota_observation"
                result.append(
                    {
                        "account_id": row.account_id,
                        "window_id": row.window_id,
                        "resets_at": row.resets_at,
                        "observed_at": row.observed_at,
                        "evidence": payload,
                        "remaining": value.model_dump(),
                    }
                )
            return result
