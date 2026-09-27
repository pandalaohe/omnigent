"""Artifact-link token and per-session key primitives.

Artifact capability URLs carry a signed descriptor (session, workspace,
root, entry, kind, view) keyed by a random per-session secret stored in the
server-reserved :data:`ARTIFACT_LINK_KEY_LABEL` conversation label. This
module owns the wire format and the key lifecycle; the HTTP routes and the
serve path consume it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

from omnigent.stores.conversation_store import ARTIFACT_LINK_KEY_LABEL

if TYPE_CHECKING:
    from omnigent.stores.conversation_store import ConversationStore

_TOKEN_PREFIX = "a1"
_BRIDGE_CONTEXT = b"bridge"
_MAC_BYTES = 16
_SESSION_KEY_BYTES = 32
_PAYLOAD_KEYS = frozenset({"s", "r", "b", "e", "k", "v", "i", "w"})
# ``conversations.workspace_id`` is a signed 64-bit integer; an id above its
# range raises in the store lookup before the mac is even verified.
_WORKSPACE_ID_MAX = 2**63 - 1

ArtifactVerifyResult = Literal["ok", "revoked", "forged"]


@dataclass(frozen=True)
class ArtifactTokenClaims:
    """Descriptor carried by an artifact capability URL.

    :param session_id: Session the link belongs to, e.g. ``"conv_abc123"``.
    :param workspace_id: Workspace the link was minted in; the credential-free
        serve route rebinds it around every store / reader lookup.
    :param root: Bundle root — workspace-relative (posix, ``""`` = workspace
        root) when ``absolute`` is ``False``, absolute host path otherwise.
    :param absolute: ``True`` when ``root`` is an absolute host path (the
        owner-only absolute-read case).
    :param entry: Entry path relative to ``root``.
    :param kind: ``"b"`` for a bundle (an HTML entry; descendants allowed) or
        ``"f"`` for a single file.
    :param view: ``"p"`` for the panel view (in-frame scripts injected) or
        ``"r"`` for the raw standalone view.
    :param key_id: Identifier of the key that signed the token (see
        :func:`key_id`); distinguishes a revoked token (stale key id) from a
        forged one (bad mac under the current key).
    """

    session_id: str
    workspace_id: int
    root: str
    absolute: bool
    entry: str
    kind: Literal["b", "f"]
    view: Literal["p", "r"]
    key_id: str


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    # ``validate=True`` turns non-alphabet characters into an error instead of
    # silently ignoring them, so a corrupted payload segment never decodes.
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def key_id(session_key: bytes) -> str:
    """Return the identifier embedded in tokens signed by *session_key*."""
    return hashlib.sha256(session_key).hexdigest()[:8]


def encode_artifact_token(
    session_key: bytes,
    *,
    session_id: str,
    workspace_id: int,
    root: str,
    absolute: bool,
    entry: str,
    kind: Literal["b", "f"],
    view: Literal["p", "r"],
) -> str:
    """Encode the capability token for one artifact target.

    Wire form ``a1.<payload>.<mac>``: ``payload`` is compact JSON
    base64url-encoded without padding, ``mac`` is the first 16 bytes of
    HMAC-SHA256(session_key, ``a1.`` + payload) base64url without padding.

    :returns: The token, e.g. ``"a1.eyJzIjoi...NiI"``.
    """
    payload = _b64url_encode(
        json.dumps(
            {
                "s": session_id,
                "r": root,
                "b": 1 if absolute else 0,
                "e": entry,
                "k": kind,
                "v": view,
                "i": key_id(session_key),
                "w": workspace_id,
            },
            separators=(",", ":"),
        ).encode("utf-8")
    )
    signed = f"{_TOKEN_PREFIX}.{payload}"
    mac = _mac(session_key, signed)
    return f"{signed}.{_b64url_encode(mac)}"


def _mac(session_key: bytes, signed: str) -> bytes:
    """The truncated HMAC over one token's ``a1.<payload>`` segment."""
    return hmac.new(session_key, signed.encode("ascii"), hashlib.sha256).digest()[:_MAC_BYTES]


