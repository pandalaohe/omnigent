"""Shared label names and codec for durable child check-in follow-ups."""

import json
from dataclasses import dataclass

from omnigent.db.db_models import LABEL_VALUE_MAX_LEN
from omnigent.db.utils import strip_nul_bytes

CHECKIN_LABEL_PREFIX = "omnigent.checkin."
FOLLOWUP_PREFIX = f"{CHECKIN_LABEL_PREFIX}fu."
FOLLOWUP_NEXT_KEY = f"{CHECKIN_LABEL_PREFIX}fu_next"
INSTRUCTIONS_PREFIX = f"{CHECKIN_LABEL_PREFIX}instr."
POLICY_KEY = f"{CHECKIN_LABEL_PREFIX}policy"
LAST_KEY = f"{CHECKIN_LABEL_PREFIX}last"


@dataclass(frozen=True)
class FollowUp:
    """A durable re-check; uncertain deliveries stay visible without resending."""

    id: str
    due: int
    text: str
    uncertain: bool = False


def encode_followup(item: FollowUp) -> str:
    """Validate and encode a follow-up without truncating its stored text."""
    if type(item.due) is not int or item.due < 0:
        raise ValueError("follow-up due time must be a nonnegative integer")
    if type(item.uncertain) is not bool:
        raise ValueError("follow-up uncertainty must be a boolean")
    text = strip_nul_bytes(item.text)
    if not 1 <= len(text) <= 200 or not text.strip():
        raise ValueError("follow-up text must be 1 to 200 characters and must not be blank")
    raw = json.dumps(
        {"d": item.due, "t": text, "u": int(item.uncertain)},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    if len(raw) > LABEL_VALUE_MAX_LEN:
        raise ValueError("encoded follow-up exceeds its storage limit")
    return raw


def decode_followup(raw: str) -> FollowUp | None:
    """Decode valid stored fields; the label key supplies the follow-up id."""
    if len(raw) > LABEL_VALUE_MAX_LEN:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if (
        not isinstance(value, dict)
        or type(value.get("d")) is not int
        or value["d"] < 0
        or type(value.get("u")) is not int
        or value["u"] not in (0, 1)
        or not isinstance(value.get("t"), str)
    ):
        return None
    text = strip_nul_bytes(value["t"])
    if not 1 <= len(text) <= 200 or not text.strip():
        return None
    return FollowUp(id="", due=value["d"], text=text, uncertain=bool(value["u"]))


def next_followup_due(labels: dict[str, str]) -> int | None:
    """Find the earliest valid follow-up still eligible for automatic delivery."""
    return min(
        (
            item.due
            for key, raw in labels.items()
            if key.startswith(FOLLOWUP_PREFIX)
            and (item := decode_followup(raw)) is not None
            and not item.uncertain
        ),
        default=None,
    )
