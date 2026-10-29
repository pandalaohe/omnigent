"""Codex-native runtime approval presets — mirrors Codex's ``/permissions`` popup.

The interactive ``codex`` TUI switches approval stance through its own
``/permissions`` popup, NOT through the app-server ``thread/settings/update``
RPC (that path drives model/effort but is a no-op for approval). So Omnigent's
running-session switcher drives the popup by keystroke: type ``/permissions``,
then the option's menu digit (position-independent, unlike arrow navigation),
then confirm the sub-dialog for the ones that ask.

These presets use the popup's labels. Menu digits are discovered from the
rendered popup because row positions vary by platform and configuration.

Read Only is absent from the default macOS/Linux popup, but appears on Windows
and with permission profiles. The full-bypass launch flag has no popup row.

Kept dependency-free so the server routes, the runner, and the web contract can
all agree on the same list.
"""

from __future__ import annotations

import re
from base64 import b64decode, urlsafe_b64encode
from binascii import Error as Base64Error
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from typing import cast


@dataclass(frozen=True)
class CodexPermissionPreset:
    """One row of Codex's ``/permissions`` popup.

    :param value: Stable slug stored on the label / sent over the wire.
    :param label: Exact popup label, shown in the web picker too.
    :param description: The popup's own one-line explanation.
    :param needs_confirm: Whether the row opens a "Yes, continue anyway"
        sub-dialog that must be accepted (Full Access does).
    """

    value: str
    label: str
    description: str
    needs_confirm: bool


# Labels match Codex's ``/permissions`` popup. Row positions are discovered at
# runtime because they vary by platform and configuration.
CODEX_NATIVE_PERMISSION_PRESETS: tuple[CodexPermissionPreset, ...] = (
    CodexPermissionPreset(
        value="ask-for-approval",
        label="Ask for approval",
        description="Read/edit/run in the workspace; approval for the internet or external edits",
        needs_confirm=False,
    ),
    CodexPermissionPreset(
        value="approve-for-me",
        label="Approve for me",
        description="Only asks for actions detected as potentially unsafe",
        needs_confirm=False,
    ),
    CodexPermissionPreset(
        value="full-access",
        label="Full Access",
        description="Edit any file and access the internet without approval",
        needs_confirm=True,
    ),
    # Read Only is absent from the default macOS/Linux popup; it appears on
    # Windows and in the permission-profiles popup variant.
    CodexPermissionPreset(
        value="read-only",
        label="Read Only",
        description="Read files only; approval required to edit files or access the internet",
        needs_confirm=False,
    ),
)

CODEX_NATIVE_PERMISSION_VALUES: frozenset[str] = frozenset(
    preset.value for preset in CODEX_NATIVE_PERMISSION_PRESETS
)


def codex_permission_preset(value: str) -> CodexPermissionPreset | None:
    """:returns: The preset for *value*, or ``None`` when it is not a preset."""
    return next((p for p in CODEX_NATIVE_PERMISSION_PRESETS if p.value == value), None)


_MENU_ROW_RE = re.compile(r"^\s*(?:›\s*)?(\d+)\.\s+(.*)$")
_CURRENT_SUFFIX_RE = re.compile(r"\s*\(current\)\s*$", re.IGNORECASE)
_CONFIRMATION_MARKERS = (
    "Permissions updated to ",
    "Permission selection requested: ",
)


def codex_permissions_menu(pane_text: str) -> dict[str, str]:
    """Return ``{label: digit}`` for a captured ``/permissions`` popup."""
    rows: dict[str, str] = {}
    for line in pane_text.splitlines():
        match = _MENU_ROW_RE.match(line)
        if match is None:
            continue
        digit, remainder = match.groups()
        label = re.split(r"\s{2,}", remainder, maxsplit=1)[0].strip()
        label = _CURRENT_SUFFIX_RE.sub("", label).strip()
        if label:
            rows[label] = digit
    return rows


def codex_permission_switch_confirmed(pane_text: str, label: str) -> bool:
    """Return whether Codex acknowledged the latest permission selection."""
    confirmations = [
        line.split(marker, 1)[1].strip()
        for line in pane_text.splitlines()
        for marker in _CONFIRMATION_MARKERS
        if marker in line
    ]
    return bool(confirmations) and confirmations[-1] == label


# Sandbox ``type`` spellings Codex uses (the app-server ``thread/settings/updated``
# notification uses camelCase; other paths hyphenate).
_FULL_ACCESS_SANDBOX_TYPES = frozenset({"dangerFullAccess", "danger-full-access"})
_READ_ONLY_SANDBOX_TYPES = frozenset({"readOnly", "read-only"})


