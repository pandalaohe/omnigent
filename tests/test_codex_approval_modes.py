"""Unit tests for the codex-native ``/permissions`` presets."""

from __future__ import annotations

from dataclasses import replace
from itertools import product
from typing import Any, cast

import pytest

from omnigent import codex_approval_modes as modes
from omnigent.codex_approval_modes import (
    CODEX_NATIVE_PERMISSION_PRESETS,
    CODEX_NATIVE_PERMISSION_VALUES,
    codex_permission_preset,
    codex_permission_preset_from_thread_settings,
    codex_permission_switch_confirmed,
    codex_permissions_menu,
)

# Real ``threadSettings`` payloads captured from codex-cli 0.146.0's
# ``thread/settings/updated`` for each /permissions preset (trimmed to the
# approval fields the mapper reads).
_ASK_FOR_APPROVAL_SETTINGS = {
    "approvalPolicy": "on-request",
    "approvalsReviewer": "user",
    "sandboxPolicy": {"type": "workspaceWrite"},
    "activePermissionProfile": {"id": ":workspace", "extends": None},
}
_APPROVE_FOR_ME_SETTINGS = {
    "approvalPolicy": "on-request",
    "approvalsReviewer": "auto_review",
    "sandboxPolicy": {"type": "workspaceWrite"},
    "activePermissionProfile": {"id": ":workspace", "extends": None},
}
_FULL_ACCESS_SETTINGS = {
    "approvalPolicy": "never",
    "approvalsReviewer": "user",
    "sandboxPolicy": {"type": "dangerFullAccess"},
    "activePermissionProfile": {"id": ":danger-full-access", "extends": None},
}

# Real captured ``/permissions`` popup snapshots (verbatim from codex render
# snapshots). Row order and presence vary by platform/feature/variant, which is
# why the selecting digit is parsed from the popup rather than hardcoded.

# Permission-profiles popup variant: all four rows, `` (current)`` on row 1.
_PROFILE_POPUP = """\
  Update Model Permissions


› 1. Ask for approval (current)  Read and edit workspace files and run commands,
                                 with approval required for internet access or
                                 edits outside the workspace
  2. Approve for me              Only ask for actions detected as potentially
                                 unsafe
  3. Full Access                 Use with caution: Codex can edit files outside
                                 this workspace and access the internet without
                                 approval
  4. Read Only                   Read workspace files, with approval required
                                 for edits or internet access

  enter select · esc back
"""

# Default macOS/Linux popup with Guardian off: two rows, NO Read Only.
_LINUX_DEFAULT_POPUP = """\
  Update Model Permissions


› 1. Ask for approval  Read and edit workspace files and run commands, with
                       approval required for internet access or edits outside
                       the workspace
  2. Full Access       Use with caution: Codex can edit files outside this
                       workspace and access the internet without approval

  enter select · esc back
"""

# Windows popup: Read Only present as row 1, with `` (current)``.
_WINDOWS_POPUP = """\
› 1. Read Only (current)  Read workspace files, with approval required for edits
                          or internet access
  2. Ask for approval     Read and edit workspace files and run commands, with
                          approval required for internet access or edits outside
  3. Full Access          Use with caution: Codex can edit files outside this
                          workspace and access the internet without approval

  enter select · esc back
"""


def test_values_match_the_preset_list() -> None:
    """The value set is exactly the presets' values."""
    assert {p.value for p in CODEX_NATIVE_PERMISSION_PRESETS} == CODEX_NATIVE_PERMISSION_VALUES


def test_only_full_access_needs_confirm() -> None:
    """Full Access is the one preset with a confirm sub-dialog."""
    confirming = [p.value for p in CODEX_NATIVE_PERMISSION_PRESETS if p.needs_confirm]
    assert confirming == ["full-access"]


@pytest.mark.parametrize("value", sorted(CODEX_NATIVE_PERMISSION_VALUES))
def test_lookup_round_trips(value: str) -> None:
    """codex_permission_preset resolves every known value."""
    preset = codex_permission_preset(value)
    assert preset is not None
    assert preset.value == value


