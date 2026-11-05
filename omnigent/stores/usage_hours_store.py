"""Hourly usage totals: the ``usage_hours`` table and its write path.

One row per (workspace, owner, account, UTC hour, model, host, basis, priced).
Rows are accumulated at the two points where Omnigent already records a turn's
spend, and replaced wholesale by a pushed report's ``activity``. Every amount,
error mark and per-model figure on the usage board reads this table; the
per-user daily rollup (``user_daily_cost``) stays the policy figure and is never
rewritten by a later price.

The module also owns the seam-side recorder. ``create_app`` configures one
:class:`UsageHoursRecorder`; the two usage paths call
:func:`record_turn_usage`, which is a no-op when nothing is configured (tests,
CLI, a deployment with the module off) and never raises into the turn.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session
from sqlalchemy.sql.dml import Insert

from omnigent.db.account_authority import require_active_account
from omnigent.db.db_models import SqlUsageHour, current_workspace_id
from omnigent.db.utils import (
    NamedManagedSessionMaker,
    get_or_create_engine,
    is_postgresql_family,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.usage.contract import ActivityHour

_logger = logging.getLogger(__name__)

#: A usage account Omnigent records itself is namespaced so a pushed account
#: with the same id can never overwrite native increments.
NATIVE_ACCOUNT_PREFIX = "native:"

#: Bases that may be counted in a money total. ``subscription`` is a list-price
#: figure rather than a bill, and ``unknown`` is not a figure at all.
COUNTED_BASES = frozenset({"metered"})

#: Bound for a pushed ``activity`` hour: a source clock can be wrong, and a
#: future row would land in every "now" range.
_ACTIVITY_MAX_AGE_S = 400 * 86_400


@dataclass(frozen=True)
class HourRow:
    """One ``usage_hours`` row, as read back."""

    workspace_id: int
    owner_id: str
    account_id: str
    hour_utc: int
    model: str
    host_id: str
    basis: str
    priced: bool
    input_tokens: int
    cached_tokens: int
    cache_write_tokens: int
    output_tokens: int
    total_tokens: int
    requests: int | None
    failed: int | None
    reasons: dict[str, int] | None
    cost_usd: float
    updated_at: int


@dataclass(frozen=True)
class TokenGrowth:
    """One turn's token counts, in the buckets a price is computed from."""

    input_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0

    def any(self) -> bool:
        """Whether any non-cached bucket carries a count."""
        return bool(
            self.input_tokens
            or self.cached_tokens
            or self.cache_write_tokens
            or self.output_tokens
        )

    def cost_dict(self) -> dict[str, int]:
        """The form :func:`compute_llm_cost` reads."""
        return {
            "input_tokens": self.input_tokens,
            "cache_read_input_tokens": self.cached_tokens,
            "cache_creation_input_tokens": self.cache_write_tokens,
            "output_tokens": self.output_tokens,
        }


def _row_from_orm(row: SqlUsageHour) -> HourRow:
    reasons: dict[str, int] | None = None
    if row.reasons:
        try:
            decoded = json.loads(row.reasons)
        except ValueError:
            decoded = None
        if isinstance(decoded, dict):
            reasons = {str(name): int(count) for name, count in decoded.items()}
    return HourRow(
        workspace_id=row.workspace_id,
        owner_id=row.owner_id,
        account_id=row.account_id,
        hour_utc=row.hour_utc,
        model=row.model,
        host_id=row.host_id,
        basis=row.basis,
        priced=bool(row.priced),
        input_tokens=row.input_tokens,
        cached_tokens=row.cached_tokens,
        cache_write_tokens=row.cache_write_tokens,
        output_tokens=row.output_tokens,
        total_tokens=row.total_tokens,
        requests=row.requests,
        failed=row.failed,
        reasons=reasons,
        cost_usd=row.cost_usd,
        updated_at=row.updated_at,
    )


