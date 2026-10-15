"""SDK delivery attempts retain identity without overloading envelope text."""

import pytest

from omnigent.sdk_peer_delivery import SDKPeerInput, sdk_attempt, sdk_attempt_reason


@pytest.mark.parametrize("phase", ["wait", "accepted", "refused", "unknown"])
@pytest.mark.parametrize("released", [False, True])
def test_attempt_reason_round_trip(phase, released):
    reason = sdk_attempt_reason("a" * 32, phase, released=released)
    assert len(reason) <= 64
    assert sdk_attempt(reason) == ("a" * 32, phase)


@pytest.mark.parametrize("reason", [None, "sdk_wait:short", "sdk_unknown:" + "x" * 32])
def test_unrelated_reason_is_not_an_attempt(reason):
    assert sdk_attempt(reason) is None


def test_control_metadata_does_not_include_envelope():
    delivery = SDKPeerInput("a" * 32, "b" * 32, "peer envelope")
    assert delivery.metadata() == {"peer_id": "a" * 32, "attempt_id": "b" * 32}
    assert SDKPeerInput.from_metadata(delivery.metadata(), delivery.text) == delivery
    with pytest.raises(ValueError):
        SDKPeerInput.from_metadata({**delivery.metadata(), "outcome": "accepted"}, "text")