def test_lookup_unknown_is_none() -> None:
    """An unknown value resolves to None (rejected upstream)."""
    assert codex_permission_preset("bypass") is None
    assert codex_permission_preset("turbo") is None


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        (_ASK_FOR_APPROVAL_SETTINGS, "ask-for-approval"),
        (_APPROVE_FOR_ME_SETTINGS, "approve-for-me"),
        (_FULL_ACCESS_SETTINGS, "full-access"),
        # Read-only signature (camelCase sandbox type, as the notification emits).
        (
            {
                "approvalPolicy": "on-request",
                "approvalsReviewer": "user",
                "sandboxPolicy": {"type": "readOnly"},
                "activePermissionProfile": {"id": ":read-only", "extends": None},
            },
            "read-only",
        ),
    ],
)
def test_preset_from_thread_settings_maps_each_popup_choice(settings: dict, expected: str) -> None:
    """Each /permissions choice's threadSettings maps back to its preset value."""
    assert codex_permission_preset_from_thread_settings(settings) == expected


def test_preset_from_thread_settings_none_for_unmapped() -> None:
    """A non-mapping / custom payload resolves to None rather than guessing."""
    assert codex_permission_preset_from_thread_settings(None) is None
    assert codex_permission_preset_from_thread_settings({}) is None


@pytest.mark.parametrize("profile_id", [":danger-no-sandbox", ":danger-full-access"])
def test_full_access_profile_without_redundant_sandbox_or_policy(profile_id: str) -> None:
    """Both Codex profile names identify Full Access on their own."""
    assert (
        codex_permission_preset_from_thread_settings(
            {"activePermissionProfile": {"id": profile_id}, "approvalPolicy": "on-request"}
        )
        == "full-access"
    )


def test_parse_profile_popup_all_four_rows() -> None:
    """The profile-popup variant parses all four rows, stripping `` (current)``."""
    assert codex_permissions_menu(_PROFILE_POPUP) == {
        "Ask for approval": "1",
        "Approve for me": "2",
        "Full Access": "3",
        "Read Only": "4",
    }


def test_parse_linux_default_popup_two_rows() -> None:
    """The default macOS/Linux popup (Guardian off) parses its two rows."""
    assert codex_permissions_menu(_LINUX_DEFAULT_POPUP) == {
        "Ask for approval": "1",
        "Full Access": "2",
    }


@pytest.mark.parametrize("pane", ["", "some unrelated transcript text\nno menu here"])
def test_parse_non_popup_is_empty(pane: str) -> None:
    """A pane without option rows parses to an empty list."""
    assert codex_permissions_menu(pane) == {}


def test_menu_digit_linux_default_read_only_absent() -> None:
    """Read Only is absent from the default macOS/Linux popup → no digit."""
    menu = codex_permissions_menu(_LINUX_DEFAULT_POPUP)
    assert menu.get("Read Only") is None
    assert menu["Full Access"] == "2"


def test_menu_digit_windows_read_only_first() -> None:
    """On Windows, Read Only is row 1 (with a `` (current)`` suffix)."""
    assert codex_permissions_menu(_WINDOWS_POPUP)["Read Only"] == "1"


@pytest.mark.parametrize(
    "message",
    [
        "• Permissions updated to Full Access",
        "• Permission selection requested: Full Access",
    ],
)
def test_permission_switch_confirmation_messages(message: str) -> None:
    """Both Codex confirmation spellings identify the selected label."""
    assert codex_permission_switch_confirmed(message, "Full Access")
    assert not codex_permission_switch_confirmed(message, "Read Only")


def test_observation_never_does_not_remove_read_only_boundary() -> None:
    observed = modes.codex_permission_observation_from_thread_settings(
        {"approvalPolicy": "never", "sandboxPolicy": {"type": "readOnly"}},
        origin="settings-updated",
    )
    assert observed.mode == "custom"
    assert observed.approval == "never"
    assert observed.sandbox == "read-only"