def codex_permission_preset_from_thread_settings(settings: object) -> str | None:
    """Map a Codex ``threadSettings`` payload to a ``/permissions`` preset value.

    Reads the approval fields Codex emits on a ``thread/settings/updated`` (and
    ``thread/resume``) notification. Presets are distinguished by, in order: a
    full-access sandbox/profile/``never`` policy → ``full-access``; a read-only
    sandbox/profile → ``read-only``; an ``auto_review`` reviewer under on-request
    → ``approve-for-me``; a plain on-request policy → ``ask-for-approval``.
    Returns ``None`` when the payload doesn't resolve to a preset (e.g. a custom
    profile).

    :param settings: The ``threadSettings`` mapping (or anything, defensively).
    :returns: A value from :data:`CODEX_NATIVE_PERMISSION_VALUES`, or ``None``.
    """
    if not isinstance(settings, Mapping):
        return None
    approval_policy = settings.get("approvalPolicy")
    reviewer = settings.get("approvalsReviewer")
    sandbox = settings.get("sandboxPolicy")
    sandbox_type = sandbox.get("type") if isinstance(sandbox, Mapping) else None
    if not isinstance(sandbox_type, str):
        sandbox_type = None
    profile = settings.get("activePermissionProfile")
    profile_id = profile.get("id") if isinstance(profile, Mapping) else None
    if (
        sandbox_type in _FULL_ACCESS_SANDBOX_TYPES
        or profile_id in (":danger-full-access", ":danger-no-sandbox")
        or approval_policy == "never"
    ):
        return "full-access"
    if sandbox_type in _READ_ONLY_SANDBOX_TYPES or profile_id == ":read-only":
        return "read-only"
    if reviewer == "auto_review":
        return "approve-for-me"
    if approval_policy == "on-request":
        return "ask-for-approval"
    return None


# The order is the c1 storage/wire contract, including the nullable zero codes.
_OBSERVATION_CODES: dict[str, tuple[str | None, ...]] = {
    "source": ("native-settings", "application-receipt"),
    "origin": ("resume", "settings-updated", "web-switch"),
    "mode": (
        "unknown",
        "custom",
        "ask-for-approval",
        "approve-for-me",
        "full-access",
        "read-only",
    ),
    "approval": (None, "on-request", "never", "on-failure", "untrusted", "granular"),
    "reviewer": (None, "user", "auto_review", "guardian_subagent"),
    "sandbox": (None, "read-only", "workspace-write", "danger-full-access", "external-sandbox"),
    "profile": (None, "read-only", "workspace", "full-access", "custom"),
    "uncertainty": (
        None,
        "missing-fields",
        "unsupported-value",
        "conflicting-fields",
        "no-settings",
        "application-receipt",
    ),
    "requested_mode": (None, "ask-for-approval", "approve-for-me", "full-access", "read-only"),
}
_PROFILE_SANDBOX = {
    "read-only": "read-only",
    "workspace": "workspace-write",
    "full-access": "danger-full-access",
}
_PROFILE_IDS = {
    ":read-only": "read-only",
    ":workspace": "workspace",
    ":danger-full-access": "full-access",
    ":danger-no-sandbox": "full-access",
}
_SANDBOX_SPELLINGS = {
    "readOnly": "read-only",
    "read-only": "read-only",
    "workspaceWrite": "workspace-write",
    "workspace-write": "workspace-write",
    "dangerFullAccess": "danger-full-access",
    "danger-full-access": "danger-full-access",
    "externalSandbox": "external-sandbox",
    "external-sandbox": "external-sandbox",
}
CODEX_PERMISSION_OBSERVATION_MAX_LEN = 212
_MAX_RECEIVED_AT = (1 << 63) - 1


@dataclass(frozen=True)
class CodexPermissionObservation:
    """Finite readback categories, never a control request or native attestation.

    ``received_at`` and ``runner_id`` belong to the receiving application; a
    producer payload cannot supply them. Values are validated on construction.
    """

    source: str = "native-settings"
    origin: str = "resume"
    mode: str = "unknown"
    approval: str | None = None
    reviewer: str | None = None
    sandbox: str | None = None
    profile: str | None = None
    uncertainty: str | None = "no-settings"
    requested_mode: str | None = None
    received_at: int | None = None
    runner_id: str | None = None
    version: int = 1

    def __post_init__(self) -> None:
        _validate_permission_observation(self)

    def to_dict(self) -> dict[str, object]:
        """Return the readable event shape, including receiver-owned metadata."""
        return asdict(self)

    def to_payload(self) -> dict[str, object]:
        """Return only producer-owned fields; receipts are application-owned."""
        if self.source != "native-settings":
            raise ValueError("application receipts are not producer observations")
        return {"version": self.version, **{key: getattr(self, key) for key in _OBSERVATION_CODES}}


