"""Agent splits are subordinate to the unchanged hourly accounting key."""

from dataclasses import replace
from decimal import Decimal
from typing import Any

from sqlalchemy import func
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from omnigent.db.db_models import SqlUsageHourAgent

KEY = ("workspace_id", "owner_id", "account_id", "hour_utc", "model", "host_id", "basis", "priced")
MEASURES = {
    "input_tokens": "uncached_input_tokens",
    "cached_tokens": "cache_read_input_tokens",
    "cache_write_tokens": "cache_creation_input_tokens",
    "output_tokens": "output_tokens",
    "total_tokens": "tokens",
    "reasoning_tokens": "reasoning_tokens",
    "requests": "requests",
    "failed": "failed",
    "cost_usd": "cost_usd",
}


def part_values(part):
    return {
        name: part.total() if name == "total_tokens" else getattr(part, field)
        for name, field in MEASURES.items()
    }


def money_close(total, subtotal):
    total = Decimal(str(total))
    return abs(total - subtotal) <= max(abs(total) * Decimal("0.0001"), Decimal("0.000001"))


def valid_parts(main, parts) -> bool:
    for name in MEASURES:
        if name == "cost_usd":
            continue
        values = [part[name] for part in parts if part[name] is not None]
        if values:
            subtotal = sum((Decimal(str(value)) for value in values), Decimal(0))
            if main[name] is None:
                return False
            if subtotal > Decimal(str(main[name])):
                return False
    return True


def increment_part(session, values) -> None:
    table = SqlUsageHourAgent
    dialect = session.get_bind().dialect.name
    insert = (
        mysql_insert if dialect == "mysql" else sqlite_insert if dialect == "sqlite" else pg_insert
    )
    statement: Any = insert(table).values(**values)
    proposed = statement.inserted if dialect == "mysql" else statement.excluded
    updates = {
        name: func.coalesce(getattr(table, name), 0.0 if name == "cost_usd" else 0)
        + getattr(proposed, name)
        for name in MEASURES
        if values.get(name) is not None
    }
    updates["updated_at"] = values["updated_at"]
    if dialect == "mysql":
        statement = statement.on_duplicate_key_update(**updates)
    else:
        statement = statement.on_conflict_do_update(index_elements=[*KEY, "agent"], set_=updates)
    session.execute(statement)


def split_row(row, parts):
    """Validate integers exactly, preserving them when only money is inconsistent."""
    if row.account_id.startswith("native:"):
        return [replace(row, agent=row.account_id.removeprefix("native:"))]
    main = {name: getattr(row, name) for name in MEASURES}
    values = [{name: part[name] for name in MEASURES} for part in parts]
    if not values or not valid_parts(main, values):
        return [row]
    subtotal = sum((Decimal(str(part["cost_usd"] or 0)) for part in values), Decimal(0))
    total = Decimal(str(main["cost_usd"] or 0))
    close = main["cost_usd"] is not None and money_close(total, subtotal)
    money_unknown = subtotal > total and not close
    if money_unknown:
        for part in values:
            part["cost_usd"] = None
    elif close and subtotal:
        known = [part for part in values if part["cost_usd"] is not None]
        for part in known[:-1]:
            part["cost_usd"] = Decimal(str(part["cost_usd"])) * total / subtotal
        known[-1]["cost_usd"] = total - sum((part["cost_usd"] for part in known[:-1]), Decimal(0))
    result = [
        replace(
            row,
            agent=part["agent"],
            agent_money_unknown=money_unknown,
            reasons=None,
            **{name: values_part[name] if main[name] is not None else None for name in MEASURES},
        )
        for part, values_part in zip(parts, values, strict=True)
    ]
    residual = {
        name: None
        if value is None or name == "cost_usd"
        else value - sum(part[name] or 0 for part in values)
        for name, value in main.items()
    }
    if main["cost_usd"] is not None:
        residual["cost_usd"] = (
            None
            if money_unknown
            else 0.0
            if close and subtotal
            else max(
                0.0,
                float(
                    Decimal(str(main["cost_usd"]))
                    - sum((Decimal(str(part["cost_usd"] or 0)) for part in values), Decimal(0))
                ),
            )
        )
    if any(value for value in residual.values() if value is not None):
        result.append(replace(row, agent="", agent_money_unknown=money_unknown, **residual))
    return result