@pytest.mark.parametrize(
    ("settings", "mode", "uncertainty"),
    [
        (_ASK_FOR_APPROVAL_SETTINGS, "ask-for-approval", None),
        (_APPROVE_FOR_ME_SETTINGS, "approve-for-me", None),
        (_FULL_ACCESS_SETTINGS, "full-access", None),
        (
            {
                "approvalPolicy": "on-request",
                "approvalsReviewer": "user",
                "sandboxPolicy": {"type": "readOnly"},
            },
            "read-only",
            None,
        ),
        ({"approvalPolicy": "never"}, "unknown", "missing-fields"),
        ({"sandboxPolicy": {"type": "dangerFullAccess"}}, "unknown", "missing-fields"),
        (
            {"approvalPolicy": "on-request", "sandboxPolicy": {"type": "workspaceWrite"}},
            "unknown",
            "missing-fields",
        ),
        (
            {"approvalPolicy": "on-request", "sandboxPolicy": {"type": "dangerFullAccess"}},
            "custom",
            None,
        ),
        (
            {
                "approvalPolicy": "on-request",
                "approvalsReviewer": "guardian_subagent",
                "sandboxPolicy": {"type": "workspaceWrite"},
            },
            "custom",
            None,
        ),
        ({"approvalPolicy": "on-failure", "sandboxPolicy": {"type": "read-only"}}, "custom", None),
        (
            {"approvalPolicy": "untrusted", "sandboxPolicy": {"type": "workspace-write"}},
            "custom",
            None,
        ),
        (
            {
                "approvalPolicy": {"granular": {"sandbox_approval": True}},
                "sandboxPolicy": {"type": "workspaceWrite"},
            },
            "custom",
            None,
        ),
        (
            {"approvalPolicy": "never", "sandboxPolicy": {"type": "externalSandbox"}},
            "custom",
            None,
        ),
        (
            {"approvalPolicy": "never", "activePermissionProfile": {"id": ":danger-no-sandbox"}},
            "full-access",
            None,
        ),
        (
            {"approvalPolicy": "never", "activePermissionProfile": {"id": ":workspace"}},
            "custom",
            None,
        ),
        (
            {
                "approvalPolicy": "never",
                "sandboxPolicy": {"type": "readOnly"},
                "activePermissionProfile": {"id": ":danger-full-access"},
            },
            "unknown",
            "conflicting-fields",
        ),
        (
            {
                "approvalPolicy": "never",
                "approval_policy": "on-request",
                "sandboxPolicy": {"type": "dangerFullAccess"},
            },
            "unknown",
            "conflicting-fields",
        ),
        (
            {
                "approvalPolicy": "never",
                "activePermissionProfile": {"id": "test-custom", "extends": ":danger-full-access"},
            },
            "custom",
            None,
        ),
        (
            {
                "approvalPolicy": "never",
                "sandboxPolicy": {"type": "dangerFullAccess", "access": {"type": "future"}},
            },
            "custom",
            "unsupported-value",
        ),
        (
            {
                "approvalPolicy": "never",
                "sandboxPolicy": {"type": "workspaceWrite", "networkAccess": True},
            },
            "custom",
            "unsupported-value",
        ),
        (
            {
                "approvalPolicy": "never",
                "sandboxPolicy": {"type": "readOnly", "readOnlyAccess": {"type": "future"}},
            },
            "custom",
            "unsupported-value",
        ),
        (
            {"approvalPolicy": "never", "sandboxPolicy": {"type": "future"}},
            "unknown",
            "unsupported-value",
        ),
        (
            {"approvalPolicy": "never", "activePermissionProfile": {}},
            "unknown",
            "unsupported-value",
        ),
        (
            {"approvalPolicy": True, "sandboxPolicy": {"type": "dangerFullAccess"}},
            "unknown",
            "unsupported-value",
        ),
        ({"approvalPolicy": {"granular": {"future": "yes"}}}, "unknown", "unsupported-value"),
        (
            {
                "approvalPolicy": "never",
                "approvalsReviewer": "future",
                "sandboxPolicy": {"type": "dangerFullAccess"},
            },
            "unknown",
            "unsupported-value",
        ),
        ({"approvalPolicy": None}, "unknown", "missing-fields"),
        (
            {"config": {"approval_policy": "never"}, "mode": "full-access"},
            "unknown",
            "no-settings",
        ),
        (None, "unknown", "no-settings"),
        ([], "unknown", "unsupported-value"),
    ],
)
def test_observation_classifies_one_frame(
    settings: object, mode: str, uncertainty: str | None
) -> None:
    observed = modes.codex_permission_observation_from_thread_settings(settings, origin="resume")
    assert (observed.mode, observed.uncertainty) == (mode, uncertainty)
    assert observed.received_at is None and observed.runner_id is None
    assert (
        modes.decode_codex_permission_observation(
            modes.encode_codex_permission_observation(observed)
        )
        == observed
    )


