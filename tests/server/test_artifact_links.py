"""Unit tests for the artifact-link token and per-session key primitives."""

from __future__ import annotations

import base64
import json

import pytest

from omnigent.server.artifact_links import (
    ArtifactTokenClaims,
    bridge_nonce,
    decode_artifact_token,
    encode_artifact_token,
    generate_session_key,
    key_id,
    session_key_bytes,
    verify_artifact_token,
)

_KEY = b"\x11" * 32
_ROTATED_KEY = b"\x22" * 32
_EXPIRY = 1_800_000_000


def _token(key: bytes = _KEY, **overrides: object) -> str:
    fields: dict[str, object] = {
        "session_id": "conv_abc123",
        "workspace_id": 0,
        "root": "reports",
        "absolute": False,
        "entry": "index.html",
        "kind": "b",
        "view": "p",
    }
    fields.update(overrides)
    if fields["view"] == "p":
        fields.setdefault("expires_at", _EXPIRY)
    return encode_artifact_token(key, **fields)  # type: ignore[arg-type]


def _payload_segment(claims: ArtifactTokenClaims, **overrides: object) -> str:
    """Re-encode a payload from claims, allowing field tweaks for tamper tests."""
    fields: dict[str, object] = {
        "s": claims.session_id,
        "r": claims.root,
        "b": 1 if claims.absolute else 0,
        "e": claims.entry,
        "k": claims.kind,
        "v": claims.view,
        "i": claims.key_id,
        "w": claims.workspace_id,
    }
    if claims.expires_at is not None:
        fields["x"] = claims.expires_at
    fields.update(overrides)
    raw = json.dumps(fields, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def test_round_trip_preserves_every_claim() -> None:
    token = _token(workspace_id=7, absolute=True, entry="deep/page.html", kind="f", view="r")
    claims = decode_artifact_token(token)

    assert claims is not None
    assert claims == ArtifactTokenClaims(
        session_id="conv_abc123",
        workspace_id=7,
        root="reports",
        absolute=True,
        entry="deep/page.html",
        kind="f",
        view="r",
        key_id=key_id(_KEY),
    )
    assert verify_artifact_token(claims, token, _KEY) == "ok"


def test_round_trip_preserves_the_panel_expiry() -> None:
    token = _token()
    claims = decode_artifact_token(token)

    assert claims is not None
    assert claims.view == "p"
    assert claims.expires_at == _EXPIRY


@pytest.mark.parametrize("view", ["g", "h"])
def test_visit_views_round_trip_without_an_expiry(view: str) -> None:
    """Visitor views are deterministic like ``r``: no ``x`` claim."""
    token = _token(view=view)
    claims = decode_artifact_token(token)

    assert claims is not None
    assert claims.view == view
    assert claims.expires_at is None
    assert verify_artifact_token(claims, token, _KEY) == "ok"


@pytest.mark.parametrize("view", ["g", "h"])
def test_visit_views_refuse_an_expiry(view: str) -> None:
    with pytest.raises(ValueError, match="panel-view"):
        _token(view=view, expires_at=_EXPIRY)


def test_tampered_payload_is_forged() -> None:
    token = _token()
    prefix, _payload, mac = token.split(".")
    claims = decode_artifact_token(token)
    assert claims is not None

    # Keep the original mac but point the payload at a different entry: the
    # claims parse cleanly, so only the signature can catch the swap.
    tampered = f"{prefix}.{_payload_segment(claims, e='other.html')}.{mac}"
    tampered_claims = decode_artifact_token(tampered)

    assert tampered_claims is not None
    assert tampered_claims.entry == "other.html"
    assert verify_artifact_token(tampered_claims, tampered, _KEY) == "forged"


def test_tampered_mac_is_forged() -> None:
    token = _token()
    prefix, payload, mac = token.split(".")
    flipped = ("A" if mac[0] != "A" else "B") + mac[1:]
    tampered = f"{prefix}.{payload}.{flipped}"
    claims = decode_artifact_token(tampered)

    assert claims is not None
    assert verify_artifact_token(claims, tampered, _KEY) == "forged"


def test_rotated_key_is_revoked() -> None:
    token = _token()
    claims = decode_artifact_token(token)
    assert claims is not None

    assert verify_artifact_token(claims, token, _ROTATED_KEY) == "revoked"


def test_missing_key_is_revoked() -> None:
    token = _token()
    claims = decode_artifact_token(token)
    assert claims is not None

    assert verify_artifact_token(claims, token, None) == "revoked"


def _payload_b64(payload: object) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


_VALID_PAYLOAD: dict[str, object] = {
    "s": "conv_abc123",
    "r": "reports",
    "b": 0,
    "e": "index.html",
    "k": "b",
    "v": "p",
    "i": "0123abcd",
    "w": 0,
    "x": _EXPIRY,
}

_MALFORMED_TOKENS = [
    "",
    "a1",
    "a1.",
    "a1..",
    "a1..mac.",
    "a1.payload.mac.extra",
    "a2.payload.mac",
    "A1.payload.mac",
    "a1.!!!.mac",
    "a1." + base64.urlsafe_b64encode(b"\xff\xfe").decode("ascii") + ".mac",
    "a1." + _payload_b64([1, 2, 3]) + ".mac",
    "a1." + _payload_b64("not-an-object") + ".mac",
    "a1." + _payload_b64({**_VALID_PAYLOAD, "i": None}) + ".mac",
    "a1." + _payload_b64({k: v for k, v in _VALID_PAYLOAD.items() if k != "i"}) + ".mac",
    "a1." + _payload_b64({**_VALID_PAYLOAD, "x": "extra"}) + ".mac",
    "a1." + _payload_b64({**_VALID_PAYLOAD, "b": True}) + ".mac",
    "a1." + _payload_b64({**_VALID_PAYLOAD, "b": 2}) + ".mac",
    "a1." + _payload_b64({**_VALID_PAYLOAD, "e": 42}) + ".mac",
    "a1." + _payload_b64({**_VALID_PAYLOAD, "k": "z"}) + ".mac",
    "a1." + _payload_b64({**_VALID_PAYLOAD, "v": "z"}) + ".mac",
    "a1." + _payload_b64({k: v for k, v in _VALID_PAYLOAD.items() if k != "w"}) + ".mac",
    "a1." + _payload_b64({**_VALID_PAYLOAD, "w": "0"}) + ".mac",
    "a1." + _payload_b64({**_VALID_PAYLOAD, "w": True}) + ".mac",
    "a1." + _payload_b64({**_VALID_PAYLOAD, "w": -1}) + ".mac",
    "a1." + _payload_b64({**_VALID_PAYLOAD, "w": 2**63}) + ".mac",
    # A panel token must carry its expiry; a raw-view token must not, and the
    # visitor views are raw-shaped.
    "a1." + _payload_b64({k: v for k, v in _VALID_PAYLOAD.items() if k != "x"}) + ".mac",
    "a1." + _payload_b64({**_VALID_PAYLOAD, "v": "r"}) + ".mac",
    "a1." + _payload_b64({**_VALID_PAYLOAD, "v": "g"}) + ".mac",
    "a1." + _payload_b64({**_VALID_PAYLOAD, "v": "h"}) + ".mac",
    # A raw-view token must not carry the key at all, even as null.
    "a1." + _payload_b64({**_VALID_PAYLOAD, "v": "r", "x": None}) + ".mac",
    # ``x`` must be a plain int inside the 64-bit range.
    "a1." + _payload_b64({**_VALID_PAYLOAD, "x": True}) + ".mac",
    "a1." + _payload_b64({**_VALID_PAYLOAD, "x": -1}) + ".mac",
    "a1." + _payload_b64({**_VALID_PAYLOAD, "x": 2**63}) + ".mac",
    "a1." + _payload_b64({**_VALID_PAYLOAD, "x": "1800000000"}) + ".mac",
]


@pytest.mark.parametrize("token", _MALFORMED_TOKENS)
def test_malformed_tokens_decode_to_none_without_raising(token: str) -> None:
    assert decode_artifact_token(token) is None


def test_panel_token_without_expiry_and_raw_token_with_expiry_decode_to_none() -> None:
    """The expiry claim is panel-only: each view refuses the other's shape."""
    panel_without_x = (
        "a1." + _payload_b64({k: v for k, v in _VALID_PAYLOAD.items() if k != "x"}) + ".mac"
    )
    raw_with_x = "a1." + _payload_b64({**_VALID_PAYLOAD, "v": "r"}) + ".mac"

    assert decode_artifact_token(panel_without_x) is None
    assert decode_artifact_token(raw_with_x) is None


def test_encode_refuses_an_expiry_on_a_raw_token() -> None:
    """A raw-view URL stays deterministic, so an expiry is a caller bug."""
    with pytest.raises(ValueError, match="panel-view"):
        _token(view="r", expires_at=_EXPIRY)


def test_non_string_token_decodes_to_none() -> None:
    assert decode_artifact_token(None) is None  # type: ignore[arg-type]


def test_deeply_nested_payload_decodes_to_none_without_raising() -> None:
    """A deeply nested JSON payload raises ``RecursionError`` inside
    ``json.loads``; the decoder runs on attacker-controlled tokens, so that
    too must return ``None`` instead of escaping."""
    nested = "[" * 10_000 + "]" * 10_000
    payload = base64.urlsafe_b64encode(nested.encode("ascii")).decode("ascii").rstrip("=")

    assert decode_artifact_token(f"a1.{payload}.mac") is None


def test_malformed_token_verifies_as_forged() -> None:
    token = _token()
    prefix, payload, _mac = token.split(".")
    claims = decode_artifact_token(token)
    assert claims is not None

    # A matching key id with a token that cannot carry a valid mac.
    assert verify_artifact_token(claims, f"{prefix}.{payload}.!!!", _KEY) == "forged"


def test_generate_session_key_is_32_random_bytes_base64url() -> None:
    first = generate_session_key()
    second = generate_session_key()

    assert len(first) == 43
    assert first != second
    assert len(session_key_bytes(first)) == 32
    assert all(c.isalnum() or c in "-_" for c in first)


def test_key_id_is_eight_hex_chars() -> None:
    assert len(key_id(_KEY)) == 8
    assert all(c in "0123456789abcdef" for c in key_id(_KEY))


def test_bridge_nonce_is_deterministic_per_token_and_key() -> None:
    token = _token()

    assert bridge_nonce(_KEY, token) == bridge_nonce(_KEY, token)
    assert len(bridge_nonce(_KEY, token)) == 16
    assert bridge_nonce(_KEY, token) != bridge_nonce(_ROTATED_KEY, token)
    assert bridge_nonce(_KEY, token) != bridge_nonce(_KEY, _token(entry="other.html"))