class UsageHoursStore:
    """Lazy synchronous access to the ``usage_hours`` table."""

    def __init__(self, storage_location: str) -> None:
        self._storage_location = storage_location
        self._read_sessions: NamedManagedSessionMaker | None = None
        self._write_sessions: NamedManagedSessionMaker | None = None

    def _read_session(self) -> NamedManagedSessionMaker:
        if self._read_sessions is None:
            self._read_sessions = make_named_managed_session_maker(
                get_or_create_engine(self._storage_location),
                query_name_prefix="omnigent.usage_hours_store",
            )
        return self._read_sessions

    def _write_session(self) -> NamedManagedSessionMaker:
        if self._write_sessions is None:
            self._write_sessions = make_named_managed_session_maker(
                get_or_create_engine(self._storage_location),
                query_name_prefix="omnigent.usage_hours_store",
                immediate=True,
            )
        return self._write_sessions

    # -- writes ---------------------------------------------------------

    def add_turn(
        self,
        owner: str,
        account_id: str,
        hour: int,
        model: str,
        host_id: str,
        basis: str,
        priced: bool,
        input_tokens: int,
        cached_tokens: int,
        cache_write_tokens: int,
        output_tokens: int,
        total_tokens: int,
        cost_usd: float,
    ) -> None:
        """Add one turn's usage to its hour row, creating the row if needed.

        A single dialect upsert increments every counter, so concurrent turns
        in the same hour never lose each other's deltas.

        :param owner: The account the usage belongs to.
        :param account_id: ``native:<agent key>`` or a pushed account id.
        :param hour: Epoch seconds of the UTC hour start.
        :param model: Model id, or ``""`` when the path knows none.
        :param host_id: Host that ran the session, or ``""``.
        :param basis: ``metered``, ``subscription``, ``estimate`` or
            ``unknown``; frozen on the row.
        :param priced: Whether ``cost_usd`` covers this row's tokens.
        :param input_tokens: Non-cached input tokens.
        :param cached_tokens: Cache-read input tokens.
        :param cache_write_tokens: Cache-creation input tokens.
        :param output_tokens: Output tokens.
        :param total_tokens: Full count for display.
        :param cost_usd: USD to add; a non-positive value adds nothing.
        """
        tokens = TokenGrowth(
            input_tokens=max(0, input_tokens),
            cached_tokens=max(0, cached_tokens),
            cache_write_tokens=max(0, cache_write_tokens),
            output_tokens=max(0, output_tokens),
            total_tokens=max(0, total_tokens),
        )
        cost = max(0.0, float(cost_usd))
        if not tokens.any() and cost <= 0:
            # Nothing to record: an empty turn must not create a zero row that
            # later reads as a real (empty) hour.
            return
        self._increment(
            owner,
            account_id,
            hour,
            model,
            host_id,
            basis,
            priced,
            tokens,
            None,
            None,
            None,
            cost,
        )

    def replace_activity(self, owner: str, account_id: str, rows: Sequence[ActivityHour]) -> None:
        """Replace the rows one reported ``activity`` covers.

        Each entry replaces the ``(account, hour, model)`` rows it names, so a
        retry or a source's own correction is safe. An hour in the future or
        older than the report bound is dropped rather than written.

        :param owner: The account owner.
        :param account_id: The pushed account the activity belongs to.
        :param rows: The validated activity entries, at most 500 per report.
        """
        now = now_epoch()
        bounded = [
            row for row in rows if row.hour <= now and row.hour >= now - _ACTIVITY_MAX_AGE_S
        ]
        if not bounded:
            return

        def write(session: Session) -> None:
            require_active_account(session, owner)
            for row in bounded:
                model = row.model or ""
                session.execute(
                    delete(SqlUsageHour).where(
                        SqlUsageHour.workspace_id == current_workspace_id(),
                        SqlUsageHour.owner_id == owner,
                        SqlUsageHour.account_id == account_id,
                        SqlUsageHour.hour_utc == hours_of(row.hour),
                        SqlUsageHour.model == model,
                    )
                )
                tokens = max(0, row.tokens or 0)
                cost = 0.0 if row.cost_usd is None else max(0.0, float(row.cost_usd))
                self._upsert(
                    session,
                    owner,
                    account_id,
                    hours_of(row.hour),
                    model,
                    "",
                    row.cost_basis,
                    row.cost_usd is not None,
                    TokenGrowth(total_tokens=tokens),
                    requests=row.requests,
                    failed=row.failed,
                    reasons=row.reasons,
                    cost_usd=cost,
                    increment=False,
                )

        run_write_transaction(self._write_session(), "replace_usage_hours", write)

    def delete_account(self, owner: str, account_id: str) -> None:
        """Remove one account's hours."""

        def write(session: Session) -> None:
            session.execute(
                delete(SqlUsageHour).where(
                    SqlUsageHour.workspace_id == current_workspace_id(),
                    SqlUsageHour.owner_id == owner,
                    SqlUsageHour.account_id == account_id,
                )
            )

        run_write_transaction(self._write_session(), "delete_usage_account_hours", write)

    def fill_unpriced(
        self,
        price_for: Callable[[str], Any],
        closed_before: int,
    ) -> int:
        """Price unpriced rows of closed hours whose model now has a price.

        One transaction per row: the cost (and the token split) moves onto the
        priced row and the unpriced row goes, so a retry finds nothing left.
        The fill touches ``usage_hours`` only — ``user_daily_cost``,
        ``session_usage`` and budgets never change retroactively, so a fill
        cannot trip a budget for spend nobody could have seen.

        A row without a token split (a pushed ``activity`` row carries one
        number, not a split) is skipped: there is nothing to price it from.

        :param price_for: Model id to pricing (``None`` when unpriced). The
            same lookup a turn uses.
        :param closed_before: Epoch seconds; only rows whose hour start is
            strictly earlier are filled, so a live hour's concurrent writer
            is never disturbed.
        :returns: How many rows were filled.
        """
        from omnigent.llms.context_window import compute_llm_cost

        candidates = self._read_unpriced(closed_before)
        filled = 0
        for row in candidates:
            growth = TokenGrowth(
                input_tokens=row.input_tokens,
                cached_tokens=row.cached_tokens,
                cache_write_tokens=row.cache_write_tokens,
                output_tokens=row.output_tokens,
                total_tokens=row.total_tokens,
            )
            if not growth.any() or not row.model:
                continue
            pricing = price_for(row.model)
            if pricing is None:
                continue
            cost = compute_llm_cost(growth.cost_dict(), pricing)
            if self._move_to_priced(row, cost):
                filled += 1
        return filled

    # -- reads ----------------------------------------------------------

    def read(
        self,
        owner: str,
        since_hour: int,
        until_hour: int,
        account_id: str | None = None,
    ) -> list[HourRow]:
        """Read one owner's rows in ``[since_hour, until_hour)``.

        :param owner: The account owner.
        :param since_hour: Epoch seconds of the first UTC hour to include.
        :param until_hour: Epoch seconds bound; rows at or after it are out.
        :param account_id: Restrict to one account when given.
        :returns: Rows ordered by hour, then account and model.
        """
        with self._read_session()("read_usage_hours") as session:
            query = select(SqlUsageHour).where(
                SqlUsageHour.workspace_id == current_workspace_id(),
                SqlUsageHour.owner_id == owner,
                SqlUsageHour.hour_utc >= since_hour,
                SqlUsageHour.hour_utc < until_hour,
            )
            if account_id is not None:
                query = query.where(SqlUsageHour.account_id == account_id)
            query = query.order_by(
                SqlUsageHour.hour_utc,
                SqlUsageHour.account_id,
                SqlUsageHour.model,
            )
            return [_row_from_orm(row) for row in session.execute(query).scalars()]

    def _read_unpriced(self, closed_before: int) -> list[HourRow]:
        with self._read_session()("read_unpriced_usage_hours") as session:
            query = (
                select(SqlUsageHour)
                .where(
                    SqlUsageHour.workspace_id == current_workspace_id(),
                    SqlUsageHour.priced == 0,
                    SqlUsageHour.hour_utc < closed_before,
                )
                .order_by(SqlUsageHour.hour_utc)
            )
            return [_row_from_orm(row) for row in session.execute(query).scalars()]

    # -- internals ------------------------------------------------------

    def _move_to_priced(self, row: HourRow, cost: float) -> bool:
        def write(session: Session) -> bool:
            if row.owner_id:
                require_active_account(session, row.owner_id)
            # Read the unpriced row again inside the transaction: a concurrent
            # fill of the same row (two server replicas) must not double-add.
            present = session.get(
                SqlUsageHour,
                (
                    current_workspace_id(),
                    row.owner_id,
                    row.account_id,
                    row.hour_utc,
                    row.model,
                    row.host_id,
                    row.basis,
                    0,
                ),
            )
            if present is None:
                return False
            self._upsert(
                session,
                row.owner_id,
                row.account_id,
                row.hour_utc,
                row.model,
                row.host_id,
                row.basis,
                True,
                TokenGrowth(
                    input_tokens=present.input_tokens,
                    cached_tokens=present.cached_tokens,
                    cache_write_tokens=present.cache_write_tokens,
                    output_tokens=present.output_tokens,
                    total_tokens=present.total_tokens,
                ),
                requests=present.requests,
                failed=present.failed,
                reasons=None,
                cost_usd=cost,
                increment=True,
            )
            session.delete(present)
            return True

        return run_write_transaction(self._write_session(), "fill_usage_hours", write)

    def _increment(
        self,
        owner: str,
        account_id: str,
        hour: int,
        model: str,
        host_id: str,
        basis: str,
        priced: bool,
        growth: TokenGrowth,
        requests: int | None,
        failed: int | None,
        reasons: Mapping[str, int] | None,
        cost_usd: float,
    ) -> None:
        def write(session: Session) -> None:
            require_active_account(session, owner)
            self._upsert(
                session,
                owner,
                account_id,
                hour,
                model,
                host_id,
                basis,
                priced,
                growth,
                requests=requests,
                failed=failed,
                reasons=reasons,
                cost_usd=cost_usd,
                increment=True,
            )

        run_write_transaction(self._write_session(), "add_usage_hour", write)

    def _upsert(
        self,
        session: Session,
        owner: str,
        account_id: str,
        hour: int,
        model: str,
        host_id: str,
        basis: str,
        priced: bool,
        growth: TokenGrowth,
        *,
        requests: int | None,
        failed: int | None,
        reasons: Mapping[str, int] | None,
        cost_usd: float,
        increment: bool,
    ) -> None:
        """Insert or merge one row, adding the counters when *increment*."""
        now = now_epoch()
        values: dict[str, Any] = {
            "workspace_id": current_workspace_id(),
            "owner_id": owner,
            "account_id": account_id,
            "hour_utc": hour,
            "model": model,
            "host_id": host_id,
            "basis": basis,
            "priced": 1 if priced else 0,
            "input_tokens": growth.input_tokens,
            "cached_tokens": growth.cached_tokens,
            "cache_write_tokens": growth.cache_write_tokens,
            "output_tokens": growth.output_tokens,
            "total_tokens": growth.total_tokens,
            "requests": requests,
            "failed": failed,
            "reasons": json.dumps(reasons, sort_keys=True) if reasons else None,
            "cost_usd": cost_usd,
            "updated_at": now,
        }
        key = [
            "workspace_id",
            "owner_id",
            "account_id",
            "hour_utc",
            "model",
            "host_id",
            "basis",
            "priced",
        ]
        dialect = session.get_bind().dialect.name
        if dialect == "mysql":
            # MySQL names the proposed row ``inserted``; the others ``excluded``.
            insert_stmt = mysql_insert(SqlUsageHour).values(**values)
            stmt: Insert = insert_stmt.on_duplicate_key_update(
                **self._merge_values(values, insert_stmt.inserted, key, increment)
            )
        elif dialect == "sqlite":
            sqlite_stmt = sqlite_insert(SqlUsageHour).values(**values)
            stmt = sqlite_stmt.on_conflict_do_update(
                index_elements=key,
                set_=self._merge_values(values, sqlite_stmt.excluded, key, increment),
            )
        elif is_postgresql_family(dialect):
            pg_stmt = pg_insert(SqlUsageHour).values(**values)
            stmt = pg_stmt.on_conflict_do_update(
                index_elements=key,
                set_=self._merge_values(values, pg_stmt.excluded, key, increment),
            )
        else:
            raise RuntimeError(f"usage_hours has no upsert for dialect {dialect!r}")
        session.execute(stmt)

    @staticmethod
    def _merge_values(
        values: Mapping[str, Any],
        proposed: Any,
        key: Sequence[str],
        increment: bool,
    ) -> dict[str, Any]:
        """Columns to set when the row already exists.

        Counters add when *increment* (a turn's delta) and replace otherwise
        (an activity entry, which is already a whole-hour figure).

        :param values: The row's proposed values.
        :param proposed: The statement's proposed-row alias (``excluded``, or
            ``inserted`` on MySQL), used for the additive counters.
        :param key: Primary-key columns, never updated.
        :param increment: Add the counters instead of replacing them.
        """
        counters = (
            "input_tokens",
            "cached_tokens",
            "cache_write_tokens",
            "output_tokens",
            "total_tokens",
            "cost_usd",
        )
        merged: dict[str, Any] = {}
        for column, value in values.items():
            if column in key:
                continue
            if increment and column in counters:
                merged[column] = getattr(SqlUsageHour, column) + getattr(proposed, column)
            elif column in {"requests", "failed", "reasons"} and value is None:
                # A turn carries no request counts: keep whatever a source
                # already reported for the hour instead of erasing it.
                continue
            else:
                merged[column] = value
        merged["updated_at"] = values["updated_at"]
        return merged