@pytest.mark.parametrize("sandbox", ["workspaceWrite", "workspace-write"])
def test_observation_aliases_and_known_workspace_scope(sandbox: str) -> None:
    observed = modes.codex_permission_observation_from_thread_settings(
        {
            "approval_policy": "on-request",
            "approvals_reviewer": "auto_review",
            "sandbox_policy": {
                "type": sandbox,
                "networkAccess": False,
                "writableRoots": [],
                "excludeSlashTmp": True,
            },
            "active_permission_profile": {"id": ":workspace", "extends": None},
            "received_at": 1,
            "runner_id": "producer-claim",
        },
        origin="settings-updated",
    )
    assert observed.mode == "approve-for-me"
    assert observed.received_at is None and observed.runner_id is None
    assert observed.profile == "workspace" and observed.sandbox == "workspace-write"


@pytest.mark.parametrize(
    ("camel", "snake"),
    [
        ("excludeSlashTmp", "exclude_slash_tmp"),
        ("excludeTmpdirEnvVar", "exclude_tmpdir_env_var"),
    ],
)
@pytest.mark.parametrize(("camel_value", "snake_value"), product([False, True], repeat=2))
@pytest.mark.parametrize("origin", ["resume", "settings-updated"])
def test_observation_workspace_boolean_alias_agreement(
    camel: str, snake: str, camel_value: bool, snake_value: bool, origin: str
) -> None:
    observed = modes.codex_permission_observation_from_thread_settings(
        {
            **_ASK_FOR_APPROVAL_SETTINGS,
            "sandboxPolicy": {"type": "workspaceWrite", camel: camel_value, snake: snake_value},
        },
        origin=origin,
    )
    expected = (
        ("ask-for-approval", None)
        if camel_value == snake_value
        else ("unknown", "conflicting-fields")
    )
    assert (observed.mode, observed.uncertainty) == expected
    assert modes.codex_permission_observation_from_payload(observed.to_payload()) == observed
    assert (
        modes.decode_codex_permission_observation(
            modes.encode_codex_permission_observation(observed)
        )
        == observed
    )


@pytest.mark.parametrize(
    "alias",
    ["excludeSlashTmp", "exclude_slash_tmp", "excludeTmpdirEnvVar", "exclude_tmpdir_env_var"],
)
@pytest.mark.parametrize("value", [False, True])
def test_observation_workspace_single_boolean_alias_remains_exact(alias: str, value: bool) -> None:
    observed = modes.codex_permission_observation_from_thread_settings(
        {**_ASK_FOR_APPROVAL_SETTINGS, "sandboxPolicy": {"type": "workspaceWrite", alias: value}},
        origin="settings-updated",
    )
    assert (observed.mode, observed.uncertainty) == ("ask-for-approval", None)


def test_observation_never_defaults_or_combines_frames() -> None:
    full = modes.codex_permission_observation_from_thread_settings(
        _FULL_ACCESS_SETTINGS, origin="resume"
    )
    partial = modes.codex_permission_observation_from_thread_settings(
        {"approvalPolicy": "never"}, origin="settings-updated"
    )
    empty = modes.codex_permission_observation_from_thread_settings({}, origin="resume")
    assert full.mode == "full-access"
    assert (partial.mode, partial.sandbox, empty.mode) == ("unknown", None, "unknown")
    with pytest.raises(ValueError, match="origin"):
        modes.codex_permission_observation_from_thread_settings({}, origin="web-switch")