def _mode_for_permission_axes(
    approval: str | None, reviewer: str | None, sandbox: str | None, profile: str | None
) -> tuple[str, str | None]:
    profile_sandbox = _PROFILE_SANDBOX.get(profile or "")
    if sandbox is not None and profile_sandbox is not None and sandbox != profile_sandbox:
        return "unknown", "conflicting-fields"
    boundary = sandbox or profile_sandbox
    if approval is None or (boundary is None and profile != "custom"):
        return "unknown", "missing-fields"
    if profile == "custom":
        return "custom", None
    if approval == "never" and boundary == "danger-full-access":
        return "full-access", None
    if approval == "on-request" and boundary in {"read-only", "workspace-write"}:
        if reviewer is None:
            return "unknown", "missing-fields"
        if reviewer == "user":
            return ("read-only" if boundary == "read-only" else "ask-for-approval"), None
        if reviewer == "auto_review" and boundary == "workspace-write":
            return "approve-for-me", None
    return "custom", None


def _valid_observation_runner_id(value: object) -> bool:
    if value is None:
        return True
    if not isinstance(value, str) or not value:
        return False
    try:
        return len(value.encode("utf-8")) <= 128 and not any(
            ord(char) <= 31 or 127 <= ord(char) <= 159 for char in value
        )
    except UnicodeEncodeError:
        return False


def _validate_permission_observation(value: CodexPermissionObservation) -> None:
    if type(value.version) is not int or value.version != 1:
        raise ValueError("unsupported permission observation version")
    for key, codes in _OBSERVATION_CODES.items():
        field = getattr(value, key)
        if (field is not None and not isinstance(field, str)) or field not in codes:
            raise ValueError(f"invalid permission observation {key}")
    if value.received_at is not None and (
        type(value.received_at) is not int or not 0 <= value.received_at <= _MAX_RECEIVED_AT
    ):
        raise ValueError("invalid permission observation received_at")
    if not _valid_observation_runner_id(value.runner_id):
        raise ValueError("invalid permission observation runner_id")
    axes = (value.approval, value.reviewer, value.sandbox, value.profile)
    if value.source == "application-receipt":
        if (
            value.origin != "web-switch"
            or value.mode != "unknown"
            or value.requested_mode not in CODEX_NATIVE_PERMISSION_VALUES
            or any(axis is not None for axis in axes)
            or value.uncertainty not in {"application-receipt", "unsupported-value"}
        ):
            raise ValueError("invalid application receipt combination")
        return
    if value.origin == "web-switch" or value.requested_mode is not None:
        raise ValueError("native settings cannot describe a control receipt")
    expected_mode, expected_uncertainty = _mode_for_permission_axes(*axes)
    match value.uncertainty:
        case "no-settings":
            valid = value.mode == "unknown" and all(axis is None for axis in axes)
        case "conflicting-fields":
            # Conflicts can also be between raw aliases, which are not retained.
            valid = value.mode == "unknown"
        case "unsupported-value":
            # Unmodelled access details and unrepresentable receiver context
            # cannot name a preset, even when the known axes match one.
            valid = value.mode == "unknown" or (
                value.mode == "custom"
                and value.approval is not None
                and (value.sandbox is not None or value.profile == "custom")
                and expected_uncertainty != "conflicting-fields"
            )
        case _:
            valid = (value.mode, value.uncertainty) == (expected_mode, expected_uncertainty)
    if not valid:
        raise ValueError("inconsistent permission observation mode or uncertainty")


def _observation_from_values(
    values: list[object],
    *,
    version: int = 1,
    received_at: int | None = None,
    runner_id: str | None = None,
) -> CodexPermissionObservation:
    return CodexPermissionObservation(
        source=cast(str, values[0]),
        origin=cast(str, values[1]),
        mode=cast(str, values[2]),
        approval=cast(str | None, values[3]),
        reviewer=cast(str | None, values[4]),
        sandbox=cast(str | None, values[5]),
        profile=cast(str | None, values[6]),
        uncertainty=cast(str | None, values[7]),
        requested_mode=cast(str | None, values[8]),
        received_at=received_at,
        runner_id=runner_id,
        version=version,
    )