class UsageHoursRecorder:
    """The seam-side writer: store, deployment switch and basis resolution."""

    def __init__(
        self,
        store: UsageHoursStore,
        *,
        enabled: bool = True,
        login_kind: Callable[[str, str | None], str | None] | None = None,
    ) -> None:
        self._store = store
        self._enabled = enabled
        self._login_kind = login_kind

    @property
    def store(self) -> UsageHoursStore:
        """The underlying store, for readers."""
        return self._store

    @property
    def enabled(self) -> bool:
        """Whether the deployment records usage."""
        return self._enabled

    def basis_for(self, host_id: str, harness: str | None) -> str:
        """The accounting basis of a session's turns, from its host login.

        Frozen per turn so history never changes with a later reading:
        a key or provider-managed host is ``metered``, a subscription or
        ChatGPT login is ``subscription`` (a list-price figure, not a bill),
        and an unknown login or no live host is ``unknown`` — left out of the
        money total rather than inflating it.

        :param host_id: The host that ran the session, or ``""``.
        :param harness: The session's harness; its agent key names the entry
            in the host's reported login kinds.
        :returns: ``metered``, ``subscription`` or ``unknown``.
        """
        if not host_id or self._login_kind is None:
            return "unknown"
        from omnigent.runtime.instruction_layers import agent_key_for_harness

        try:
            kind = self._login_kind(host_id, agent_key_for_harness(harness))
        except Exception:
            _logger.debug("usage basis lookup failed for host %s", host_id, exc_info=True)
            return "unknown"
        if kind in {"api_key", "providers"}:
            return "metered"
        if kind in {"subscription", "chatgpt"}:
            return "subscription"
        return "unknown"

    def add_growth(
        self,
        *,
        owner: str,
        account_id: str,
        model: str,
        host_id: str,
        harness: str | None,
        growth: TokenGrowth,
        cost_usd: float,
        priced: bool,
        hour: int | None = None,
    ) -> None:
        """Record one turn's growth at the current UTC hour.

        A store failure is logged and swallowed: recording usage never fails
        the turn that produced it.
        """
        if not self._enabled:
            return
        basis = self.basis_for(host_id, harness)
        try:
            self._store.add_turn(
                owner,
                account_id,
                hour if hour is not None else now_epoch() // 3600 * 3600,
                model or "",
                host_id,
                basis,
                priced,
                growth.input_tokens,
                growth.cached_tokens,
                growth.cache_write_tokens,
                growth.output_tokens,
                growth.total_tokens,
                cost_usd if priced else 0.0,
            )
        except Exception:
            _logger.warning("usage_hours write failed; continuing the turn", exc_info=True)