# Literal event/storage fixtures for the later Python/TS consumer contract.
PERMISSION_OBSERVATION_CONTRACT_FIXTURES = [
    {
        "encoded": "c1:0,1,2,1,1,2,2,0,0,1792749199,cnVubmVy",
        "observation": {
            "version": 1,
            "source": "native-settings",
            "origin": "settings-updated",
            "mode": "ask-for-approval",
            "approval": "on-request",
            "reviewer": "user",
            "sandbox": "workspace-write",
            "profile": "workspace",
            "uncertainty": None,
            "requested_mode": None,
            "received_at": 1792749199,
            "runner_id": "runner",
        },
    },
    {
        "encoded": "c1:1,2,0,0,0,0,0,5,3,-,-",
        "observation": {
            "version": 1,
            "source": "application-receipt",
            "origin": "web-switch",
            "mode": "unknown",
            "approval": None,
            "reviewer": None,
            "sandbox": None,
            "profile": None,
            "uncertainty": "application-receipt",
            "requested_mode": "full-access",
            "received_at": None,
            "runner_id": None,
        },
    },
    {
        "encoded": "c1:0,0,0,0,0,0,0,4,0,-,-",
        "observation": {
            "version": 1,
            "source": "native-settings",
            "origin": "resume",
            "mode": "unknown",
            "approval": None,
            "reviewer": None,
            "sandbox": None,
            "profile": None,
            "uncertainty": "no-settings",
            "requested_mode": None,
            "received_at": None,
            "runner_id": None,
        },
    },
]


@pytest.mark.parametrize("case", PERMISSION_OBSERVATION_CONTRACT_FIXTURES)
def test_observation_fixed_storage_event_contract(case: dict[str, object]) -> None:
    encoded = cast(str, case["encoded"])
    decoded = modes.decode_codex_permission_observation(encoded)
    assert decoded.to_dict() == case["observation"]
    assert modes.encode_codex_permission_observation(decoded) == encoded


@pytest.mark.parametrize("requested", sorted(CODEX_NATIVE_PERMISSION_VALUES))
def test_observation_application_response_is_not_native_confirmation(requested: str) -> None:
    observed = modes.codex_permission_application_receipt(requested)
    assert observed.source == "application-receipt" and observed.mode == "unknown"
    assert observed.requested_mode == requested
    assert (observed.approval, observed.reviewer, observed.sandbox, observed.profile) == (
        None,
    ) * 4
    assert (
        modes.decode_codex_permission_observation(
            modes.encode_codex_permission_observation(observed)
        )
        == observed
    )
    with pytest.raises(ValueError, match="producer"):
        observed.to_payload()
    with pytest.raises(ValueError, match="producer"):
        modes.codex_permission_observation_from_payload(
            {
                key: val
                for key, val in observed.to_dict().items()
                if key not in {"received_at", "runner_id"}
            }
        )


def test_observation_producer_cannot_supply_metadata_or_control_fields() -> None:
    observed = modes.codex_permission_observation_from_thread_settings(
        _FULL_ACCESS_SETTINGS, origin="resume"
    )
    payload = observed.to_payload()
    assert modes.codex_permission_observation_from_payload(payload) == observed
    for key in ("received_at", "runner_id", "terminal_launch_args", "overrides"):
        with pytest.raises(ValueError, match="producer fields"):
            modes.codex_permission_observation_from_payload({**payload, key: None})
    for invalid_payload in (None, {}, {**payload, "unexpected": "full-access"}):
        with pytest.raises(ValueError, match="producer fields"):
            modes.codex_permission_observation_from_payload(invalid_payload)
    attached = modes.codex_permission_observation_with_metadata(
        observed, received_at=0, runner_id="receiver"
    )
    assert attached.to_payload() == observed.to_payload()
    assert (attached.received_at, attached.runner_id) == (0, "receiver")


