"""Stable identities for archived source report changes."""

from datetime import UTC, datetime
from hashlib import sha256
from time import time_ns
from typing import Any
from uuid import uuid4

from omnigent.usage.evidence import SourceEvent


def change_event_id() -> str:
    # The archive stores seconds; preserve writer order for changes in the same second.
    return f"{time_ns():020d}{uuid4().hex[:12]}"


def report_log_event(account: str, entry: dict[str, Any]) -> SourceEvent | None:
    kind, at = entry["kind"], entry["at"]
    # An envelope receipt is not a source state change.
    if kind == "cloudevent":
        return None
    severity = {
        "read_failed": "error",
        "writer_conflict": "error",
        "rejected": "error",
        "import_rejected": "error",
        "correction": "info",
    }.get(kind, "warn")
    return SourceEvent.model_validate(
        {
            "specversion": "1.0",
            "id": sha256(f"{account}|{kind}|{at}".encode()).hexdigest()[:32],
            "source": f"report:{account}",
            "type": kind,
            "subject": account,
            "time": datetime.fromtimestamp(at, UTC).isoformat(),
            "severity": severity,
            "data": entry,
        }
    )