_recorder: UsageHoursRecorder | None = None


def configure_usage_hours(recorder: UsageHoursRecorder | None) -> None:
    """Install the process-wide recorder (called once from ``create_app``)."""
    global _recorder
    _recorder = recorder


def usage_hours_recorder() -> UsageHoursRecorder | None:
    """The configured recorder, or ``None`` when the module is not wired."""
    return _recorder


def record_turn_usage(
    *,
    owner: str | None,
    account_id: str,
    model: str,
    host_id: str,
    harness: str | None,
    growth: TokenGrowth,
    cost_usd: float,
    priced: bool,
) -> None:
    """Record a turn's usage through the configured recorder, if any."""
    recorder = _recorder
    if recorder is None or not owner:
        return
    recorder.add_growth(
        owner=owner,
        account_id=account_id,
        model=model,
        host_id=host_id,
        harness=harness,
        growth=growth,
        cost_usd=cost_usd,
        priced=priced,
    )


def hours_of(epoch_seconds: int) -> int:
    """Floor an epoch second to its UTC hour start."""
    return epoch_seconds // 3600 * 3600


__all__ = [
    "COUNTED_BASES",
    "NATIVE_ACCOUNT_PREFIX",
    "HourRow",
    "TokenGrowth",
    "UsageHoursRecorder",
    "UsageHoursStore",
    "configure_usage_hours",
    "hours_of",
    "record_turn_usage",
    "usage_hours_recorder",
]