@pytest.mark.parametrize(
    "changes",
    [
        {"version": True},
        {"version": 1.0},
        {"version": 2},
        {"source": "future"},
        {"origin": "web-switch"},
        {"mode": "full-access"},
        {"approval": "future"},
        {"reviewer": True},
        {"sandbox": "future"},
        {"profile": "future"},
        {"uncertainty": "future"},
        {"requested_mode": "full-access"},
        {"uncertainty": "application-receipt"},
        {"mode": "custom", "uncertainty": "unsupported-value"},
    ],
)
def test_observation_validates_finite_native_combinations(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        replace(modes.CodexPermissionObservation(), **changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"mode": "full-access"},
        {"origin": "resume"},
        {"requested_mode": None},
        {"requested_mode": "custom"},
        {"approval": "never"},
        {"reviewer": "user"},
        {"sandbox": "danger-full-access"},
        {"profile": "full-access"},
        {"uncertainty": None},
    ],
)
def test_observation_validates_receipt_combinations(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        replace(modes.codex_permission_application_receipt("full-access"), **changes)


@pytest.mark.parametrize("timestamp", [True, 1.0, -1, 1 << 63, "0"])
def test_observation_rejects_invalid_receiver_timestamp(timestamp: object) -> None:
    with pytest.raises(ValueError, match="received_at"):
        replace(modes.CodexPermissionObservation(), received_at=cast(int, timestamp))


@pytest.mark.parametrize("runner_id", ["r" * 128, "中" * 42 + "ab"])
def test_observation_maximum_storage_bound_roundtrips_real_codec(runner_id: str) -> None:
    observed = modes.codex_permission_observation_from_thread_settings(
        {
            "approvalPolicy": "granular",
            "approvalsReviewer": "guardian_subagent",
            "sandboxPolicy": {"type": "externalSandbox"},
            "activePermissionProfile": {"id": "test-custom"},
        },
        origin="settings-updated",
    )
    observed = modes.codex_permission_observation_with_metadata(
        observed, received_at=(1 << 63) - 1, runner_id=runner_id
    )
    encoded = modes.encode_codex_permission_observation(observed)
    assert len(runner_id.encode()) == 128
    assert len(encoded) == len(encoded.encode("ascii")) == 212
    assert len(encoded) <= modes.CODEX_PERMISSION_OBSERVATION_MAX_LEN < 256
    assert modes.decode_codex_permission_observation(encoded) == observed


@pytest.mark.parametrize(
    "runner_id",
    [
        "",
        "r" * 129,
        "中" * 43,
        "\ud800",
        1,
        *[chr(code) for code in (*range(32), *range(127, 160))],
    ],
)
def test_observation_invalid_binding_is_rejected_or_explicitly_unknown(runner_id: object) -> None:
    full = modes.codex_permission_observation_from_thread_settings(
        _FULL_ACCESS_SETTINGS, origin="resume"
    )
    with pytest.raises(ValueError, match="runner_id"):
        replace(full, runner_id=cast(str, runner_id))
    observed = modes.codex_permission_observation_with_metadata(
        full, received_at=1, runner_id=cast(str, runner_id)
    )
    assert (observed.mode, observed.uncertainty, observed.runner_id) == (
        "unknown",
        "unsupported-value",
        None,
    )
    assert observed.sandbox == "danger-full-access"
    assert (
        modes.decode_codex_permission_observation(
            modes.encode_codex_permission_observation(observed)
        )
        == observed
    )


@pytest.mark.parametrize(
    ("index", "code"), product(range(9), ["", "9", "+1", "01", "-1", "x", "１"])
)
def test_observation_codec_rejects_bad_code_in_every_position(index: int, code: str) -> None:
    parts = ["0", "1", "4", "2", "1", "3", "3", "0", "0", "-", "-"]
    parts[index] = code
    with pytest.raises(ValueError):
        modes.decode_codex_permission_observation("c1:" + ",".join(parts))


@pytest.mark.parametrize(
    "encoded",
    [
        "",
        "full-access",
        "{}",
        "c0:0,0,0,0,0,0,0,4,0,-,-",
        "c2:0,0,0,0,0,0,0,4,0,-,-",
        "c1:0,0,0,0,0,0,0,4,0,-",
        "c1:0,0,0,0,0,0,0,4,0,-,-,0",
        "c1:0,1,4,2,1,1,3,0,0,-,-",
        "c1:1,2,4,0,0,0,0,5,3,-,-",
        "c1:0,1,0,2,0,0,0,0,0,-,-",
        "c1:0,1,4,2,1,3,3,2,0,-,-",
        "c1:" + "x" * 210,
    ],
)
def test_observation_codec_never_falls_back_on_invalid_storage(encoded: str) -> None:
    with pytest.raises(ValueError):
        modes.decode_codex_permission_observation(encoded)


@pytest.mark.parametrize(
    "timestamp",
    [
        "01",
        "-0",
        "+1",
        "1.0",
        "true",
        "-1",
        "9223372036854775808",
        "10000000000000000000",
        "",
        "１",
    ],
)
def test_observation_codec_rejects_noncanonical_or_out_of_range_time(timestamp: str) -> None:
    with pytest.raises(ValueError):
        modes.decode_codex_permission_observation(f"c1:0,1,4,2,1,3,3,0,0,{timestamp},-")


@pytest.mark.parametrize(
    "binding", ["", "a", "eA=", "eA==", "eB", "__", "AA", "fw", "wog", "**", "a" * 172]
)
def test_observation_codec_rejects_bad_binding_encoding(binding: str) -> None:
    with pytest.raises(ValueError):
        modes.decode_codex_permission_observation(f"c1:0,1,4,2,1,3,3,0,0,-,{binding}")


@pytest.mark.parametrize("sandbox", [None, False, [], {"type": []}, {"type": {}}, {"type": False}])
def test_observation_malformed_sandbox_does_not_raise_or_name_full(sandbox: object) -> None:
    observed = modes.codex_permission_observation_from_thread_settings(
        {"approvalPolicy": "never", "sandboxPolicy": sandbox}, origin="resume"
    )
    assert observed.mode == "unknown"
    assert observed.sandbox is None


@pytest.mark.parametrize(
    "scope",
    [
        {"networkAccess": True},
        {"writableRoots": ["/opt/extra"]},
        {"readOnlyAccess": {}},
        {"excludeSlashTmp": "false"},
    ],
)
def test_observation_extra_access_cannot_claim_an_exact_workspace_preset(scope: dict) -> None:
    observed = modes.codex_permission_observation_from_thread_settings(
        {**_ASK_FOR_APPROVAL_SETTINGS, "sandboxPolicy": {"type": "workspaceWrite", **scope}},
        origin="settings-updated",
    )
    assert (observed.mode, observed.uncertainty) == ("custom", "unsupported-value")


def test_observation_read_only_profile_and_boundary_aliases_are_exact() -> None:
    observed = modes.codex_permission_observation_from_thread_settings(
        {
            "approval_policy": "on-request",
            "approvals_reviewer": "user",
            "active_permission_profile": {"id": ":read-only"},
        },
        origin="settings-updated",
    )
    assert observed.to_dict() == {
        "version": 1,
        "source": "native-settings",
        "origin": "settings-updated",
        "mode": "read-only",
        "approval": "on-request",
        "reviewer": "user",
        "sandbox": "read-only",
        "profile": "read-only",
        "uncertainty": None,
        "requested_mode": None,
        "received_at": None,
        "runner_id": None,
    }
    assert modes.encode_codex_permission_observation(observed) == "c1:0,1,5,1,1,1,1,0,0,-,-"


@pytest.mark.parametrize("invalid", [None, False, 1, b"c1:0,0,0,0,0,0,0,4,0,-,-"])
def test_observation_codec_rejects_non_string_input(invalid: object) -> None:
    with pytest.raises(ValueError):
        modes.decode_codex_permission_observation(cast(str, invalid))
    with pytest.raises(ValueError):
        modes.encode_codex_permission_observation(cast(modes.CodexPermissionObservation, invalid))


@pytest.mark.parametrize("timestamp", [None, 0, 1792749199, (1 << 63) - 1])
def test_observation_nullable_binding_and_time_roundtrip(timestamp: int | None) -> None:
    observed = modes.codex_permission_observation_with_metadata(
        modes.CodexPermissionObservation(), received_at=timestamp, runner_id=None
    )
    assert (
        modes.decode_codex_permission_observation(
            modes.encode_codex_permission_observation(observed)
        )
        == observed
    )


def test_observation_minimum_binding_encoding_and_invalid_receipt_context() -> None:
    minimum = modes.codex_permission_observation_with_metadata(
        modes.CodexPermissionObservation(), received_at=None, runner_id="x"
    )
    assert modes.encode_codex_permission_observation(minimum) == "c1:0,0,0,0,0,0,0,4,0,-,eA"
    assert modes.decode_codex_permission_observation("c1:0,0,0,0,0,0,0,4,0,-,eA") == minimum
    receipt = modes.codex_permission_observation_with_metadata(
        modes.codex_permission_application_receipt("full-access"),
        received_at=0,
        runner_id="invalid\nidentity",
    )
    assert receipt.mode == "unknown" and receipt.runner_id is None
    assert receipt.uncertainty == "unsupported-value" and receipt.requested_mode == "full-access"
    assert modes.decode_codex_permission_observation("c1:1,2,0,0,0,0,0,2,3,0,-") == receipt