def codex_permission_observation_from_payload(payload: object) -> CodexPermissionObservation:
    """Validate a producer object without accepting metadata or control fields."""
    if not isinstance(payload, Mapping) or set(payload) != {"version", *_OBSERVATION_CODES}:
        raise ValueError("permission payload requires only the declared producer fields")
    observed = _observation_from_values(
        [payload[key] for key in _OBSERVATION_CODES], version=cast(int, payload["version"])
    )
    if observed.source != "native-settings":
        raise ValueError("application receipts cannot arrive as producer observations")
    return observed


def codex_permission_application_receipt(requested_mode: str) -> CodexPermissionObservation:
    """Describe an application response, without claiming fresh native settings."""
    return CodexPermissionObservation(
        source="application-receipt",
        origin="web-switch",
        requested_mode=requested_mode,
        uncertainty="application-receipt",
    )


def codex_permission_observation_with_metadata(
    observed: CodexPermissionObservation, *, received_at: int | None, runner_id: str | None
) -> CodexPermissionObservation:
    """Attach receiver-supplied context, without inspecting configuration or time.

    The caller owns provenance/authentication. An unrepresentable binding cannot
    be truncated into a different identity or retain a reported preset name.
    """
    if not _valid_observation_runner_id(runner_id):
        return replace(
            observed,
            mode="unknown",
            uncertainty="unsupported-value",
            received_at=received_at,
            runner_id=None,
        )
    return replace(observed, received_at=received_at, runner_id=runner_id)


def _permission_setting(
    settings: Mapping[object, object], camel: str, snake: str
) -> tuple[object, bool]:
    if camel in settings and snake in settings and settings[camel] != settings[snake]:
        return None, True
    return settings.get(camel, settings.get(snake)), False


def _sandbox_has_unmodelled_access(sandbox: Mapping[object, object]) -> bool:
    kind = sandbox.get("type")
    if not isinstance(kind, str) or kind not in {"workspaceWrite", "workspace-write"}:
        return bool(set(sandbox) - {"type"})
    for key, value in sandbox.items():
        if key == "type":
            continue
        if key in {"networkAccess", "network_access"}:
            if value is not False:
                return True
        elif key in {"writableRoots", "writable_roots"}:
            # Without workspace context, extra writable roots are not an exact
            # built-in preset, even if their element type is recognised.
            if value != []:
                return True
        elif key in {
            "excludeTmpdirEnvVar",
            "exclude_tmpdir_env_var",
            "excludeSlashTmp",
            "exclude_slash_tmp",
        }:
            if type(value) is not bool:
                return True
        else:
            return True
    return False


def codex_permission_observation_from_thread_settings(
    settings: object, *, origin: str
) -> CodexPermissionObservation:
    """Classify one frame, with no defaults, cached axes, control args or I/O."""
    if not isinstance(origin, str) or origin not in {"resume", "settings-updated"}:
        raise ValueError("native observation origin must be resume or settings-updated")
    if not isinstance(settings, Mapping):
        return CodexPermissionObservation(
            origin=origin, uncertainty="no-settings" if settings is None else "unsupported-value"
        )
    pairs = (
        ("approvalPolicy", "approval_policy"),
        ("approvalsReviewer", "approvals_reviewer"),
        ("sandboxPolicy", "sandbox_policy"),
        ("activePermissionProfile", "active_permission_profile"),
    )
    if not any(key in settings for pair in pairs for key in pair):
        return CodexPermissionObservation(origin=origin)
    fields = [_permission_setting(settings, *pair) for pair in pairs]
    (raw_approval, _), (raw_reviewer, _), (raw_sandbox, _), (raw_profile, _) = fields
    conflict = any(conflicted for _, conflicted in fields)
    unsupported = False
    custom_access = False
    approval = (
        raw_approval
        if isinstance(raw_approval, str) and raw_approval in _OBSERVATION_CODES["approval"]
        else None
    )
    if isinstance(raw_approval, Mapping) and set(raw_approval) == {"granular"}:
        granular = raw_approval["granular"]
        if isinstance(granular, Mapping) and all(
            isinstance(key, str) and type(val) is bool for key, val in granular.items()
        ):
            approval = "granular"
    unsupported |= raw_approval is not None and approval is None
    reviewer = (
        raw_reviewer
        if isinstance(raw_reviewer, str) and raw_reviewer in _OBSERVATION_CODES["reviewer"]
        else None
    )
    unsupported |= raw_reviewer is not None and reviewer is None
    sandbox = None
    if isinstance(raw_sandbox, Mapping):
        conflict |= any(
            _permission_setting(raw_sandbox, *pair)[1]
            for pair in (
                ("excludeSlashTmp", "exclude_slash_tmp"),
                ("excludeTmpdirEnvVar", "exclude_tmpdir_env_var"),
            )
        )
        raw_type = raw_sandbox.get("type")
        sandbox = _SANDBOX_SPELLINGS.get(raw_type) if isinstance(raw_type, str) else None
        custom_access = _sandbox_has_unmodelled_access(raw_sandbox)
    unsupported |= raw_sandbox is not None and sandbox is None
    profile = None
    if isinstance(raw_profile, Mapping):
        profile_id = raw_profile.get("id")
        if isinstance(profile_id, str) and profile_id:
            profile = _PROFILE_IDS.get(profile_id, "custom")
            if raw_profile.get("extends") is not None or set(raw_profile) - {"id", "extends"}:
                profile = "custom"
    unsupported |= raw_profile is not None and profile is None
    if sandbox is None and raw_sandbox is None:
        sandbox = _PROFILE_SANDBOX.get(profile or "")
    mode, uncertainty = _mode_for_permission_axes(approval, reviewer, sandbox, profile)
    if conflict or uncertainty == "conflicting-fields":
        mode, uncertainty = "unknown", "conflicting-fields"
    elif unsupported:
        mode, uncertainty = "unknown", "unsupported-value"
    elif custom_access and uncertainty != "missing-fields":
        mode, uncertainty = "custom", "unsupported-value"
    return CodexPermissionObservation(
        origin=origin,
        mode=mode,
        approval=approval,
        reviewer=reviewer,
        sandbox=sandbox,
        profile=profile,
        uncertainty=uncertainty,
    )


