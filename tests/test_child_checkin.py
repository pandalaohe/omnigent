"""Shared child check-in label codec and discovery index tests."""

import json

import pytest

from omnigent.child_checkin import (
    FOLLOWUP_PREFIX,
    FollowUp,
    decode_followup,
    encode_followup,
    next_followup_due,
)


@pytest.mark.parametrize("text", ["中文" * 100, 'quoted "text"', "a\x00b"])
def test_followup_round_trip(text: str) -> None:
    raw = encode_followup(FollowUp("abcd1234", 123, text, uncertain=True))
    assert len(raw) <= 256
    assert decode_followup(raw) == FollowUp("", 123, text.replace("\x00", ""), True)
    assert json.loads(raw)["u"] == 1


@pytest.mark.parametrize("text", ['"' * 200, "a" * 201, "", " \t ", "\x00"])
def test_followup_rejects_invalid_or_overlong_encoded_text(text: str) -> None:
    with pytest.raises(ValueError):
        encode_followup(FollowUp("abcd1234", 123, text))


@pytest.mark.parametrize(
    "raw",
    [
        "broken",
        "[]",
        '{"d":true,"t":"work","u":0}',
        '{"d":-1,"t":"work","u":0}',
        '{"d":1,"t":"work","u":false}',
        '{"d":1,"t":"work","u":2}',
        '{"d":1,"t":"work"}',
        '{"d":1,"t":" ","u":0}',
        '{"d":1,"t":42,"u":0}',
        json.dumps({"d": 1, "t": "x" * 201, "u": 0}),
    ],
)
def test_followup_skips_malformed_records(raw: str) -> None:
    assert decode_followup(raw) is None


@pytest.mark.parametrize("due", [-1, True, 1.5])
def test_followup_rejects_invalid_due(due: int) -> None:
    with pytest.raises(ValueError, match="nonnegative integer"):
        encode_followup(FollowUp("abcd1234", due, "work"))


def test_followup_next_ignores_uncertain_and_malformed_items() -> None:
    labels = {
        f"{FOLLOWUP_PREFIX}a": encode_followup(FollowUp("a", 20, "later")),
        f"{FOLLOWUP_PREFIX}b": encode_followup(FollowUp("b", 10, "earlier")),
        f"{FOLLOWUP_PREFIX}c": encode_followup(FollowUp("c", 1, "uncertain", True)),
        f"{FOLLOWUP_PREFIX}d": "invalid",
        "other": encode_followup(FollowUp("e", 0, "unrelated")),
    }
    assert next_followup_due(labels) == 10
    del labels[f"{FOLLOWUP_PREFIX}a"], labels[f"{FOLLOWUP_PREFIX}b"]
    assert next_followup_due(labels) is None
