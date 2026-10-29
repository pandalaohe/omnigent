"""Persisted retry identifiers remain stable across releases and namespaces."""

import re

import pytest
from pydantic import ValidationError

from omnigent.entities.subagent_retry import InboxOwnerFence, RetryControl, retry_identity


def test_identity_encoding_is_unambiguous_and_namespaced():
    chain = retry_identity("subagent-chain", "child", "turn")
    assert chain == "7b4e80c3e9c2eb0c750bee56455fae32"
    assert re.fullmatch("[a-f0-9]{32}", chain)
    assert chain != retry_identity("subagent-final", "child", "turn")
    assert retry_identity("input", "ab", "c") != retry_identity("input", "a", "bc")
    assert retry_identity("input", "a|b", "c") != retry_identity("input", "a", "b|c")


@pytest.mark.parametrize("generation", [True, "1", -1])
def test_generation_and_owner_epoch_do_not_coerce_untrusted_values(generation):
    with pytest.raises(ValidationError):
        RetryControl(child_id="child", generation=generation, turn_id=None, input_id=None)
    with pytest.raises(ValidationError):
        InboxOwnerFence(parent_id="parent", runner_id="runner", epoch=generation)