def encode_codex_permission_observation(observed: CodexPermissionObservation) -> str:
    """Encode c1 within 212 ASCII characters; never rely on storage clipping."""
    if not isinstance(observed, CodexPermissionObservation):
        raise ValueError("permission observation must be a validated observation")
    _validate_permission_observation(observed)
    codes = [
        str(values.index(getattr(observed, key))) for key, values in _OBSERVATION_CODES.items()
    ]
    timestamp = "-" if observed.received_at is None else str(observed.received_at)
    runner = (
        "-"
        if observed.runner_id is None
        else urlsafe_b64encode(observed.runner_id.encode("utf-8")).decode("ascii").rstrip("=")
    )
    encoded = "c1:" + ",".join([*codes, timestamp, runner])
    if not encoded.isascii() or len(encoded) > CODEX_PERMISSION_OBSERVATION_MAX_LEN:
        raise ValueError("permission observation exceeds its storage bound")
    return encoded


def decode_codex_permission_observation(encoded: str) -> CodexPermissionObservation:
    """Reject malformed/unknown/truncated observations instead of guessing a preset."""
    if (
        not isinstance(encoded, str)
        or not encoded.isascii()
        or not encoded.startswith("c1:")
        or len(encoded) > CODEX_PERMISSION_OBSERVATION_MAX_LEN
    ):
        raise ValueError("invalid permission observation encoding")
    parts = encoded[3:].split(",")
    if len(parts) != 11:
        raise ValueError("permission observation requires eleven fields")
    values: list[object] = []
    for code, allowed in zip(parts[:9], _OBSERVATION_CODES.values(), strict=True):
        if len(code) != 1 or code not in "0123456789" or int(code) >= len(allowed):
            raise ValueError("invalid permission observation code")
        values.append(allowed[int(code)])
    timestamp = parts[9]
    if timestamp != "-" and re.fullmatch(r"0|[1-9][0-9]{0,18}", timestamp) is None:
        raise ValueError("invalid permission observation timestamp")
    runner = None
    if parts[10] != "-":
        if re.fullmatch(r"[A-Za-z0-9_-]{2,171}", parts[10]) is None:
            raise ValueError("invalid permission observation binding encoding")
        try:
            raw = b64decode(parts[10] + "=" * (-len(parts[10]) % 4), altchars=b"-_", validate=True)
            runner = raw.decode("utf-8", errors="strict")
        except (Base64Error, UnicodeDecodeError) as exc:
            raise ValueError("invalid permission observation binding encoding") from exc
    observed = _observation_from_values(
        values, received_at=None if timestamp == "-" else int(timestamp), runner_id=runner
    )
    if encode_codex_permission_observation(observed) != encoded:
        raise ValueError("noncanonical permission observation encoding")
    return observed
