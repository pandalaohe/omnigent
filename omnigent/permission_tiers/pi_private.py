"""Keep Pi transport authority out of public results and diagnostic sinks."""

from __future__ import annotations

import re
from collections.abc import Mapping

_TOKEN = re.compile(r"\bpi_(?:scope|receipt|attestor)_[A-Za-z0-9_-]{32,256}")
_FIELD = re.compile(
    r"(?P<label>\b(?:pi_origin_receipt|scope_credential|attestor_credential|process_capability)"
    r"[\"']?\s*[:=]\s*[\"']?)[A-Za-z0-9_-]+"
)
_PRIVATE_KEYS = frozenset(
    (
        "pi_origin_receipt",
        "permission_tier_binding",
        "scope_credential",
        "attestor_credential",
        "process_capability",
    )
)


def redact_pi_authority(text: str) -> str:
    """Recognize tagged opaque capabilities across process/log boundaries."""
    return _FIELD.sub(
        r"\g<label>[private Pi authority]", _TOKEN.sub("[private Pi authority]", text)
    )


def public_pi_value(value: object) -> object:
    """Project public data before serialization, never serialize authority envelopes."""
    if isinstance(value, str):
        return redact_pi_authority(value)
    if isinstance(value, Mapping):
        return {
            key: public_pi_value(item) for key, item in value.items() if key not in _PRIVATE_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [public_pi_value(item) for item in value]
    return value