def decode_artifact_token(token: str) -> ArtifactTokenClaims | None:
    """Parse a token's payload without verifying its signature.

    Returns ``None`` for any malformed input — wrong prefix, bad base64, bad
    JSON, missing or mistyped fields, or ``kind`` / ``view`` outside their
    literal sets — and never raises. Callers pair this with
    :func:`verify_artifact_token` before trusting the claims.

    :param token: The wire token, e.g. ``"a1.eyJzIjoi...NiI"``.
    :returns: The claims, or ``None`` when the token cannot be parsed.
    """
    if not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != _TOKEN_PREFIX:
        return None
    try:
        payload = json.loads(_b64url_decode(parts[1]))
    except (ValueError, TypeError, RecursionError):
        return None
    if not isinstance(payload, dict) or set(payload) != _PAYLOAD_KEYS:
        return None
    session_id = payload["s"]
    root = payload["r"]
    entry = payload["e"]
    absolute = payload["b"]
    kind = payload["k"]
    view = payload["v"]
    kid = payload["i"]
    workspace_id = payload["w"]
    if not isinstance(session_id, str) or not isinstance(root, str) or not isinstance(entry, str):
        return None
    if not isinstance(kid, str):
        return None
    # bool is an int subclass; only the wire's 0 / 1 are valid.
    if isinstance(absolute, bool) or absolute not in (0, 1):
        return None
    # Workspace ids are non-negative ints within the column's range; a bool
    # would silently compare as 0 / 1 and aim the lookup at the wrong workspace.
    if (
        isinstance(workspace_id, bool)
        or not isinstance(workspace_id, int)
        or not 0 <= workspace_id <= _WORKSPACE_ID_MAX
    ):
        return None
    if kind not in ("b", "f") or view not in ("p", "r"):
        return None
    return ArtifactTokenClaims(
        session_id=session_id,
        workspace_id=workspace_id,
        root=root,
        absolute=bool(absolute),
        entry=entry,
        kind=cast(Literal["b", "f"], kind),
        view=cast(Literal["p", "r"], view),
        key_id=kid,
    )


def verify_artifact_token(
    claims: ArtifactTokenClaims,
    token: str,
    session_key: bytes | None,
) -> ArtifactVerifyResult:
    """Classify a decoded token against the session's current key.

    ``None`` key (never minted, or deleted by archive) or a key-id mismatch
    (rotated / archived) → ``"revoked"``. A matching key id with a bad mac →
    ``"forged"``. A malformed token cannot carry a valid mac, so it too
    classifies as ``"forged"`` instead of raising.

    :param claims: Claims from :func:`decode_artifact_token`.
    :param token: The token those claims were decoded from.
    :param session_key: The session's current key bytes, or ``None``.
    :returns: ``"ok"``, ``"revoked"``, or ``"forged"``.
    """
    if session_key is None or claims.key_id != key_id(session_key):
        return "revoked"
    if not isinstance(token, str):
        return "forged"
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != _TOKEN_PREFIX:
        return "forged"
    try:
        expected = _mac(session_key, f"{parts[0]}.{parts[1]}")
        actual = _b64url_decode(parts[2])
    except (UnicodeEncodeError, ValueError, TypeError):
        return "forged"
    if not hmac.compare_digest(expected, actual):
        return "forged"
    return "ok"


def generate_session_key() -> str:
    """Return a fresh session key: 32 random bytes, base64url, 43 chars.

    The encoded form fits the 256-char conversation-label value cap.
    """
    return _b64url_encode(secrets.token_bytes(_SESSION_KEY_BYTES))


def session_key_bytes(value: str) -> bytes:
    """Decode a stored :data:`ARTIFACT_LINK_KEY_LABEL` value to raw key bytes."""
    return _b64url_decode(value)


def get_or_create_artifact_key(store: ConversationStore, session_id: str) -> bytes:
    """Return the session's artifact-link key, creating it on first use.

    Creation is first-writer-wins through
    :meth:`ConversationStore.insert_label_if_absent`, so concurrent first
    mints converge on the key one of them stored. The store call is sync —
    routes invoke it through ``asyncio.to_thread``, like ``set_labels``.

    :param store: The conversation store holding the session.
    :param session_id: Session/conversation identifier.
    :returns: The raw key every token of this session is signed with.
    """
    stored = store.insert_label_if_absent(
        session_id, ARTIFACT_LINK_KEY_LABEL, generate_session_key()
    )
    return session_key_bytes(stored)


def rotate_artifact_key(store: ConversationStore, session_id: str) -> None:
    """Replace the session's key with a fresh one, revoking every old link."""
    store.set_labels(session_id, {ARTIFACT_LINK_KEY_LABEL: generate_session_key()})


def read_artifact_key(labels: dict[str, str]) -> bytes | None:
    """Return the artifact-link key carried by a conversation's labels.

    :param labels: Stored conversation labels.
    :returns: The raw key bytes, or ``None`` when absent or undecodable
        (either way every token verifies as revoked).
    """
    value = labels.get(ARTIFACT_LINK_KEY_LABEL)
    if not value:
        return None
    try:
        return session_key_bytes(value)
    except (ValueError, TypeError):
        return None


def bridge_nonce(session_key: bytes, token: str) -> str:
    """Return the 16-hex nonce injected into panel pages served under *token*.

    The nonce lets the in-frame comment bridge prove it was served by this
    server for this token without carrying the token's own mac.
    """
    return hmac.new(
        session_key, _BRIDGE_CONTEXT + token.encode("utf-8"), hashlib.sha256
    ).hexdigest()[:16]
