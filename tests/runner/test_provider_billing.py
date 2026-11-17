"""Billing refusal semantics precede generic provider auth statuses."""

import pytest

from omnigent.harnesses.codex_native.forwarder import _classify_codex_error, _error_payload_message
from omnigent.runner.launch_failure import classify_native_turn_error


@pytest.mark.parametrize(
    "message,kind",
    [
        ("HTTP 401 Unauthorized", "auth"),
        ("HTTP 403 forbidden", "auth"),
        ("HTTP 403 blocked by Databricks IP ACL", "generic"),
    ],
)
def test_auth_and_ip_refusals_remain_distinct(message, kind):
    assert _classify_codex_error({}, message) == kind


def test_structured_provider_code_precedes_generic_auth_variant():
    assert (
        _classify_codex_error(
            {"codexErrorInfo": "unauthorized", "error": {"code": "billing_disabled"}},
            "HTTP 403 Forbidden",
        )
        == "generic"
    )


def test_structured_billing_meaning_survives_forwarding_to_the_server():
    message = _error_payload_message(
        {
            "message": "HTTP 403 Forbidden",
            "error": {"code": "billing_disabled"},
        }
    )
    assert classify_native_turn_error("codex_reauth_required", message) == "budget_exhausted"
    assert _classify_codex_error({}, message) == "generic"


@pytest.mark.parametrize(
    "message",
    [
        'unexpected status 403: {"error":{"code":"billing_disabled",'
        '"message":"balance billing is disabled for this key"}}',
        'HTTP 403 {"error":{"code":"plan_exhausted","message":"Plan exhausted"}}',
    ],
)
def test_billing_refusal_is_not_reauth(message):
    assert classify_native_turn_error("codex_reauth_required", message) == "budget_exhausted"
    assert _classify_codex_error({"codexErrorInfo": {"httpStatusCode": 403}}, message) == "generic"
