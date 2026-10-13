"""Tests for native Antigravity bridge state helpers."""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import omnigent.harnesses.antigravity_native.bridge as _mod
from omnigent.harnesses.antigravity_native.bridge import (
    ANTIGRAVITY_NATIVE_BRIDGE_DIR_ENV_VAR,
    ANTIGRAVITY_NATIVE_REQUEST_SESSION_ID_ENV_VAR,
    AntigravityNativeBridgeState,
    agy_gemini_dir,
    agy_home_dir,
    build_antigravity_native_spawn_env,
    build_mcp_config,
    clear_bridge_state,
    ensure_agy_feedback_survey_disabled,
    ensure_agy_onboarding_complete,
    inject_user_message_via_tui,
    prepare_bridge_dir,
    read_bridge_state,
    read_tmux_info,
    seed_isolated_agy_home,
    send_interaction_keys_via_tui,
    update_conversation_id,
    write_bridge_state,
    write_mcp_bridge_config,
    write_mcp_config,
    write_tmux_target,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_active_turn(bridge_dir: Path, active_turn_id: str | None) -> None:
    """
    Write bridge state with a given active turn id.

    :param bridge_dir: Native Antigravity bridge directory.
    :param active_turn_id: Active turn id to seed, e.g. ``"turn_1"``,
        or ``None`` for no running turn.
    :returns: None.
    """
    write_bridge_state(
        bridge_dir,
        AntigravityNativeBridgeState(
            session_id="conv_test",
            conversation_id="conv_agy_test",
            active_turn_id=active_turn_id,
        ),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def bridge_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Create an isolated bridge directory rooted under ``tmp_path``.

    :param tmp_path: pytest temp directory.
    :param monkeypatch: pytest monkeypatch fixture.
    :returns: Prepared bridge directory.
    """
    monkeypatch.setattr(_mod, "_BRIDGE_ROOT", tmp_path / "antigravity-native")
    return prepare_bridge_dir("bridge_test")


# ---------------------------------------------------------------------------
# State roundtrip
# ---------------------------------------------------------------------------


def test_state_roundtrip_full(bridge_dir: Path) -> None:
    """
    Written state reads back equal, including active_turn_id when set.

    Guards the full field set: a dropped or renamed field would cause the
    harness or forwarder to read stale/None values and lose track of the
    conversation.
    """
    state = AntigravityNativeBridgeState(
        session_id="conv_abc123",
        conversation_id="agy_conv_xyz",
        active_turn_id="turn_abc123",
    )
    write_bridge_state(bridge_dir, state)
    read_back = read_bridge_state(bridge_dir)
    assert read_back == state


def test_state_roundtrip_active_turn_none(bridge_dir: Path) -> None:
    """
    Written state with active_turn_id=None reads back with active_turn_id=None.

    Guards the idle state: the default field value must survive the JSON
    roundtrip so the forwarder does not wrongly report a running turn.
    """
    state = AntigravityNativeBridgeState(
        session_id="conv_abc123",
        conversation_id="agy_conv_xyz",
        active_turn_id=None,
    )
    write_bridge_state(bridge_dir, state)
    read_back = read_bridge_state(bridge_dir)
    assert read_back == state
    assert read_back is not None
    assert read_back.active_turn_id is None


def test_state_roundtrip_conversation_id(bridge_dir: Path) -> None:
    """
    The conversation_id field roundtrips verbatim.

    The forwarder discovers agy's real UUID and writes it here; a dropped or
    mangled value would break resume (which reads this id back to pass to
    ``agy --conversation``).
    """
    state = AntigravityNativeBridgeState(
        session_id="conv_abc",
        conversation_id="68caaeac-2eaf-4e2c-9b95-721b022f4903",
    )
    write_bridge_state(bridge_dir, state)
    read_back = read_bridge_state(bridge_dir)
    assert read_back is not None
    assert read_back.conversation_id == "68caaeac-2eaf-4e2c-9b95-721b022f4903"


# ---------------------------------------------------------------------------
# legacy agy_pid key (removed field)
# ---------------------------------------------------------------------------


def test_read_bridge_state_ignores_legacy_agy_pid_key(bridge_dir: Path) -> None:
    """
    A legacy state.json carrying the removed ``agy_pid`` key reads back fine.

    The ``agy_pid`` field was removed (agy is launched under
    ``tmux_start_on_attach`` so no pid exists at launch; the executor always
    discovers the connect-RPC port at injection time). A state file written by
    an older build must not crash the reader; the obsolete key is ignored and
    the remaining fields parse normally.

    :param bridge_dir: Isolated bridge directory fixture.
    :returns: None.
    """
    (bridge_dir / "state.json").write_text(
        json.dumps(
            {
                "session_id": "conv_abc",
                "conversation_id": "agy_conv",
                "active_turn_id": "turn_1",
                "agy_pid": 72753,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    state = read_bridge_state(bridge_dir)
    assert state is not None
    assert state.session_id == "conv_abc"
    assert state.conversation_id == "agy_conv"
    assert state.active_turn_id == "turn_1"
    assert not hasattr(state, "agy_pid")


def test_write_bridge_state_omits_agy_pid_key(bridge_dir: Path) -> None:
    """
    Written state.json never contains an ``agy_pid`` key.

    The field was removed, so a fresh write must not re-introduce it (a stray
    key would mislead any tooling that still inspects the file).

    :param bridge_dir: Isolated bridge directory fixture.
    :returns: None.
    """
    write_bridge_state(
        bridge_dir,
        AntigravityNativeBridgeState(session_id="conv_abc", conversation_id="agy_conv"),
    )
    raw = json.loads((bridge_dir / "state.json").read_text(encoding="utf-8"))
    assert "agy_pid" not in raw


# ---------------------------------------------------------------------------
# read_bridge_state — None branches
# ---------------------------------------------------------------------------


def test_read_bridge_state_missing_dir_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Reading from a non-existent bridge directory returns None.

    Guards startup safety: if the harness starts before the wrapper has
    written state, the caller must gracefully wait rather than crash.
    """
    monkeypatch.setattr(_mod, "_BRIDGE_ROOT", tmp_path / "antigravity-native")
    missing = prepare_bridge_dir("never_written")
    # Don't write any state — the directory exists but state.json does not.
    assert read_bridge_state(missing) is None


def test_read_bridge_state_corrupt_json_returns_none(bridge_dir: Path) -> None:
    """
    Corrupt JSON in state.json returns None, not an exception.

    Guards partial-write resilience: if the file is truncated mid-write
    the reader must degrade gracefully rather than crash the harness.
    """
    (bridge_dir / "state.json").write_text("{not valid json", encoding="utf-8")
    assert read_bridge_state(bridge_dir) is None


def test_read_bridge_state_missing_required_fields_returns_none(bridge_dir: Path) -> None:
    """
    JSON missing required fields returns None.

    Guards schema enforcement: an incomplete state (e.g. old format) must
    not be returned as a partially-valid object that causes attribute errors.
    """
    (bridge_dir / "state.json").write_text('{"session_id": "conv_abc"}\n', encoding="utf-8")
    assert read_bridge_state(bridge_dir) is None


def test_read_bridge_state_missing_conversation_id_returns_none(bridge_dir: Path) -> None:
    """
    JSON missing conversation_id returns None.

    conversation_id is required (the forwarder/resume both depend on it); a
    state lacking it must be rejected rather than returned with an empty id.
    """
    (bridge_dir / "state.json").write_text(
        json.dumps({"session_id": "conv_abc", "active_turn_id": None}) + "\n",
        encoding="utf-8",
    )
    assert read_bridge_state(bridge_dir) is None


def test_read_bridge_state_empty_conversation_id_returns_none(bridge_dir: Path) -> None:
    """
    An empty-string conversation_id returns None.

    Guards the non-empty contract: an empty id never names a real agy brain
    dir, so it must be rejected rather than handed to resume.
    """
    (bridge_dir / "state.json").write_text(
        json.dumps(
            {
                "session_id": "conv_abc",
                "conversation_id": "",
                "active_turn_id": None,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert read_bridge_state(bridge_dir) is None


def test_read_bridge_state_ignores_legacy_sidecar_fields(bridge_dir: Path) -> None:
    """
    Legacy state.json carrying removed fields still reads back via the current
    (reduced) schema.

    Guards forward-compat across the Task 12 cutover: a state file written by a
    prior build must not crash the reader. This covers the removed sidecar_port /
    data_dir fields AND the durable read cursor (forwarded_step_index /
    forwarded_steps) the transcript forwarder persisted before the RPC reader
    superseded it — all obsolete keys are simply ignored.
    """
    (bridge_dir / "state.json").write_text(
        json.dumps(
            {
                "session_id": "conv_abc",
                "sidecar_port": 9000,
                "conversation_id": "agy_conv",
                "data_dir": "/tmp/data",
                "active_turn_id": None,
                # Retired durable cursor keys (Task 12 cutover): a forwarder-era
                # state.json carries these; the reader must tolerate + drop them.
                "forwarded_step_index": 14,
                "forwarded_steps": [0, 2, 14],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    state = read_bridge_state(bridge_dir)
    assert state is not None
    assert state.session_id == "conv_abc"
    assert state.conversation_id == "agy_conv"
    assert state.active_turn_id is None
    # The retired cursor fields are gone from the dataclass entirely.
    assert not hasattr(state, "forwarded_step_index")
    assert not hasattr(state, "forwarded_steps")


# ---------------------------------------------------------------------------
# build_antigravity_native_spawn_env
# ---------------------------------------------------------------------------


def test_build_spawn_env_sets_both_env_vars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    build_antigravity_native_spawn_env sets both required env vars.

    The harness executor depends on both env vars being present to locate the
    bridge directory and correlate the request session. A missing var would
    leave the executor unable to read state or match the session.
    """
    monkeypatch.setattr(_mod, "_BRIDGE_ROOT", tmp_path / "antigravity-native")
    env = build_antigravity_native_spawn_env("conv_abc123")
    assert ANTIGRAVITY_NATIVE_BRIDGE_DIR_ENV_VAR in env
    assert ANTIGRAVITY_NATIVE_REQUEST_SESSION_ID_ENV_VAR in env
    assert env[ANTIGRAVITY_NATIVE_REQUEST_SESSION_ID_ENV_VAR] == "conv_abc123"


def test_build_spawn_env_bridge_id_defaults_to_conversation_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    When bridge_id is None, the conversation_id is used as the bridge_id.

    Guards the default behaviour: the bridge dir must be deterministic even
    when no explicit bridge_id label is attached to the conversation.
    """
    monkeypatch.setattr(_mod, "_BRIDGE_ROOT", tmp_path / "antigravity-native")
    env_default = build_antigravity_native_spawn_env("conv_abc123")
    env_explicit = build_antigravity_native_spawn_env("conv_abc123", bridge_id="conv_abc123")
    assert (
        env_default[ANTIGRAVITY_NATIVE_BRIDGE_DIR_ENV_VAR]
        == env_explicit[ANTIGRAVITY_NATIVE_BRIDGE_DIR_ENV_VAR]
    )


def test_build_spawn_env_explicit_bridge_id_differs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    An explicit bridge_id produces a different bridge dir than the conversation_id.

    Guards bridge isolation: two conversations sharing a bridge_id (label-based
    routing) must resolve to the same directory, while distinct ids must not
    collide.
    """
    monkeypatch.setattr(_mod, "_BRIDGE_ROOT", tmp_path / "antigravity-native")
    env_conv = build_antigravity_native_spawn_env("conv_abc123")
    env_bridge = build_antigravity_native_spawn_env("conv_abc123", bridge_id="bridge_xyz")
    assert (
        env_conv[ANTIGRAVITY_NATIVE_BRIDGE_DIR_ENV_VAR]
        != env_bridge[ANTIGRAVITY_NATIVE_BRIDGE_DIR_ENV_VAR]
    )


# ---------------------------------------------------------------------------
# prepare_bridge_dir permissions
# ---------------------------------------------------------------------------


def test_prepare_bridge_dir_creates_0o700_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    prepare_bridge_dir creates the directory with mode 0o700.

    Guards filesystem privacy: bridge dirs contain auth tokens and session
    ids. A wider mode (e.g. 0o755) would expose those to other local users.
    """
    import stat

    monkeypatch.setattr(_mod, "_BRIDGE_ROOT", tmp_path / "antigravity-native")
    bd = prepare_bridge_dir("bridge_perms_test")
    assert bd.is_dir()
    mode = stat.S_IMODE(bd.stat().st_mode)
    assert mode == 0o700


# ---------------------------------------------------------------------------
# update_conversation_id
# ---------------------------------------------------------------------------


def test_update_conversation_id_mutates_correctly(bridge_dir: Path) -> None:
    """
    update_conversation_id updates the conversation_id and active_turn_id.

    Guards conversation rotation: when agy creates a fresh conversation while
    the Omnigent session stays the same, the bridge must reflect the new id.
    """
    _seed_active_turn(bridge_dir, "turn_old")
    assert update_conversation_id(bridge_dir, "agy_conv_new", active_turn_id="turn_fresh") is True
    state = read_bridge_state(bridge_dir)
    assert state is not None
    assert state.conversation_id == "agy_conv_new"
    assert state.active_turn_id == "turn_fresh"


def test_update_conversation_id_clears_active_turn_by_default(bridge_dir: Path) -> None:
    """
    update_conversation_id with no active_turn_id clears the active turn.

    Guards the default: a new conversation starts with no active turn unless
    the caller explicitly supplies one.
    """
    _seed_active_turn(bridge_dir, "turn_old")
    assert update_conversation_id(bridge_dir, "agy_conv_new") is True
    state = read_bridge_state(bridge_dir)
    assert state is not None
    assert state.conversation_id == "agy_conv_new"
    assert state.active_turn_id is None


def test_update_conversation_id_returns_false_and_warns_when_no_state(
    bridge_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    update_conversation_id returns False and WARNs when there is no state.

    Guards observability (Fix C): on a missing/invalid state file the real
    cascade id must NOT be silently dropped — the function returns ``False`` and
    logs a WARNING naming the dropped id so the cold-start caller can report that
    the reader will stay bound to the ``agy_conv_*`` placeholder.
    """
    with caplog.at_level(logging.WARNING, logger="omnigent.harnesses.antigravity_native.bridge"):
        result = update_conversation_id(bridge_dir, "agy_conv_new")  # empty bridge dir
    assert result is False
    assert read_bridge_state(bridge_dir) is None
    # The WARNING names the dropped conversation id.
    assert any(
        record.levelno == logging.WARNING and "agy_conv_new" in record.getMessage()
        for record in caplog.records
    )


# ---------------------------------------------------------------------------
# clear_bridge_state
# ---------------------------------------------------------------------------


def test_clear_bridge_state_removes_state(bridge_dir: Path) -> None:
    """
    clear_bridge_state removes state.json so read_bridge_state returns None.

    Guards stale-state cleanup: before a new agy launch the old state must
    be removed so the harness waits for the new state instead of reading
    stale sidecar coordinates.
    """
    _seed_active_turn(bridge_dir, None)
    assert read_bridge_state(bridge_dir) is not None
    clear_bridge_state(bridge_dir)
    assert read_bridge_state(bridge_dir) is None


def test_clear_bridge_state_noop_when_absent(bridge_dir: Path) -> None:
    """
    clear_bridge_state is a no-op when state.json does not exist.

    Guards idempotency: calling clear before the first write must not raise.
    """
    clear_bridge_state(bridge_dir)  # must not raise


# ---------------------------------------------------------------------------
# Onboarding marker (ensure_agy_onboarding_complete)
# ---------------------------------------------------------------------------

_COMPLETE_STATE = {
    "consumerOnboardingComplete": True,
    "enterpriseOnboardingComplete": False,
    "onboardingComplete": True,
}


@pytest.fixture
def agy_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Redirect the agy onboarding marker into an isolated temp HOME.

    :param tmp_path: pytest temp directory.
    :param monkeypatch: pytest monkeypatch fixture.
    :returns: The (not-yet-created) marker path under ``tmp_path``.
    """
    marker = tmp_path / ".gemini" / "antigravity-cli" / "cache" / "onboarding.json"
    monkeypatch.setattr(_mod, "_AGY_ONBOARDING_MARKER", marker)
    return marker


def test_ensure_onboarding_writes_marker_and_parents_when_absent(agy_marker: Path) -> None:
    """
    A fresh host (no marker, no cache dir) gets the complete-state file written.

    Guards the core wizard-suppression: agy reads ``onboardingComplete: true`` and
    skips the first-run TUI, and the nested ``cache/`` parents are created.
    """
    assert not agy_marker.exists()
    ensure_agy_onboarding_complete()
    assert agy_marker.is_file()
    assert json.loads(agy_marker.read_text(encoding="utf-8")) == _COMPLETE_STATE


def test_ensure_onboarding_is_idempotent_no_rewrite(agy_marker: Path) -> None:
    """
    When all three keys are already complete the file is left byte-for-byte intact.

    Pre-write a non-canonical encoding (custom key order, no trailing newline); a
    returning user's agy state must not be churned, so an early return — not a
    re-serialize — is required.
    """
    agy_marker.parent.mkdir(parents=True, exist_ok=True)
    original = '{"onboardingComplete": true, "consumerOnboardingComplete": true, "enterpriseOnboardingComplete": false}'  # noqa: E501
    agy_marker.write_text(original, encoding="utf-8")
    ensure_agy_onboarding_complete()
    assert agy_marker.read_text(encoding="utf-8") == original


def test_ensure_onboarding_preserves_unknown_keys_and_completes(agy_marker: Path) -> None:
    """
    A partial/older marker is upgraded: unknown keys survive, the gate flips True.
    """
    agy_marker.parent.mkdir(parents=True, exist_ok=True)
    agy_marker.write_text(json.dumps({"custom": 7, "onboardingComplete": False}), encoding="utf-8")
    ensure_agy_onboarding_complete()
    result = json.loads(agy_marker.read_text(encoding="utf-8"))
    assert result["custom"] == 7
    assert result["onboardingComplete"] is True
    assert result["consumerOnboardingComplete"] is True
    assert result["enterpriseOnboardingComplete"] is False


def test_ensure_onboarding_overwrites_corrupt_file(agy_marker: Path) -> None:
    """
    A non-JSON marker (regenerable, non-secret cache) is overwritten, not raised on.
    """
    agy_marker.parent.mkdir(parents=True, exist_ok=True)
    agy_marker.write_text("not json {{{", encoding="utf-8")
    ensure_agy_onboarding_complete()
    assert json.loads(agy_marker.read_text(encoding="utf-8")) == _COMPLETE_STATE


def test_ensure_onboarding_overwrites_non_object_json(agy_marker: Path) -> None:
    """
    Valid-but-non-object JSON (e.g. a list) is treated as absent and replaced.
    """
    agy_marker.parent.mkdir(parents=True, exist_ok=True)
    agy_marker.write_text("[1, 2, 3]", encoding="utf-8")
    ensure_agy_onboarding_complete()
    assert json.loads(agy_marker.read_text(encoding="utf-8")) == _COMPLETE_STATE


def test_ensure_onboarding_normalises_numeric_truthy_values(agy_marker: Path) -> None:
    """
    A marker storing numeric 1/0 (which ``==`` True/False in Python) is rewritten
    to real JSON booleans — the early-return uses identity, not equality, so a
    numeric file is not mistaken for the boolean complete-state.
    """
    agy_marker.parent.mkdir(parents=True, exist_ok=True)
    agy_marker.write_text(
        json.dumps(
            {
                "consumerOnboardingComplete": 1,
                "enterpriseOnboardingComplete": 0,
                "onboardingComplete": 1,
            }
        ),
        encoding="utf-8",
    )
    ensure_agy_onboarding_complete()
    result = json.loads(agy_marker.read_text(encoding="utf-8"))
    assert result["onboardingComplete"] is True
    assert result["consumerOnboardingComplete"] is True
    assert result["enterpriseOnboardingComplete"] is False


def test_ensure_onboarding_raises_when_marker_dir_unwritable(agy_marker: Path) -> None:
    """
    An un-creatable marker directory surfaces OSError (fail-loud), not a silent
    skip — a missing marker would otherwise hang agy on the wizard.
    """
    # Put a regular FILE where the ``cache`` directory must be so mkdir() fails.
    agy_marker.parent.parent.mkdir(parents=True, exist_ok=True)
    agy_marker.parent.write_text("not a dir", encoding="utf-8")
    with pytest.raises(OSError):
        ensure_agy_onboarding_complete()


def test_ensure_onboarding_overwrites_non_utf8_file(agy_marker: Path) -> None:
    """
    A non-UTF-8 marker is treated as corrupt and regenerated, not raised on —
    UnicodeDecodeError is a ValueError, caught alongside JSONDecodeError.
    """
    agy_marker.parent.mkdir(parents=True, exist_ok=True)
    agy_marker.write_bytes(b"\xff\xfe\x00 not utf-8 \x80\x81")
    ensure_agy_onboarding_complete()
    assert json.loads(agy_marker.read_text(encoding="utf-8")) == _COMPLETE_STATE


# ---------------------------------------------------------------------------
# tmux target advertisement (write_tmux_target / read_tmux_info)
# ---------------------------------------------------------------------------


def test_write_read_tmux_target_roundtrip(tmp_path: Path) -> None:
    """A written tmux target reads back with its socket path and pane target."""
    bridge_dir = tmp_path / "bridge"
    write_tmux_target(
        bridge_dir,
        socket_path=Path("/tmp/omnigent-x/tmux.sock"),
        tmux_target="main",
    )
    info = read_tmux_info(bridge_dir)
    assert info == {"socket_path": "/tmp/omnigent-x/tmux.sock", "tmux_target": "main"}


def test_read_tmux_info_missing_returns_none(tmp_path: Path) -> None:
    """A bridge dir with no ``tmux.json`` yields ``None`` (not an error)."""
    assert read_tmux_info(tmp_path / "bridge") is None


def test_read_tmux_info_rejects_malformed_json(tmp_path: Path) -> None:
    """A corrupt ``tmux.json`` is treated as absent rather than raising."""
    bridge_dir = prepare_bridge_dir("bridge_malformed")
    (bridge_dir / "tmux.json").write_text("{not json", encoding="utf-8")
    assert read_tmux_info(bridge_dir) is None


def test_read_tmux_info_rejects_missing_fields(tmp_path: Path) -> None:
    """A ``tmux.json`` lacking a non-empty target/socket is rejected."""
    bridge_dir = prepare_bridge_dir("bridge_partial")
    (bridge_dir / "tmux.json").write_text(json.dumps({"socket_path": "/s"}), encoding="utf-8")
    assert read_tmux_info(bridge_dir) is None
    (bridge_dir / "tmux.json").write_text(
        json.dumps({"socket_path": "", "tmux_target": "main"}), encoding="utf-8"
    )
    assert read_tmux_info(bridge_dir) is None


def test_clear_bridge_state_removes_tmux_json(tmp_path: Path) -> None:
    """
    Clearing runtime state also drops the advertised tmux pane.

    A relaunch opens a new pane (and, after a host restart, a new socket), so a
    surviving ``tmux.json`` would let the executor bootstrap the first turn
    against the prior run's pane.
    """
    bridge_dir = prepare_bridge_dir("bridge_clear")
    write_bridge_state(
        bridge_dir,
        AntigravityNativeBridgeState(session_id="conv_1", conversation_id="cid-1"),
    )
    write_tmux_target(bridge_dir, socket_path=Path("/tmp/a/tmux.sock"), tmux_target="main")
    assert read_tmux_info(bridge_dir) is not None
    clear_bridge_state(bridge_dir)
    assert read_bridge_state(bridge_dir) is None
    assert read_tmux_info(bridge_dir) is None


# ---------------------------------------------------------------------------
# Paste payload encoding + submit needle
# ---------------------------------------------------------------------------


def test_paste_payload_bytes_maps_newlines_to_cr_and_strips_controls() -> None:
    """
    Newlines become CR (so the TUI keeps multi-line input as data under
    bracketed paste), tabs are kept, and other control bytes are dropped (a
    stray ESC would close the bracketed paste early).
    """
    payload = _mod._paste_payload_bytes("a\nb\r\nc\td\x1b\x00e")
    # \n and \r\n both collapse to a single CR; \t kept; ESC + NUL dropped.
    assert payload == b"a\rb\rc\tde"


def test_submit_needle_uses_first_substantial_line() -> None:
    """The needle is the first line with >=4 non-space chars, capped at 24."""
    needle = _mod._submit_needle("  \nHello there, this is a long first line")
    assert needle == "Hello there, this is a l"
    assert _mod._submit_needle("hi") == ""


def test_submit_needle_uses_last_substantial_line_for_collapsed_draft() -> None:
    content = "<omnigent_agent_instructions>\nbe terse\nReply with exactly PONG_FINAL\nhi\n"
    assert _mod._submit_needle(content) == "<omnigent_agent_instruct"
    assert _mod._submit_needle(content, from_tail=True) == "Reply with exactly PONG_"
    assert _mod._submit_needle("hi", from_tail=True) == ""


# ---------------------------------------------------------------------------
# First-turn TUI delivery (inject_user_message_via_tui)
# ---------------------------------------------------------------------------


@pytest.fixture
def _fast_tmux_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the TUI delivery polls/timeouts so subprocess tests run fast."""
    monkeypatch.setattr(_mod, "_TMUX_POLL_INTERVAL_S", 0.001)
    monkeypatch.setattr(_mod, "_PASTE_SETTLE_S", 0.0)
    monkeypatch.setattr(_mod, "_IDLE_CLEAR_TIMEOUT_S", 0.1)
    monkeypatch.setattr(_mod, "_BASELINE_CAPTURE_TIMEOUT_S", 0.1)
    monkeypatch.setattr(_mod, "_PASTE_COMMIT_TIMEOUT_S", 0.3)
    monkeypatch.setattr(_mod, "_SUBMIT_VERIFY_TIMEOUT_S", 0.1)
    monkeypatch.setattr(_mod, "_VERIFY_PROBE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(_mod, "_VERIFY_RETRY_INTERVAL_S", 0.0)
    monkeypatch.setattr(_mod, "_VERIFY_RETRY_TIMEOUT_S", 5.0)


def test_inject_user_message_via_tui_happy_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _fast_tmux_timeouts: None,
) -> None:
    """
    The delivery clears, pastes via buffer, and submits with a verified Enter.

    The fake models agy's TUI: idle (``? for shortcuts``) before the paste, the
    draft visible after the paste, and a running turn (``esc to cancel``) once
    Enter submits — exercising the readiness, paste-commit, and submit-verify
    gates that a static pane would stall.
    """
    bridge_dir = tmp_path / "bridge"
    write_tmux_target(bridge_dir, socket_path=Path("/tmp/ex/tmux.sock"), tmux_target="main")
    content = "Reply with exactly WORKING"
    captured: list[list[str]] = []
    loaded: list[bytes] = []
    tui = {"pane": "> \n? for shortcuts"}

    def _fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        """Record delivery calls; drive the simulated agy input box."""
        del kwargs
        if "has-session" in cmd:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "capture-pane" in cmd:
            return SimpleNamespace(returncode=0, stdout=tui["pane"], stderr="")
        if "load-buffer" in cmd:
            loaded.append(Path(cmd[-1]).read_bytes())
        if "paste-buffer" in cmd:
            tui["pane"] = f"> {content}\n? for shortcuts"
        if cmd[-1] == "Enter":
            tui["pane"] = "> \nesc to cancel"
        captured.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", _fake_run)
    inject_user_message_via_tui(bridge_dir, content=content, timeout_s=0.5)

    # C-a, C-k (clear), load-buffer, paste-buffer, Enter.
    assert len(captured) == 5, f"expected 5 delivery calls, got {captured}"
    clear_home, clear_kill, load, paste, submit = captured
    assert clear_home[-1] == "C-a"
    assert clear_kill[-1] == "C-k"
    assert loaded == [b"Reply with exactly WORKING\r"]
    assert load == [
        "tmux",
        "-S",
        "/tmp/ex/tmux.sock",
        "load-buffer",
        "-b",
        "omnigent-agy-paste",
        load[-1],  # the temp paste file path (unlinked after the paste)
    ]
    assert paste == [
        "tmux",
        "-S",
        "/tmp/ex/tmux.sock",
        "paste-buffer",
        "-p",
        "-d",
        "-b",
        "omnigent-agy-paste",
        "-t",
        "main",
    ]
    assert submit == ["tmux", "-S", "/tmp/ex/tmux.sock", "send-keys", "-t", "main", "Enter"]


def test_inject_user_message_via_tui_resends_enter_when_coalesced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _fast_tmux_timeouts: None,
) -> None:
    """
    A first Enter folded into the paste burst is re-sent until the turn starts.

    The fake leaves the pane idle after the first Enter (the submit was
    swallowed) and starts the turn only on the second — the delivery must not
    give up after one Enter.
    """
    bridge_dir = tmp_path / "bridge"
    write_tmux_target(bridge_dir, socket_path=Path("/tmp/ex/tmux.sock"), tmux_target="main")
    enters = {"n": 0}
    tui = {"pane": "> \n? for shortcuts"}

    def _fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        """Activate the turn only on the second Enter."""
        del kwargs
        if "has-session" in cmd:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "capture-pane" in cmd:
            return SimpleNamespace(returncode=0, stdout=tui["pane"], stderr="")
        if "paste-buffer" in cmd:
            tui["pane"] = "> hi there\n? for shortcuts"
        if cmd[-1] == "Enter":
            enters["n"] += 1
            if enters["n"] >= 2:
                tui["pane"] = "> \nesc to cancel"
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", _fake_run)
    inject_user_message_via_tui(bridge_dir, content="hi there", timeout_s=0.5)
    assert enters["n"] == 2, "expected the submit Enter to be re-sent once"


def test_inject_user_message_via_tui_accepts_changed_active_footer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _fast_tmux_timeouts: None,
) -> None:
    """
    The submit is confirmed when the draft leaves the composer, even if the
    running footer text differs from the known marker.

    A future agy build could rename the running footer or a narrow pane could
    truncate it; delivery must key off draft disappearance instead.
    """
    bridge_dir = tmp_path / "bridge"
    write_tmux_target(bridge_dir, socket_path=Path("/tmp/ex/tmux.sock"), tmux_target="main")
    content = "hello there"
    tui = {"pane": "> \n? for shortcuts"}

    def _fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        """Idle until Enter; then a non-idle footer that is NOT the known marker."""
        del kwargs
        if "has-session" in cmd:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "capture-pane" in cmd:
            return SimpleNamespace(returncode=0, stdout=tui["pane"], stderr="")
        if "paste-buffer" in cmd:
            tui["pane"] = f"> {content}\n? for shortcuts"
        if cmd[-1] == "Enter":
            tui["pane"] = "> \n(generating response, press the cancel key to stop)"
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", _fake_run)
    inject_user_message_via_tui(bridge_dir, content=content, timeout_s=0.5)


def test_inject_user_message_via_tui_mid_turn_sends_one_enter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _fast_tmux_timeouts: None,
) -> None:
    """
    A steer delivered while agy is mid-turn sends exactly one Enter, no re-send.

    The footer is already ``esc to cancel`` (a turn is running), so the
    idle->running verification is unavailable and a second Enter could queue a
    spurious empty turn. The delivery must still pass readiness (active footer
    accepted, not waiting for an idle footer that will not appear), paste, and
    submit a single best-effort Enter without spinning the retry budget or
    raising.
    """
    bridge_dir = tmp_path / "bridge"
    write_tmux_target(bridge_dir, socket_path=Path("/tmp/ex/tmux.sock"), tmux_target="main")
    enters = {"n": 0}
    tui = {"pane": "> \nesc to cancel"}

    def _fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        """agy stays mid-turn, but the draft clears after Enter."""
        del kwargs
        if "has-session" in cmd:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "capture-pane" in cmd:
            return SimpleNamespace(returncode=0, stdout=tui["pane"], stderr="")
        if "paste-buffer" in cmd:
            tui["pane"] = "> steer me\nesc to cancel"
        if cmd[-1] == "Enter":
            enters["n"] += 1
            tui["pane"] = "> \nesc to cancel"
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", _fake_run)
    inject_user_message_via_tui(bridge_dir, content="steer me", timeout_s=0.5)
    assert enters["n"] == 1, "mid-turn submit must send exactly one Enter (no re-send)"


def test_inject_user_message_via_tui_ignores_transcript_echo_after_submit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _fast_tmux_timeouts: None,
) -> None:
    """
    The submitted prompt remaining in transcript history is not a stuck draft.

    Claude/Kiro-style verification must scope matching to agy's live composer.
    After Enter, agy echoes the prompt above a fresh empty composer; matching the
    whole pane would falsely retry Enter and could submit an empty follow-up.
    """
    bridge_dir = tmp_path / "bridge"
    write_tmux_target(bridge_dir, socket_path=Path("/tmp/ex/tmux.sock"), tmux_target="main")
    content = "Reply exactly: transcript echo ok"
    enters = {"n": 0}
    separator = "────────────────────────────────────────────────────────────────────────────────"
    tui = {"pane": f"{separator}\n>\n{separator}\n? for shortcuts"}

    def _fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        """After Enter, keep the prompt only in transcript history."""
        del kwargs
        if "has-session" in cmd:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "capture-pane" in cmd:
            return SimpleNamespace(returncode=0, stdout=tui["pane"], stderr="")
        if "paste-buffer" in cmd:
            tui["pane"] = f"{separator}\n> {content}\n{separator}\n? for shortcuts"
        if cmd[-1] == "Enter":
            enters["n"] += 1
            tui["pane"] = (
                f"{separator}\n> {content}\n⣾  Generating...\n{separator}\n"
                f">\n{separator}\nesc to cancel"
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", _fake_run)
    inject_user_message_via_tui(bridge_dir, content=content, timeout_s=0.5)
    assert enters["n"] == 1


def test_inject_user_message_via_tui_raises_when_target_never_advertised(
    tmp_path: Path,
) -> None:
    """No ``tmux.json`` → a loud RuntimeError (the turn must not silently vanish)."""
    with pytest.raises(RuntimeError, match="tmux target was not advertised"):
        inject_user_message_via_tui(tmp_path / "bridge", content="hi", timeout_s=0.0)


def test_inject_user_message_via_tui_raises_when_session_dead(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _fast_tmux_timeouts: None,
) -> None:
    """A dead tmux pane (agy TUI exited) fails fast with a restart hint."""
    bridge_dir = tmp_path / "bridge"
    write_tmux_target(bridge_dir, socket_path=Path("/tmp/ex/tmux.sock"), tmux_target="main")

    def _fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        """has-session reports the pane is gone."""
        del kwargs
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", _fake_run)
    with pytest.raises(RuntimeError, match="no longer running"):
        inject_user_message_via_tui(bridge_dir, content="hi", timeout_s=0.5)


def test_inject_user_message_via_tui_raises_when_session_dies_before_submit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _fast_tmux_timeouts: None,
) -> None:
    """
    A TUI that exits between the readiness check and the submit fails fast.

    The entry liveness check passes, but ``_submit_and_verify`` re-checks before
    each Enter — so a pane that dies after the paste raises the specific
    "exited before the message could be submitted" error instead of spinning the
    full submit budget and then blaming paste-coalescing.
    """
    bridge_dir = tmp_path / "bridge"
    write_tmux_target(bridge_dir, socket_path=Path("/tmp/ex/tmux.sock"), tmux_target="main")
    has_session_calls = {"n": 0}
    tui = {"pane": "> \n? for shortcuts"}

    def _fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        """Alive at the entry check; dead by the time the submit re-checks."""
        del kwargs
        if "has-session" in cmd:
            has_session_calls["n"] += 1
            # 1st call = entry gate (alive); later calls = per-submit re-check (dead).
            return SimpleNamespace(returncode=0 if has_session_calls["n"] == 1 else 1, stdout="")
        if "capture-pane" in cmd:
            return SimpleNamespace(returncode=0, stdout=tui["pane"], stderr="")
        if "paste-buffer" in cmd:
            tui["pane"] = "> draft message\n? for shortcuts"
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", _fake_run)
    with pytest.raises(RuntimeError, match="exited before the message could be submitted"):
        inject_user_message_via_tui(bridge_dir, content="draft message", timeout_s=0.5)


def test_inject_user_message_via_tui_raises_when_paste_never_renders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _fast_tmux_timeouts: None,
) -> None:
    """A successful tmux paste command is not enough; the draft must render."""
    bridge_dir = tmp_path / "bridge"
    write_tmux_target(bridge_dir, socket_path=Path("/tmp/ex/tmux.sock"), tmux_target="main")

    def _fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        """Pane stays empty even after paste-buffer returns successfully."""
        del kwargs
        if "capture-pane" in cmd:
            return SimpleNamespace(returncode=0, stdout="> \n? for shortcuts", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", _fake_run)
    with pytest.raises(RuntimeError, match="did not render the pasted message"):
        inject_user_message_via_tui(bridge_dir, content="draft message", timeout_s=0.5)


def test_inject_user_message_via_tui_raises_when_turn_never_starts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _fast_tmux_timeouts: None,
) -> None:
    """
    If no turn ever starts after the submit attempts, the delivery raises.

    The pasted draft stays visible forever (Enter never takes), so after the
    bounded re-send budget the executor gets a clear error rather than a false
    success.
    """
    bridge_dir = tmp_path / "bridge"
    write_tmux_target(bridge_dir, socket_path=Path("/tmp/ex/tmux.sock"), tmux_target="main")
    tui = {"pane": "> \n? for shortcuts"}

    def _fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        """Pane never leaves idle — the submit never starts a turn."""
        del kwargs
        if "capture-pane" in cmd:
            return SimpleNamespace(returncode=0, stdout=tui["pane"], stderr="")
        if "paste-buffer" in cmd:
            tui["pane"] = "> draft message\n? for shortcuts"
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", _fake_run)
    with pytest.raises(RuntimeError, match="draft is still visible"):
        inject_user_message_via_tui(bridge_dir, content="draft message", timeout_s=0.5)


def test_inject_user_message_via_tui_rejects_empty_content(tmp_path: Path) -> None:
    """Empty content is a programming error, not something to type into the TUI."""
    with pytest.raises(RuntimeError, match="non-empty content"):
        inject_user_message_via_tui(tmp_path / "bridge", content="")


# ---------------------------------------------------------------------------
# Collapsed-paste draft detection
# ---------------------------------------------------------------------------


@pytest.fixture
def agy134_paste_captures() -> dict[str, str]:
    return {
        "mode_hint": """\
────────────────────────────────────────────────────────────────────────────────
> Accept-edits mode: file edits auto-approved (shift+tab to cycle)
────────────────────────────────────────────────────────────────────────────────
? for shortcuts                         accept-edits · Gemini 3.8 Flash · medium
""",
        "idle_empty": """\
────────────────────────────────────────────────────────────────────────────────
>
────────────────────────────────────────────────────────────────────────────────
? for shortcuts                         accept-edits · Gemini 3.8 Flash · medium
""",
        "placeholder": """\
────────────────────────────────────────────────────────────────────────────────
> [Pasted text #1 +15 lines]
────────────────────────────────────────────────────────────────────────────────
                                        accept-edits · Gemini 3.8 Flash · medium
""",
        "collapsed": """\
────────────────────────────────────────────────────────────────────────────────
> ↑ 9 more lines

  Paragraph 4: the quick brown fox jumps over the lazy dog while the runtime
  posts routine status messages into this session; treat them as status.

  Paragraph 5: the quick brown fox jumps over the lazy dog while the runtime
  posts routine status messages into this session; treat them as status.

  Paragraph 6: the quick brown fox jumps over the lazy dog while the runtime
  posts routine status messages into this session; treat them as status.

  </omnigent_agent_instructions>
  Reply with exactly PONG_FINAL
────────────────────────────────────────────────────────────────────────────────
                                        accept-edits · Gemini 3.8 Flash · medium
""",
    }


def test_agy_draft_candidate_lines_ignore_mode_hint(agy134_paste_captures: dict[str, str]) -> None:
    pane = agy134_paste_captures["mode_hint"]
    assert _mod._agy_draft_candidate_lines(_mod._agy_input_region(pane)) == []


def test_agy_draft_candidate_lines_keep_shortcut_suffix() -> None:
    pane = "> Explain the shortcut (shift+tab to cycle)\n? for shortcuts"
    assert _mod._agy_draft_candidate_lines(_mod._agy_input_region(pane)) == [
        "Explain the shortcut (shift+tab to cycle)"
    ]


def test_inject_user_message_via_tui_submits_shortcut_suffix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _fast_tmux_timeouts: None,
    agy134_paste_captures: dict[str, str],
) -> None:
    write_tmux_target(tmp_path, socket_path=Path("/tmp/ex/tmux.sock"), tmux_target="main")
    pane = agy134_paste_captures["mode_hint"]
    actions: list[str] = []

    def fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        nonlocal pane
        if "capture-pane" in cmd:
            return SimpleNamespace(returncode=0, stdout=pane, stderr="")
        if "send-keys" in cmd:
            actions.append(cmd[-1])
            if cmd[-1] == "Enter":
                assert pane == "> Explain the shortcut (shift+tab to cycle)\n? for shortcuts"
                pane = agy134_paste_captures["idle_empty"]
        elif "paste-buffer" in cmd:
            actions.append("paste")
            pane = "> Explain the shortcut (shift+tab to cycle)\n? for shortcuts"
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    inject_user_message_via_tui(
        tmp_path, content="Explain the shortcut (shift+tab to cycle)", timeout_s=0.01
    )
    assert actions == ["C-a", "C-k", "paste", "Enter"]
    assert pane == agy134_paste_captures["idle_empty"]


@pytest.mark.parametrize(
    "initial,submitted,expected_batches",
    [
        ("mode_hint", "mode_hint", 0),
        ("mode_hint", "idle_empty", 0),
        ("read_only_hint", "idle_empty", 1),
    ],
)
def test_inject_user_message_via_tui_replaces_idle_hints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _fast_tmux_timeouts: None,
    agy134_paste_captures: dict[str, str],
    initial: str,
    submitted: str,
    expected_batches: int,
) -> None:
    write_tmux_target(tmp_path, socket_path=Path("/tmp/ex/tmux.sock"), tmux_target="main")
    pane = (
        agy134_paste_captures["idle_empty"].replace(">\n", "> Some read-only hint\n")
        if initial == "read_only_hint"
        else agy134_paste_captures[initial]
    )
    actions: list[str] = []

    def fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        nonlocal pane
        if "capture-pane" in cmd:
            return SimpleNamespace(returncode=0, stdout=pane, stderr="")
        if "send-keys" in cmd:
            actions.append(cmd[-1])
            if cmd[-1] == "C-w":
                assert cmd[3:] == ["send-keys", "-t", "main", "-N", "300", "C-w"]
            elif cmd[-1] == "Enter":
                pane = agy134_paste_captures[submitted]
        elif "paste-buffer" in cmd:
            actions.append("paste")
            pane = agy134_paste_captures["collapsed"]
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    inject_user_message_via_tui(
        tmp_path,
        content="<omnigent_agent_instructions>\nbe terse\nReply with exactly PONG_FINAL",
        timeout_s=0.01,
    )
    assert actions == ["C-a", "C-k", *(["C-w"] * expected_batches), "paste", "Enter"]
    assert pane == agy134_paste_captures[submitted]


@pytest.mark.parametrize(
    "shape,tail_needle,expected",
    [
        ("collapsed", "Reply with exactly PONG_", True),
        ("collapsed", "", False),
        ("unrelated", "Reply with exactly PONG_", False),
        ("placeholder", "Reply with exactly PONG_", True),
        ("idle_empty", "Reply with exactly PONG_", False),
        ("plain", "Reply with exactly PONG_", True),
        ("singular", "Reply with exactly PONG_", True),
        ("no_marker", "Reply with exactly PONG_", False),
    ],
)
def test_draft_in_input_region_agy134_shapes(
    agy134_paste_captures: dict[str, str], shape: str, tail_needle: str, expected: bool
) -> None:
    panes = {
        **agy134_paste_captures,
        "plain": (
            "────────\n> <omnigent_agent_instructions>\n  be terse\n"
            "  </omnigent_agent_instructions>\n  Reply with exactly PONG_FINAL\n"
            "────────\n? for shortcuts\n"
        ),
        "singular": agy134_paste_captures["collapsed"].replace("↑ 9 more lines", "↑ 1 more line"),
        "no_marker": agy134_paste_captures["collapsed"].replace("> ↑ 9 more lines", ">"),
        "unrelated": agy134_paste_captures["collapsed"].replace(
            "Reply with exactly PONG_FINAL", "unrelated visible tail"
        ),
    }
    assert (
        _mod._draft_in_input_region(
            panes[shape], "<omnigent_agent_instructi", ">", tail_needle=tail_needle
        )
        is expected
    )


def test_draft_in_input_region_rejects_unchanged_captured_baseline(
    agy134_paste_captures: dict[str, str],
) -> None:
    pane = agy134_paste_captures["collapsed"]
    baseline = pane.split("\n", 1)[1].rsplit("\n────────", 1)[0]
    assert not _mod._draft_in_input_region(
        pane, "<omnigent_agent_instructi", baseline, tail_needle="Reply with exactly PONG_"
    )


@pytest.mark.parametrize(
    "pane,expected",
    [
        ("esc to cancel\n────────\n>\n────────\n? for shortcuts", False),
        ("────────\n> esc to cancel\n────────\n? for shortcuts", False),
        (
            "────────\n>\n────────\nesc to cancel                    "
            "accept-edits · Gemini 3.8 Flash · high",
            True,
        ),
        ("────────\n>\n────────\nstatus\nesc to cancel", True),
        ("> \nesc to cancel", True),
        ("> esc to cancel\n? for shortcuts", True),
        ("> \n? for shortcuts", False),
    ],
)
def test_agy_turn_running_uses_footer_when_separators_exist(pane: str, expected: bool) -> None:
    assert _mod._agy_turn_running(pane) is expected


@pytest.mark.parametrize("location", ["draft", "transcript"])
@pytest.mark.parametrize("footer,expected", [("? for shortcuts", False), ("esc to cancel", True)])
def test_agy_turn_running_ignores_embedded_separator(
    location: str, footer: str, expected: bool
) -> None:
    text = "esc to cancel\n────────\nmore text"
    pane = (
        f"────────\n> {text}\n────────\n{footer}"
        if location == "draft"
        else f"{text}\n────────\n>\n────────\n{footer}"
    )
    assert _mod._agy_turn_running(pane) is expected


@pytest.mark.parametrize("initial", ["draft_marker", "history_marker", "active", "capture_retry"])
def test_inject_user_message_via_tui_distinguishes_footer_and_retries_baseline_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _fast_tmux_timeouts: None,
    agy134_paste_captures: dict[str, str],
    initial: str,
) -> None:
    write_tmux_target(tmp_path, socket_path=Path("/tmp/ex/tmux.sock"), tmux_target="main")
    idle = agy134_paste_captures["idle_empty"]
    draft = agy134_paste_captures["collapsed"].replace(
        "Paragraph 4:", "Paragraph 4: esc to cancel"
    )
    draft += "? for shortcuts\n"
    history = "Earlier text: esc to cancel\n" if initial == "history_marker" else ""
    active = initial == "active"
    pane = (
        idle.replace("? for shortcuts", "esc to cancel")
        if active
        else history
        + (agy134_paste_captures["collapsed"] + "? for shortcuts\n" if history else draft)
    )
    actions: list[str] = []
    baseline_pending = False
    failed_captures = 0
    enters = 0

    def fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        nonlocal pane, baseline_pending, failed_captures, enters
        if "capture-pane" in cmd:
            if baseline_pending and initial == "capture_retry" and failed_captures == 0:
                failed_captures += 1
                return SimpleNamespace(returncode=1, stdout="", stderr="capture failed")
            baseline_pending = False
            return SimpleNamespace(returncode=0, stdout=pane, stderr="")
        if "send-keys" in cmd:
            actions.append(cmd[-1])
            if cmd[-1] == "C-k":
                baseline_pending = True
            elif cmd[-1] == "C-w":
                assert cmd[3:] == ["send-keys", "-t", "main", "-N", "300", "C-w"]
                pane = history + idle
            elif cmd[-1] == "Enter":
                enters += 1
                if active or enters == 2:
                    pane = history + idle.replace("? for shortcuts", "esc to cancel")
        elif "paste-buffer" in cmd:
            actions.append("paste")
            if not active:
                assert pane == history + idle, "leftover draft must clear before paste"
            pane = history + draft
            if active:
                pane = pane.replace("? for shortcuts", "esc to cancel")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    inject_user_message_via_tui(
        tmp_path,
        content="<omnigent_agent_instructions>\nesc to cancel\nReply with exactly PONG_FINAL",
        timeout_s=0.01,
    )
    assert actions == (
        ["C-a", "C-k", "paste", "Enter"]
        if active
        else ["C-a", "C-k", "C-w", "paste", "Enter", "Enter"]
    )
    assert failed_captures == (1 if initial == "capture_retry" else 0)


@pytest.mark.parametrize("marker_location", ["draft", "history"])
def test_submit_and_verify_rejects_draft_with_marker_outside_footer(
    monkeypatch: pytest.MonkeyPatch,
    _fast_tmux_timeouts: None,
    marker_location: str,
) -> None:
    pane = "────────\n> Keep this draft\n────────\n? for shortcuts"
    if marker_location == "draft":
        pane = pane.replace("Keep this draft", "Keep this draft esc to cancel")
    else:
        pane = "Earlier text: esc to cancel\n" + pane
    enters: list[str] = []

    def fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        if "send-keys" in cmd:
            enters.append(cmd[-1])
        return SimpleNamespace(returncode=0, stdout=pane, stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    with pytest.raises(RuntimeError, match="draft_visible=True"):
        _mod._submit_and_verify(
            "/tmp/ex/tmux.sock",
            "main",
            needle="Keep this draft",
            baseline_region=">",
            draft_seen=True,
        )
    assert enters == ["Enter", "Enter", "Enter"]


def test_paste_and_submit_rejects_failed_baseline_capture_before_paste(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _fast_tmux_timeouts: None
) -> None:
    captures = 0
    actions: list[str] = []

    def fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        nonlocal captures
        assert "load-buffer" not in cmd and "paste-buffer" not in cmd
        if "capture-pane" in cmd:
            captures += 1
            return SimpleNamespace(returncode=1, stdout="", stderr="capture failed")
        if "send-keys" in cmd:
            actions.append(cmd[-1])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    with pytest.raises(RuntimeError, match="baseline capture-pane failed before paste"):
        _mod._paste_and_submit(tmp_path, "/tmp/ex/tmux.sock", "main", content="New message")
    assert captures >= 2, "failed capture must be retried within the budget"
    assert actions == ["C-a", "C-k"]


def test_clear_idle_composer_rejects_empty_capture() -> None:
    with pytest.raises(RuntimeError, match="capture-pane failed; captured_pane_empty=True"):
        _mod._clear_idle_composer("/tmp/ex/tmux.sock", "main", "")


def test_paste_and_submit_rejects_failed_capture_after_idle_clear_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _fast_tmux_timeouts: None
) -> None:
    pane = "────────\n> [Pasted text #1 +17 lines]\n────────\n? for shortcuts"
    failed_captures = 0
    actions: list[str] = []

    def fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        nonlocal pane, failed_captures
        assert "load-buffer" not in cmd and "paste-buffer" not in cmd
        if "capture-pane" in cmd:
            if not pane:
                failed_captures += 1
            return SimpleNamespace(returncode=0, stdout=pane, stderr="")
        if "send-keys" in cmd:
            actions.append(cmd[-1])
            if cmd[-1] == "C-w":
                assert cmd[3:] == ["send-keys", "-t", "main", "-N", "300", "C-w"]
                pane = ""
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    with pytest.raises(RuntimeError, match="capture-pane failed; captured_pane_empty=True"):
        _mod._paste_and_submit(tmp_path, "/tmp/ex/tmux.sock", "main", content="New message")
    assert failed_captures >= 2, "failed capture must be retried within the batch window"
    assert actions == ["C-a", "C-k", "C-w"]


@pytest.mark.parametrize("marker_location", ["draft", "history"])
def test_paste_and_submit_does_not_treat_idle_marker_text_as_mid_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _fast_tmux_timeouts: None,
    marker_location: str,
) -> None:
    monkeypatch.setattr(_mod, "_PASTE_COMMIT_TIMEOUT_S", 0.01)
    idle = "────────\n>\n────────\n? for shortcuts"
    pane = idle
    actions: list[str] = []

    def fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        nonlocal pane
        if "capture-pane" in cmd:
            return SimpleNamespace(returncode=0, stdout=pane, stderr="")
        if "paste-buffer" in cmd:
            actions.append("paste")
            pane = idle.replace(">", "> unrelated tail")
            if marker_location == "draft":
                pane = pane.replace("> unrelated tail", "> unrelated tail esc to cancel")
            else:
                pane = "Earlier text: esc to cancel\n" + pane
        elif "send-keys" in cmd:
            actions.append(cmd[-1])
            if cmd[-1] == "C-w":
                pane = idle
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    with pytest.raises(RuntimeError, match="did not render the pasted message"):
        _mod._paste_and_submit(tmp_path, "/tmp/ex/tmux.sock", "main", content="New message")
    assert actions == ["C-a", "C-k", "paste", "C-w"]
    assert pane == idle


@pytest.mark.parametrize("marker_location", ["draft", "history"])
def test_account_verification_rejected_ignores_marker_outside_footer(
    monkeypatch: pytest.MonkeyPatch,
    _fast_tmux_timeouts: None,
    marker_location: str,
) -> None:
    pane = _VERIFYING_PANE
    if marker_location == "draft":
        pane = pane.replace("> ", "> esc to cancel")
    else:
        pane = "Earlier text: esc to cancel\n" + pane
    monkeypatch.setattr(_mod, "_capture_pane", lambda *_: pane)
    assert _mod._account_verification_rejected("/tmp/ex/tmux.sock", "main")


@pytest.mark.parametrize(
    "initial,expected_batches,submit_attempts,clear_delay",
    [
        ("leftover", 1, 1, 0),
        ("leftover", 1, 1, 2),
        ("placeholder", 1, 1, 0),
        ("active", 0, 1, 0),
        ("empty", 0, 1, 0),
        ("empty", 0, 2, 0),
    ],
)
def test_inject_user_message_via_tui_clears_idle_leftovers_and_submits_visible_tail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _fast_tmux_timeouts: None,
    agy134_paste_captures: dict[str, str],
    initial: str,
    expected_batches: int,
    submit_attempts: int,
    clear_delay: int,
) -> None:
    write_tmux_target(tmp_path, socket_path=Path("/tmp/ex/tmux.sock"), tmux_target="main")
    active = initial == "active"
    shape = (
        "idle_empty"
        if initial == "empty"
        else "placeholder"
        if initial == "placeholder"
        else "collapsed"
    )
    pane = agy134_paste_captures[shape]
    if active:
        pane += "esc to cancel\n"
    actions: list[str] = []
    enters = 0
    clearing = False

    def fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        nonlocal pane, enters, clearing, clear_delay
        assert "Escape" not in cmd and "C-c" not in cmd
        if "capture-pane" in cmd:
            if clearing:
                if clear_delay:
                    clear_delay -= 1
                else:
                    pane = agy134_paste_captures["idle_empty"]
                    clearing = False
            return SimpleNamespace(returncode=0, stdout=pane, stderr="")
        if "send-keys" in cmd:
            actions.append(cmd[-1])
            if cmd[-1] == "C-w":
                assert cmd[3:] == ["send-keys", "-t", "main", "-N", "300", "C-w"]
                clearing = True
            elif cmd[-1] == "Enter":
                enters += 1
                if enters == submit_attempts:
                    pane = agy134_paste_captures["idle_empty"]
        elif "paste-buffer" in cmd:
            actions.append("paste")
            if not active:
                assert pane == agy134_paste_captures["idle_empty"]
            pane = agy134_paste_captures["collapsed"] + ("esc to cancel\n" if active else "")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    content = "<omnigent_agent_instructions>\nbe terse\nReply with exactly PONG_FINAL"
    inject_user_message_via_tui(tmp_path, content=content, timeout_s=0.01)
    assert actions == [
        "C-a",
        "C-k",
        *(["C-w"] * expected_batches),
        "paste",
        *(["Enter"] * submit_attempts),
    ]


def test_inject_user_message_via_tui_rejects_uncleared_idle_composer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _fast_tmux_timeouts: None,
    agy134_paste_captures: dict[str, str],
) -> None:
    write_tmux_target(tmp_path, socket_path=Path("/tmp/ex/tmux.sock"), tmux_target="main")
    actions: list[str] = []
    pane = agy134_paste_captures["collapsed"]

    def fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        nonlocal pane
        assert "Escape" not in cmd and "C-c" not in cmd
        assert "load-buffer" not in cmd and "paste-buffer" not in cmd
        if "capture-pane" in cmd:
            return SimpleNamespace(returncode=0, stdout=pane, stderr="")
        if "send-keys" in cmd:
            actions.append(cmd[-1])
            if cmd[-1] == "C-w":
                assert cmd[3:] == ["send-keys", "-t", "main", "-N", "300", "C-w"]
                pane = pane.replace("Paragraph 4:", "Paragraph 4: remaining")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    with pytest.raises(RuntimeError, match="draft_candidates_empty=False") as exc:
        inject_user_message_via_tui(
            tmp_path, content="Reply with exactly PONG_FINAL", timeout_s=0.01
        )
    assert actions == ["C-a", "C-k", "C-w", "C-w", "C-w", "C-w"]
    assert "composer_tail:" in str(exc.value)
    assert "Reply with exactly PONG_FINAL" in str(exc.value)


@pytest.mark.parametrize(
    "after_wait,expected_batches",
    [
        ("draft", 1),
        ("stuck", 4),
        ("clear_error", 1),
        ("active", 0),
        ("empty", 0),
        ("empty_capture", 0),
    ],
)
def test_inject_user_message_via_tui_cleans_failed_idle_render(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _fast_tmux_timeouts: None,
    agy134_paste_captures: dict[str, str],
    after_wait: str,
    expected_batches: int,
) -> None:
    write_tmux_target(tmp_path, socket_path=Path("/tmp/ex/tmux.sock"), tmux_target="main")
    monkeypatch.setattr(_mod, "_PASTE_COMMIT_TIMEOUT_S", 0.01)
    pane = agy134_paste_captures["idle_empty"]
    actions: list[str] = []
    checks = 0
    recognize_draft = _mod._draft_in_input_region

    def recognize_and_advance(
        captured_pane: str, needle: str, baseline_region: str, *, tail_needle: str = ""
    ) -> bool:
        nonlocal checks, pane
        checks += 1
        if after_wait == "active":
            pane += "esc to cancel\n"
        elif after_wait == "empty_capture":
            pane = ""
        return recognize_draft(captured_pane, needle, baseline_region, tail_needle=tail_needle)

    def fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        nonlocal pane
        assert "Escape" not in cmd and "C-c" not in cmd
        if "capture-pane" in cmd:
            return SimpleNamespace(returncode=0, stdout=pane, stderr="")
        if "send-keys" in cmd:
            actions.append(cmd[-1])
            if cmd[-1] == "C-w":
                assert cmd[3:] == ["send-keys", "-t", "main", "-N", "300", "C-w"]
                assert checks > 0, "cleanup must follow the failed render wait"
                if after_wait == "clear_error":
                    return SimpleNamespace(returncode=1, stdout="", stderr="word deletion failed")
                if after_wait == "draft":
                    pane = agy134_paste_captures["idle_empty"]
                elif after_wait == "stuck":
                    pane = pane.replace(
                        "unrelated visible tail", "unrelated visible tail shrinking"
                    )
        elif "paste-buffer" in cmd:
            actions.append("paste")
            if after_wait != "empty":
                pane = agy134_paste_captures["collapsed"].replace(
                    "Reply with exactly PONG_FINAL", "unrelated visible tail"
                )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(_mod, "_draft_in_input_region", recognize_and_advance)
    with pytest.raises(RuntimeError, match="did not render the pasted message"):
        inject_user_message_via_tui(
            tmp_path,
            content="<omnigent_agent_instructions>\nReply with exactly PONG_FINAL",
            timeout_s=0.01,
        )
    assert actions == ["C-a", "C-k", "paste", *(["C-w"] * expected_batches)]
    if after_wait == "draft":
        assert pane == agy134_paste_captures["idle_empty"]


def test_draft_in_input_region_detects_collapsed_paste_placeholder() -> None:
    """
    agy's collapsed-paste placeholder counts as a rendered draft.

    Past ~13 line breaks agy replaces the pasted text with a single
    ``[Pasted text #N +M lines]`` row instead of echoing it, so the needle is
    never visible in the composer. Without this the render gate reads a
    correctly pasted multi-line prompt as "never rendered".
    """
    baseline = "> "
    pane = "> [Pasted text #1 +17 lines]\n? for shortcuts"
    assert _mod._draft_in_input_region(pane, "Find the current weather", baseline)
    # Once the placeholder leaves the composer the draft is gone (submit verified).
    submitted = "> \n? for shortcuts"
    assert not _mod._draft_in_input_region(submitted, "Find the current weather", baseline)


def test_inject_user_message_via_tui_submits_a_collapsed_paste(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _fast_tmux_timeouts: None,
) -> None:
    """
    A multi-line prompt agy collapses is still submitted, not reported as lost.

    Reproduces the polly sub-agent dispatch: the task prompt is long enough that
    agy shows only the placeholder, which previously failed the render gate and
    raised "did not render the pasted message" while the draft sat in the
    composer.
    """
    bridge_dir = tmp_path / "bridge"
    write_tmux_target(bridge_dir, socket_path=Path("/tmp/ex/tmux.sock"), tmux_target="main")
    content = "\n".join(f"Find the current weather line {i}" for i in range(1, 19))
    tui = {"pane": "> \n? for shortcuts"}
    enters = {"n": 0}

    def _fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        """agy collapses the paste, then starts a turn on Enter."""
        del kwargs
        if "has-session" in cmd:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "capture-pane" in cmd:
            return SimpleNamespace(returncode=0, stdout=tui["pane"], stderr="")
        if "paste-buffer" in cmd:
            tui["pane"] = "> [Pasted text #1 +18 lines]\n? for shortcuts"
        if cmd[-1] == "Enter":
            enters["n"] += 1
            tui["pane"] = "> \nesc to cancel"
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", _fake_run)
    inject_user_message_via_tui(bridge_dir, content=content, timeout_s=0.5)

    assert enters["n"] == 1, "the collapsed draft should submit on the first Enter"


# ---------------------------------------------------------------------------
# Account-verification re-delivery
# ---------------------------------------------------------------------------


_VERIFYING_PANE = (
    "⚠ Verifying your account...\n"
    "  ⎿  We're finishing verifying your account eligibility.\n"
    "     This usually takes a moment. Please try again shortly.\n"
    "────────\n> \n────────\n? for shortcuts"
)


def test_inject_user_message_via_tui_redelivers_while_account_verifying(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _fast_tmux_timeouts: None,
) -> None:
    """
    A turn agy answers with its account-verification notice is re-delivered.

    agy consumes a turn submitted before its startup eligibility check settles —
    the draft leaves the composer, so the submit verifies — and renders the
    notice instead of starting a cascade. Without a re-delivery the turn is lost
    and the terminal sits idle, which is what a programmatically dispatched
    sub-agent hits on every launch.
    """
    bridge_dir = tmp_path / "bridge"
    write_tmux_target(bridge_dir, socket_path=Path("/tmp/ex/tmux.sock"), tmux_target="main")
    content = "do the thing"
    tui = {"pane": "> \n? for shortcuts"}
    pastes = {"n": 0}

    def _fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        """Reject the first submit with the notice; start a turn on the second."""
        del kwargs
        if "has-session" in cmd:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "capture-pane" in cmd:
            return SimpleNamespace(returncode=0, stdout=tui["pane"], stderr="")
        if "paste-buffer" in cmd:
            pastes["n"] += 1
            tui["pane"] = f"> {content}\n? for shortcuts"
        if cmd[-1] == "Enter":
            tui["pane"] = _VERIFYING_PANE if pastes["n"] == 1 else "> \nesc to cancel"
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", _fake_run)
    inject_user_message_via_tui(bridge_dir, content=content, timeout_s=0.5)

    assert pastes["n"] == 2, "expected the rejected turn to be re-delivered once"


def test_inject_user_message_via_tui_raises_when_account_verification_never_clears(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _fast_tmux_timeouts: None,
) -> None:
    """
    Re-delivery is bounded: a never-clearing notice surfaces a clear error.

    Retrying forever would hang the turn; the executor needs a failure it can
    surface so the user learns the account is not usable yet.
    """
    monkeypatch.setattr(_mod, "_VERIFY_RETRY_TIMEOUT_S", 0.0)
    bridge_dir = tmp_path / "bridge"
    write_tmux_target(bridge_dir, socket_path=Path("/tmp/ex/tmux.sock"), tmux_target="main")
    content = "do the thing"
    tui = {"pane": "> \n? for shortcuts"}

    def _fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        """Every submit is answered with the verification notice."""
        del kwargs
        if "has-session" in cmd:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "capture-pane" in cmd:
            return SimpleNamespace(returncode=0, stdout=tui["pane"], stderr="")
        if "paste-buffer" in cmd:
            tui["pane"] = f"> {content}\n? for shortcuts"
        if cmd[-1] == "Enter":
            tui["pane"] = _VERIFYING_PANE
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", _fake_run)
    with pytest.raises(RuntimeError, match="still verifying the Antigravity account"):
        inject_user_message_via_tui(bridge_dir, content=content, timeout_s=0.5)


def test_inject_user_message_via_tui_does_not_redeliver_an_accepted_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _fast_tmux_timeouts: None,
) -> None:
    """
    A stale notice left on screen from a prior attempt never causes a duplicate.

    agy clears the notice only when a turn actually starts, so the running-turn
    marker must win over notice text still visible in the same capture.
    """
    bridge_dir = tmp_path / "bridge"
    write_tmux_target(bridge_dir, socket_path=Path("/tmp/ex/tmux.sock"), tmux_target="main")
    content = "do the thing"
    tui = {"pane": "> \n? for shortcuts"}
    pastes = {"n": 0}

    def _fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        """Start a turn while the previous attempt's notice is still rendered."""
        del kwargs
        if "has-session" in cmd:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "capture-pane" in cmd:
            return SimpleNamespace(returncode=0, stdout=tui["pane"], stderr="")
        if "paste-buffer" in cmd:
            pastes["n"] += 1
            tui["pane"] = f"> {content}\n? for shortcuts"
        if cmd[-1] == "Enter":
            tui["pane"] = _VERIFYING_PANE.replace("? for shortcuts", "esc to cancel")
        return SimpleNamespace(returncode=0, stdout=tui["pane"], stderr="")

    monkeypatch.setattr("subprocess.run", _fake_run)
    inject_user_message_via_tui(bridge_dir, content=content, timeout_s=0.5)

    assert pastes["n"] == 1, "a running turn must not be re-delivered"


def test_inject_user_message_via_tui_ignores_stale_notice_with_changed_active_footer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _fast_tmux_timeouts: None,
) -> None:
    """A renamed active footer still prevents a stale-notice duplicate."""
    monkeypatch.setattr(_mod, "_VERIFY_RETRY_TIMEOUT_S", 0.2)
    bridge_dir = tmp_path / "bridge"
    write_tmux_target(bridge_dir, socket_path=Path("/tmp/ex/tmux.sock"), tmux_target="main")
    content = "do the thing"
    tui = {"pane": "> \n? for shortcuts"}
    pastes = {"n": 0}

    def _fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        """Reject once, then accept while the stale notice remains visible."""
        del kwargs
        if "has-session" in cmd:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "capture-pane" in cmd:
            return SimpleNamespace(returncode=0, stdout=tui["pane"], stderr="")
        if "paste-buffer" in cmd:
            pastes["n"] += 1
            tui["pane"] = f"> {content}\n? for shortcuts"
        if cmd[-1] == "Enter":
            tui["pane"] = (
                _VERIFYING_PANE
                if pastes["n"] == 1
                else _VERIFYING_PANE.replace(
                    "? for shortcuts", "(generating response, press the cancel key to stop)"
                )
            )
        return SimpleNamespace(returncode=0, stdout=tui["pane"], stderr="")

    monkeypatch.setattr("subprocess.run", _fake_run)
    inject_user_message_via_tui(bridge_dir, content=content, timeout_s=0.5)

    assert pastes["n"] == 2, "the accepted retry must not be delivered again"


# ---------------------------------------------------------------------------
# Composer-region detection robustness (#1598 review follow-ups)
# ---------------------------------------------------------------------------


def test_agy_separator_line_accepts_pure_and_decorated_rules() -> None:
    """Separator detection accepts a pure ─ rule and corner/join-decorated rules.

    agy 1.0.14 renders a pure ``─`` composer rule, but a future build could frame
    it with box-drawing corners; detection must tolerate that without matching
    ordinary text or a short dash run.
    """
    assert _mod._agy_separator_line("─" * 60)
    assert _mod._agy_separator_line("╭" + "─" * 40 + "╮")
    assert _mod._agy_separator_line("├" + "─" * 20 + "┤")
    # Not separators: ordinary text, ASCII dashes, and < 8 rule chars.
    assert not _mod._agy_separator_line("> Generating the answer now")
    assert not _mod._agy_separator_line("--- short ---")
    assert not _mod._agy_separator_line("─────")


def test_draft_candidate_lines_keep_prompt_text_with_status_words() -> None:
    """A draft line carrying the ``>`` prompt is kept even if it contains a status word.

    The chrome filters (``Generating``/idle/active markers) must apply only to
    non-prompt rows; otherwise a legitimate message whose first line contains such
    a word is filtered out and the turn is misread as "never rendered".
    """
    region = "> Generating a fix for the esc to cancel bug\n? for shortcuts"
    candidates = _mod._agy_draft_candidate_lines(region)
    assert "Generating a fix for the esc to cancel bug" in candidates
    # The non-prompt idle footer row is still filtered.
    assert "? for shortcuts" not in candidates


def test_draft_in_input_region_keeps_prompt_text_with_status_words() -> None:
    """A draft whose first line contains 'Generating' is detected, not hard-failed."""
    sep = "─" * 60
    baseline = _mod._agy_input_region(f"{sep}\n>\n{sep}\n? for shortcuts")
    content = "Generating the migration is slow, please fix it"
    needle = _mod._submit_needle(content)
    pane = f"{sep}\n> {content}\n{sep}\n? for shortcuts"
    assert _mod._draft_in_input_region(pane, needle, baseline)


def test_draft_in_input_region_matches_short_message_by_composer_change() -> None:
    """An empty needle (short message) is detected by a changed composer, not text.

    A draft in a composer that differs from the pre-paste baseline counts as
    present; the unchanged baseline (or a fresh empty composer after submit) does
    not — so short messages are render/submit-verified instead of submitted blind.
    """
    sep = "─" * 60
    baseline = _mod._agy_input_region(f"{sep}\n>\n{sep}\n? for shortcuts")
    pane_with_draft = f"{sep}\n> ok\n{sep}\n? for shortcuts"
    pane_after_submit = f"{sep}\n> ok\n{sep}\n>\n{sep}\nesc to cancel"
    assert _mod._draft_in_input_region(pane_with_draft, "", baseline)
    assert not _mod._draft_in_input_region(pane_after_submit, "", baseline)


def test_format_pane_debug_tail_redacts_email_and_secrets() -> None:
    """The diagnostic pane tail redacts emails and common secret shapes (#1598)."""
    pane = (
        "signed in as alice@example.com\n"
        "key sk-ABCDEF0123456789ABCDEF\n"
        "export GH=ghp_ABCDEFGHIJ0123456789ABCDEFGHIJ\n"
        "Authorization: Bearer abcdef123456\n"
        "> draft text"
    )
    out = _mod._format_pane_debug_tail(pane)
    assert "alice@example.com" not in out
    assert "sk-ABCDEF0123456789ABCDEF" not in out
    assert "ghp_ABCDEFGHIJ0123456789ABCDEFGHIJ" not in out
    assert "[REDACTED_EMAIL]" in out
    assert "[REDACTED_SECRET]" in out
    assert "> draft text" in out  # non-secret context is preserved
    assert _mod._format_pane_debug_tail("") == "<empty pane>"


def test_inject_user_message_via_tui_delivers_short_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _fast_tmux_timeouts: None,
) -> None:
    """A short message with no stable needle ('ok') is render- and submit-verified.

    The empty-needle path must not submit blind: the draft is confirmed to render
    by composer change, and submission is confirmed by the draft clearing — so a
    legitimate short turn is delivered with exactly one verified Enter.
    """
    bridge_dir = tmp_path / "bridge"
    write_tmux_target(bridge_dir, socket_path=Path("/tmp/ex/tmux.sock"), tmux_target="main")
    enters = {"n": 0}
    tui = {"pane": "> \n? for shortcuts"}

    def _fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        """Idle, draft after paste, composer clears after Enter."""
        del kwargs
        if "has-session" in cmd:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "capture-pane" in cmd:
            return SimpleNamespace(returncode=0, stdout=tui["pane"], stderr="")
        if "paste-buffer" in cmd:
            tui["pane"] = "> ok\n? for shortcuts"
        if cmd[-1] == "Enter":
            enters["n"] += 1
            tui["pane"] = "> \n? for shortcuts"
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", _fake_run)
    inject_user_message_via_tui(bridge_dir, content="ok", timeout_s=0.5)
    assert enters["n"] == 1


def test_inject_user_message_via_tui_short_message_raises_when_not_submitted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _fast_tmux_timeouts: None,
) -> None:
    """A short message whose draft never clears fails loudly, not silently.

    Regression guard for the empty-needle path: previously a no-needle message was
    submitted with a single unverified Enter, so a folded Enter left it stuck in
    the composer and the turn was silently lost. It must now raise.
    """
    bridge_dir = tmp_path / "bridge"
    write_tmux_target(bridge_dir, socket_path=Path("/tmp/ex/tmux.sock"), tmux_target="main")
    tui = {"pane": "> \n? for shortcuts"}

    def _fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        """Draft renders but never clears (Enter folded into the paste burst)."""
        del kwargs
        if "has-session" in cmd:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "capture-pane" in cmd:
            return SimpleNamespace(returncode=0, stdout=tui["pane"], stderr="")
        if "paste-buffer" in cmd:
            tui["pane"] = "> ok\n? for shortcuts"
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", _fake_run)
    with pytest.raises(RuntimeError, match="draft is still visible"):
        inject_user_message_via_tui(bridge_dir, content="ok", timeout_s=0.5)


# ---------------------------------------------------------------------------
# Omnigent MCP relay wiring (sys_* tools) — #1194
# ---------------------------------------------------------------------------


def test_build_mcp_config_registers_omnigent_relay(tmp_path: Path) -> None:
    """build_mcp_config emits the shared serve-mcp relay command + enabledTools."""
    config = build_mcp_config(tmp_path, python_executable="python-test")
    server = config["mcpServers"]["omnigent"]
    assert server["command"] == "python-test"
    assert server["args"] == [
        "-I",
        "-m",
        "omnigent.harnesses.claude_native.bridge",
        "serve-mcp",
        "--bridge-dir",
        str(tmp_path),
    ]
    # agy's auto-approve allowlist is the enabledTools key (not cursor's autoApprove).
    assert "sys_session_send" in server["enabledTools"]
    assert "sys_os_shell" in server["enabledTools"]
    assert "sys_terminal_launch" in server["enabledTools"]
    assert "sys_list_models" in server["enabledTools"]
    assert server["enabledTools"] == sorted(server["enabledTools"])
    assert server["env"]["TMPDIR"]
    # The relay's HOME is pinned to the runner's real home so its bridge-root
    # validation matches where the bridge dir lives, even if a future agy launch
    # path customizes process environment.
    assert server["env"]["HOME"] == str(Path.home())


def test_build_mcp_config_defaults_python_to_current_interpreter(tmp_path: Path) -> None:
    """Omitting python_executable uses sys.executable so the relay runs in our venv."""
    import sys

    server = build_mcp_config(tmp_path)["mcpServers"]["omnigent"]
    assert server["command"] == sys.executable


def test_write_mcp_config_targets_isolated_agy_gemini_dir(tmp_path: Path) -> None:
    """write_mcp_config writes into the isolated agy Gemini dir, not ~/.gemini."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    path = write_mcp_config(bridge_dir, python_executable="python-test")

    # The config lands under the isolated --gemini_dir config path, so the user's
    # real ~/.gemini is untouched.
    assert path == agy_gemini_dir(bridge_dir) / "config" / "mcp_config.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["mcpServers"]["omnigent"]["command"] == "python-test"
    # The bridge token the shared relay requires is written into the bridge dir.
    assert json.loads((bridge_dir / "bridge.json").read_text(encoding="utf-8"))["token"]


def test_write_mcp_bridge_config_is_idempotent(tmp_path: Path) -> None:
    """A second write keeps the existing token so a live relay is not re-tokened."""
    write_mcp_bridge_config(tmp_path)
    first = (tmp_path / "bridge.json").read_text(encoding="utf-8")
    write_mcp_bridge_config(tmp_path)
    assert (tmp_path / "bridge.json").read_text(encoding="utf-8") == first


def test_agy_enabled_tools_match_native_relay_surface() -> None:
    from omnigent.runner.tool_dispatch import _NATIVE_RELAY_BUILTIN_TOOLS, _OS_ENV_TOOLS

    assert set(_mod._AGY_ENABLED_TOOLS) == set(_NATIVE_RELAY_BUILTIN_TOOLS | _OS_ENV_TOOLS)
    assert sorted(_mod._AGY_ENABLED_TOOLS) == _mod._AGY_ENABLED_TOOLS
    assert len(_mod._AGY_ENABLED_TOOLS) == len(set(_mod._AGY_ENABLED_TOOLS))
    assert "ask_user_async" in _mod._AGY_ENABLED_TOOLS
    assert "sys_read_inbox" in _mod._AGY_ENABLED_TOOLS
    assert "sys_session_handover" in _mod._AGY_ENABLED_TOOLS


def test_seed_isolated_agy_home_links_global_instructions_and_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Global instructions and both rule trees remain visible under --gemini_dir."""
    fake_home = tmp_path / "real-home"
    real_gemini = fake_home / ".gemini"
    contents = {
        Path("AGENTS.md"): "# global agents",
        Path("GEMINI.md"): "# global gemini",
        Path("config/rules/a.md"): "# global rule a",
        Path("antigravity-cli/rules/b.md"): "# global rule b",
    }
    for rel, content in contents.items():
        source = real_gemini / rel
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(content, encoding="utf-8")
    real_listing = sorted(p.relative_to(real_gemini) for p in real_gemini.rglob("*"))
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: fake_home))

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    iso_gemini = agy_gemini_dir(bridge_dir)
    for _ in range(2):
        seed_isolated_agy_home(bridge_dir)
        for rel in (
            Path("AGENTS.md"),
            Path("GEMINI.md"),
            Path("config/rules"),
            Path("antigravity-cli/rules"),
        ):
            link = iso_gemini / rel
            assert link.is_symlink()
            assert link.resolve() == real_gemini / rel
        for rel, content in contents.items():
            assert (iso_gemini / rel).read_text(encoding="utf-8") == content
            assert (real_gemini / rel).read_text(encoding="utf-8") == content
        assert sorted(p.relative_to(real_gemini) for p in real_gemini.rglob("*")) == real_listing

    empty_home = tmp_path / "empty-home"
    empty_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: empty_home))
    empty_bridge = tmp_path / "empty-bridge"
    seed_isolated_agy_home(empty_bridge)
    missing = agy_gemini_dir(empty_bridge) / "AGENTS.md"
    assert not missing.exists()
    assert not missing.is_symlink()


def test_seed_isolated_agy_home_links_symlinked_global_instructions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "real-home"
    real_gemini = fake_home / ".gemini"
    real_gemini.mkdir(parents=True)
    generation = tmp_path / "generation.md"
    generation.write_text("# published global agents", encoding="utf-8")
    (real_gemini / "AGENTS.md").symlink_to(generation)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: fake_home))

    bridge_dir = tmp_path / "bridge"
    seed_isolated_agy_home(bridge_dir)

    isolated_agents = agy_gemini_dir(bridge_dir) / "AGENTS.md"
    assert isolated_agents.is_symlink()
    assert isolated_agents.read_text(encoding="utf-8") == "# published global agents"


def test_seed_isolated_agy_home_seeds_markers_but_never_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Account, installation and onboarding markers seed without copying tokens."""
    fake_home = tmp_path / "real-home"
    (fake_home / ".gemini" / "antigravity-cli").mkdir(parents=True)
    (fake_home / ".gemini" / "oauth_creds.json").write_text(
        '{"access_token":"mac-token"}', encoding="utf-8"
    )
    (fake_home / ".gemini" / "installation_id").write_text("root-install-id", encoding="utf-8")
    (fake_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token").write_text(
        "linux-token", encoding="utf-8"
    )
    (fake_home / ".gemini" / "antigravity-cli" / "installation_id").write_text(
        "cli-install-id", encoding="utf-8"
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: fake_home))

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    env = seed_isolated_agy_home(bridge_dir)

    iso_gemini = agy_gemini_dir(bridge_dir)
    assert env == {}
    macos_token = iso_gemini / "oauth_creds.json"
    linux_token = iso_gemini / "antigravity-cli" / "antigravity-oauth-token"
    assert not macos_token.exists()
    assert not linux_token.exists()
    assert (iso_gemini / "installation_id").read_text(encoding="utf-8") == "root-install-id"
    assert (iso_gemini / "antigravity-cli" / "installation_id").read_text(
        encoding="utf-8"
    ) == "cli-install-id"
    # The real credential files are left in place.
    assert (fake_home / ".gemini" / "oauth_creds.json").read_text(encoding="utf-8") == (
        '{"access_token":"mac-token"}'
    )
    assert (fake_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token").read_text(
        encoding="utf-8"
    ) == "linux-token"
    # Onboarding + migration markers seeded so a headless launch never blocks.
    onboarding = iso_gemini / "antigravity-cli" / "cache" / "onboarding.json"
    assert json.loads(onboarding.read_text(encoding="utf-8"))["onboardingComplete"] is True
    assert (iso_gemini / "config" / ".migrated").is_file()


def test_seed_isolated_agy_home_trusts_workspace_in_isolated_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Workspace trust is seeded under isolated --gemini_dir, not real ~/.gemini."""
    fake_home = tmp_path / "real-home"
    real_settings = fake_home / ".gemini" / "antigravity-cli" / "settings.json"
    real_settings.parent.mkdir(parents=True)
    real_settings.write_text(
        json.dumps({"colorScheme": "dark", "trustedWorkspaces": ["/real/repo"]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: fake_home))

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    workspace = tmp_path / "worktrees" / "feature-x"
    workspace.mkdir(parents=True)
    iso_gemini = agy_gemini_dir(bridge_dir)
    isolated_settings = iso_gemini / "antigravity-cli" / "settings.json"
    isolated_settings.parent.mkdir(parents=True)
    isolated_settings.write_text(
        json.dumps({"colorScheme": "solarized dark", "trustedWorkspaces": ["/existing"]}),
        encoding="utf-8",
    )

    seed_isolated_agy_home(bridge_dir, trusted_workspace=workspace)

    isolated = json.loads(isolated_settings.read_text(encoding="utf-8"))
    assert isolated["colorScheme"] == "solarized dark"
    assert isolated["trustedWorkspaces"] == ["/existing", str(workspace.resolve())]
    # The user's real agy settings are never modified by this bridge-scoped seed.
    assert json.loads(real_settings.read_text(encoding="utf-8")) == {
        "colorScheme": "dark",
        "trustedWorkspaces": ["/real/repo"],
    }


def test_seed_isolated_agy_home_copies_real_settings_as_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh seed copies the user's real settings.json into the isolated dir.

    The real settings carry backend config agy needs at turn time — notably the
    ``gcp`` project/location block for GCP/enterprise logins, without which every
    turn fails server-side with ``invalid location: ""`` while the session still
    looks logged in.
    """
    fake_home = tmp_path / "real-home"
    real_settings = fake_home / ".gemini" / "antigravity-cli" / "settings.json"
    real_settings.parent.mkdir(parents=True)
    real_settings.write_text(
        json.dumps(
            {
                "gcp": {"project": "acme-prod", "location": "global"},
                "theme": "dark",
                "trustedWorkspaces": ["/real/repo"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: fake_home))

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    seed_isolated_agy_home(bridge_dir, trusted_workspace=workspace)

    iso_settings = agy_gemini_dir(bridge_dir) / "antigravity-cli" / "settings.json"
    isolated = json.loads(iso_settings.read_text(encoding="utf-8"))
    # Backend config survives the copy; the trust seeder merged on top of it.
    assert isolated["gcp"] == {"project": "acme-prod", "location": "global"}
    assert isolated["theme"] == "dark"
    assert isolated["trustedWorkspaces"] == ["/real/repo", str(workspace.resolve())]
    # The real settings file is never modified.
    assert json.loads(real_settings.read_text(encoding="utf-8"))["trustedWorkspaces"] == [
        "/real/repo"
    ]


def test_seed_isolated_agy_home_never_clobbers_existing_isolated_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A re-seed keeps the isolated settings file — per-session edits survive."""
    fake_home = tmp_path / "real-home"
    real_settings = fake_home / ".gemini" / "antigravity-cli" / "settings.json"
    real_settings.parent.mkdir(parents=True)
    real_settings.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: fake_home))

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    iso_settings = agy_gemini_dir(bridge_dir) / "antigravity-cli" / "settings.json"
    iso_settings.parent.mkdir(parents=True)
    iso_settings.write_text(json.dumps({"model": "session-pick"}), encoding="utf-8")

    seed_isolated_agy_home(bridge_dir)

    assert json.loads(iso_settings.read_text(encoding="utf-8")) == {"model": "session-pick"}


def test_seed_isolated_agy_home_prefers_real_onboarding_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The user's real onboarding marker is copied over the synthetic default.

    The real marker carries auth-flow state the synthetic one lacks
    (``enterpriseOnboardingComplete``, ``previousAuthMethod``); without it an
    enterprise/GCP login re-enters the first-run wizard, which blocks TUI
    injection.
    """
    fake_home = tmp_path / "real-home"
    real_marker = fake_home / ".gemini" / "antigravity-cli" / "cache" / "onboarding.json"
    real_marker.parent.mkdir(parents=True)
    real_state = {
        "consumerOnboardingComplete": True,
        "enterpriseOnboardingComplete": True,
        "onboardingComplete": True,
        "previousAuthMethod": "gcp",
    }
    real_marker.write_text(json.dumps(real_state), encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: fake_home))

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    seed_isolated_agy_home(bridge_dir)

    iso_marker = agy_gemini_dir(bridge_dir) / "antigravity-cli" / "cache" / "onboarding.json"
    assert json.loads(iso_marker.read_text(encoding="utf-8")) == real_state


def test_seed_isolated_agy_home_tolerates_absent_real_settings_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user with no settings.json seeds cleanly — no spurious gcp block appears.

    Non-GCP (consumer subscription) users have no settings.json; seeding must
    not fail or inject a gcp key the user never configured.
    """
    fake_home = tmp_path / "real-home"
    (fake_home / ".gemini" / "antigravity-cli").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: fake_home))

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    seed_isolated_agy_home(bridge_dir)

    iso_settings = agy_gemini_dir(bridge_dir) / "antigravity-cli" / "settings.json"
    # Absent, or created by the trust/survey seeders without a gcp key.
    if iso_settings.exists():
        assert "gcp" not in json.loads(iso_settings.read_text(encoding="utf-8"))


def test_seed_isolated_agy_home_synthetic_onboarding_does_not_downgrade_enterprise_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The synthetic onboarding marker never hard-wires enterpriseOnboardingComplete=false.

    When the real onboarding.json is absent (e.g. a cleared agy cache on a
    GCP-authenticated host), a synthetic marker with
    ``enterpriseOnboardingComplete: false`` re-triggers the first-run wizard
    for enterprise accounts, which blocks TUI injection. If the seeder writes
    the key at all, it must be True.
    """
    fake_home = tmp_path / "real-home"
    (fake_home / ".gemini" / "antigravity-cli").mkdir(parents=True)
    # No real onboarding.json.
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: fake_home))

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    seed_isolated_agy_home(bridge_dir)

    onboarding_path = agy_gemini_dir(bridge_dir) / "antigravity-cli" / "cache" / "onboarding.json"
    onboarding = json.loads(onboarding_path.read_text(encoding="utf-8"))
    assert onboarding.get("onboardingComplete") is True
    if "enterpriseOnboardingComplete" in onboarding:
        assert onboarding["enterpriseOnboardingComplete"] is True


def test_seed_isolated_agy_home_tolerates_missing_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing OAuth token only means agy re-auths — never a hard failure."""
    fake_home = tmp_path / "real-home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: fake_home))

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    env = seed_isolated_agy_home(bridge_dir)

    iso_gemini = agy_gemini_dir(bridge_dir)
    assert env == {}
    # Missing tokens do not prevent the isolated Gemini dir and markers from seeding.
    assert not (iso_gemini / "antigravity-cli" / "antigravity-oauth-token").exists()
    assert (iso_gemini / "antigravity-cli" / "cache" / "onboarding.json").is_file()


def test_seed_isolated_agy_home_exposes_user_plugins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The user's imported plugins (skills + hooks) are visible under --gemini_dir."""
    fake_home = tmp_path / "real-home"
    real_config = fake_home / ".gemini" / "config"
    (real_config / "plugins" / "superpowers" / "skills").mkdir(parents=True)
    (real_config / "plugins" / "superpowers" / "skills" / "brainstorming.md").write_text(
        "# brainstorming", encoding="utf-8"
    )
    manifest = json.dumps(
        {"imports": [{"name": "superpowers", "components": ["skills", "hooks"]}]}
    )
    (real_config / "import_manifest.json").write_text(manifest, encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: fake_home))

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    seed_isolated_agy_home(bridge_dir)

    iso_config = agy_gemini_dir(bridge_dir) / "config"
    # agy resolves plugins relative to --gemini_dir, so both the manifest and the
    # plugin payload must be reachable there or the session lists zero skills.
    assert json.loads((iso_config / "import_manifest.json").read_text(encoding="utf-8")) == (
        json.loads(manifest)
    )
    seeded_skill = iso_config / "plugins" / "superpowers" / "skills" / "brainstorming.md"
    assert seeded_skill.read_text(encoding="utf-8") == "# brainstorming"


def test_seed_isolated_agy_home_does_not_copy_plugin_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plugins are linked, not copied, so plugin updates are picked up live."""
    fake_home = tmp_path / "real-home"
    real_plugins = fake_home / ".gemini" / "config" / "plugins"
    (real_plugins / "superpowers").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: fake_home))

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    seed_isolated_agy_home(bridge_dir)

    iso_plugins = agy_gemini_dir(bridge_dir) / "config" / "plugins"
    assert iso_plugins.is_symlink()
    assert iso_plugins.resolve() == real_plugins.resolve()
    # A plugin installed/updated after the session started is still visible.
    (real_plugins / "later").mkdir()
    assert (iso_plugins / "later").is_dir()


def test_seed_isolated_agy_home_plugin_seed_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-seeding an existing bridge dir does not fail on the existing link."""
    fake_home = tmp_path / "real-home"
    (fake_home / ".gemini" / "config" / "plugins").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: fake_home))

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    seed_isolated_agy_home(bridge_dir)
    seed_isolated_agy_home(bridge_dir)

    iso_plugins = agy_gemini_dir(bridge_dir) / "config" / "plugins"
    assert iso_plugins.is_symlink()


def test_seed_isolated_agy_home_tolerates_absent_plugins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user with no imported plugins seeds cleanly — no link, no manifest."""
    fake_home = tmp_path / "real-home"
    (fake_home / ".gemini").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: fake_home))

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    seed_isolated_agy_home(bridge_dir)

    iso_config = agy_gemini_dir(bridge_dir) / "config"
    assert not (iso_config / "plugins").exists()
    assert not (iso_config / "import_manifest.json").exists()
    # The rest of the seed still landed.
    assert (iso_config / ".migrated").is_file()


def test_seed_isolated_agy_home_seeds_user_global_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "real-home"
    real_config = fake_home / ".gemini" / "config"
    real_config.mkdir(parents=True)
    hooks_payload = json.dumps(
        {"no-verify-gate": {"PreToolUse": [{"matcher": "run_command", "hooks": []}]}}
    )
    (real_config / "hooks.json").write_text(hooks_payload, encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: fake_home))

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    seed_isolated_agy_home(bridge_dir)

    iso_hooks = agy_gemini_dir(bridge_dir) / "config" / "hooks.json"
    assert json.loads(iso_hooks.read_text(encoding="utf-8")) == json.loads(hooks_payload)
    assert (real_config / "hooks.json").read_text(encoding="utf-8") == hooks_payload


def test_seed_isolated_agy_home_reseed_refreshes_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "real-home"
    real_hooks = fake_home / ".gemini" / "config" / "hooks.json"
    real_hooks.parent.mkdir(parents=True)
    real_hooks.write_text('{"old-gate": {}}', encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: fake_home))

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    seed_isolated_agy_home(bridge_dir)
    real_hooks.write_text('{"new-gate": {}}', encoding="utf-8")
    seed_isolated_agy_home(bridge_dir)

    iso_hooks = agy_gemini_dir(bridge_dir) / "config" / "hooks.json"
    assert json.loads(iso_hooks.read_text(encoding="utf-8")) == {"new-gate": {}}


def test_seed_isolated_agy_home_hooks_are_copied_not_linked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "real-home"
    real_hooks = fake_home / ".gemini" / "config" / "hooks.json"
    real_hooks.parent.mkdir(parents=True)
    real_hooks.write_text('{"gate": {}}', encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: fake_home))

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    seed_isolated_agy_home(bridge_dir)

    iso_hooks = agy_gemini_dir(bridge_dir) / "config" / "hooks.json"
    assert not iso_hooks.is_symlink()
    other_bridge = tmp_path / "other-bridge"
    seed_isolated_agy_home(other_bridge)
    iso_hooks.write_text('{"session-edit": {}}', encoding="utf-8")
    assert real_hooks.read_text(encoding="utf-8") == '{"gate": {}}'
    other_hooks = agy_gemini_dir(other_bridge) / "config" / "hooks.json"
    assert json.loads(other_hooks.read_text()) == {"gate": {}}


def test_seed_isolated_agy_home_tolerates_absent_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "real-home"
    (fake_home / ".gemini").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: fake_home))

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    seed_isolated_agy_home(bridge_dir)

    iso_config = agy_gemini_dir(bridge_dir) / "config"
    assert not (iso_config / "hooks.json").exists()
    assert (iso_config / ".migrated").is_file()


@pytest.fixture
def global_hooks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home with spaces ' $HOME"
    hooks = home / ".gemini" / "config" / "hooks.json"
    hooks.parent.mkdir(parents=True)
    hooks.write_text('{"example": {}}', encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    return hooks


@pytest.mark.posix_only
@pytest.mark.parametrize("event", ["PreToolUse", "PreInvocation"])
@pytest.mark.parametrize("script_path", ["../hooks/check.sh", "./scripts/check.sh", "absolute"])
def test_seed_isolated_agy_hooks_preserves_command_behavior(
    tmp_path: Path, global_hooks: Path, event: str, script_path: str
) -> None:
    script = global_hooks.parent / ("check.sh" if script_path == "absolute" else script_path)
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        "#!/bin/sh\ncat message.txt\ncat\nprintf 'diagnostic' >&2\nexit 23\n",
        encoding="utf-8",
    )
    script.chmod(0o700)
    (global_hooks.parent / "message.txt").write_text("relative data\n", encoding="utf-8")
    script_command = str(script) if script_path == "absolute" else script_path
    command = f"printf 'prefix: '; {shlex.quote(script_command)}"
    handler = {"command": command, "timeout": 10}
    handlers = (
        [handler] if event == "PreInvocation" else [{"matcher": "run_command", "hooks": [handler]}]
    )
    source = json.dumps({"example": {"enabled": True, event: handlers}})
    global_hooks.write_text(source, encoding="utf-8")
    bridge = tmp_path / "bridge"

    seed_isolated_agy_home(bridge)
    isolated = agy_gemini_dir(bridge) / "config" / "hooks.json"
    copied = json.loads(isolated.read_text())
    seeded_handler = copied["example"][event][0]
    if event == "PreToolUse":
        seeded_handler = seeded_handler["hooks"][0]
    for cwd, shell_command in (
        (global_hooks.parent, command),
        (isolated.parent, seeded_handler["command"]),
    ):
        result = subprocess.run(
            ["sh", "-c", shell_command],
            cwd=cwd,
            input="stdin payload",
            text=True,
            capture_output=True,
        )
        assert (result.returncode, result.stdout, result.stderr) == (
            23,
            "prefix: relative data\nstdin payload",
            "diagnostic",
        )
    assert global_hooks.read_text() == source
    original_copy = isolated.read_text()
    seed_isolated_agy_home(bridge)
    assert isolated.read_text() == original_copy
    seeded_handler["command"] = command
    assert copied == json.loads(source)


def test_seed_isolated_agy_hooks_removes_deleted_source(
    tmp_path: Path, global_hooks: Path
) -> None:
    bridge = tmp_path / "bridge"
    seed_isolated_agy_home(bridge)
    isolated = agy_gemini_dir(bridge) / "config" / "hooks.json"
    assert isolated.is_file()

    global_hooks.unlink()
    seed_isolated_agy_home(bridge)

    assert not isolated.exists()


@pytest.mark.posix_only
@pytest.mark.parametrize("source_exists", [True, False])
def test_seed_isolated_agy_hooks_does_not_follow_destination_symlink(
    tmp_path: Path, global_hooks: Path, source_exists: bool
) -> None:
    bridge = tmp_path / "bridge"
    seed_isolated_agy_home(bridge)
    isolated = agy_gemini_dir(bridge) / "config" / "hooks.json"
    target = tmp_path / "unrelated-file"
    target.write_text("keep this content", encoding="utf-8")
    target.chmod(0o640)
    isolated.unlink()
    isolated.symlink_to(target)
    if not source_exists:
        global_hooks.unlink()

    seed_isolated_agy_home(bridge)

    assert target.read_text() == "keep this content"
    assert target.stat().st_mode & 0o777 == 0o640
    assert not isolated.is_symlink()
    assert isolated.exists() == source_exists
    if source_exists:
        assert isolated.stat().st_mode & 0o777 == 0o600
        assert json.loads(isolated.read_text()) == {"example": {}}


@pytest.mark.parametrize("failure", ["read", "parse", "write", "replace"])
def test_seed_isolated_agy_hooks_keeps_previous_copy_on_refresh_error(
    tmp_path: Path,
    global_hooks: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: str,
) -> None:
    bridge = tmp_path / "bridge"
    seed_isolated_agy_home(bridge)
    isolated = agy_gemini_dir(bridge) / "config" / "hooks.json"
    previous = isolated.read_bytes()
    global_hooks.write_text('{"updated": {}}', encoding="utf-8")
    if failure == "read":
        read_text = Path.read_text

        def unreadable(path: Path, *args, **kwargs):
            if path == global_hooks:
                raise PermissionError("source unreadable")
            return read_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", unreadable)
    elif failure == "parse":
        global_hooks.write_text("{invalid json", encoding="utf-8")
    elif failure == "write":

        def interrupted_write(payload, stream, **kwargs):
            stream.write('{"partial":')
            raise OSError("write interrupted")

        monkeypatch.setattr(_mod.json, "dump", interrupted_write)
    else:
        replace = os.replace

        def failed_replace(src, dst):
            if dst == isolated:
                raise OSError("replace failed")
            return replace(src, dst)

        monkeypatch.setattr(_mod.os, "replace", failed_replace)

    seed_isolated_agy_home(bridge)

    assert isolated.read_bytes() == previous
    assert not list(isolated.parent.glob("hooks.json.*"))
    assert "Could not refresh Antigravity hooks" in caplog.text


def test_seed_isolated_agy_home_exposes_user_skill_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """agy's Global and Shared skill trees resolve under ``--gemini_dir``.

    The ``/skills`` menu enumerates these from the REAL home, so a session that
    cannot resolve them there would offer a skill agy then fails to expand.
    """
    fake_home = tmp_path / "real-home"
    real_global = fake_home / ".gemini" / "antigravity-cli" / "skills"
    real_shared = fake_home / ".gemini" / "skills"
    (real_global / "global-skill").mkdir(parents=True)
    (real_global / "global-skill" / "SKILL.md").write_text("# global", encoding="utf-8")
    (real_shared / "shared-skill").mkdir(parents=True)
    (real_shared / "shared-skill" / "SKILL.md").write_text("# shared", encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: fake_home))

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    seed_isolated_agy_home(bridge_dir)

    iso_gemini = agy_gemini_dir(bridge_dir)
    iso_global = iso_gemini / "antigravity-cli" / "skills"
    iso_shared = iso_gemini / "skills"
    # Linked, not copied: a skill authored mid-session is visible immediately.
    assert iso_global.is_symlink() and iso_shared.is_symlink()
    assert (iso_global / "global-skill" / "SKILL.md").read_text(encoding="utf-8") == "# global"
    assert (iso_shared / "shared-skill" / "SKILL.md").read_text(encoding="utf-8") == "# shared"
    (real_shared / "later").mkdir()
    assert (iso_shared / "later").is_dir()


def test_seed_isolated_agy_home_skill_dir_seed_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-seeding an existing bridge dir does not fail on the existing links."""
    fake_home = tmp_path / "real-home"
    (fake_home / ".gemini" / "antigravity-cli" / "skills").mkdir(parents=True)
    (fake_home / ".gemini" / "skills").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: fake_home))

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    seed_isolated_agy_home(bridge_dir)
    seed_isolated_agy_home(bridge_dir)

    iso_gemini = agy_gemini_dir(bridge_dir)
    assert (iso_gemini / "antigravity-cli" / "skills").is_symlink()
    assert (iso_gemini / "skills").is_symlink()


def test_seed_isolated_agy_home_tolerates_absent_skill_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user with no Global/Shared skills seeds cleanly — no links, no failure."""
    fake_home = tmp_path / "real-home"
    (fake_home / ".gemini").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: fake_home))

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    seed_isolated_agy_home(bridge_dir)

    iso_gemini = agy_gemini_dir(bridge_dir)
    assert not (iso_gemini / "skills").exists()
    assert not (iso_gemini / "antigravity-cli" / "skills").exists()
    # agy owns ``antigravity-cli`` itself; seeding must not have clobbered it.
    assert (iso_gemini / "antigravity-cli" / "cache" / "onboarding.json").is_file()


def test_seed_isolated_agy_home_skill_links_never_mutate_real_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seeding links INTO the real skill trees; it never writes through them."""
    fake_home = tmp_path / "real-home"
    real_shared = fake_home / ".gemini" / "skills"
    real_shared.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: fake_home))

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    seed_isolated_agy_home(bridge_dir)

    assert sorted(p.name for p in real_shared.iterdir()) == []


def test_agy_home_dir_is_under_bridge_dir(tmp_path: Path) -> None:
    """The isolated state parent is a child of the per-session bridge dir."""
    bridge_dir = tmp_path / "bridge"
    assert agy_home_dir(bridge_dir).parent == bridge_dir


def test_agy_gemini_dir_is_under_agy_home_dir(tmp_path: Path) -> None:
    """The isolated Gemini dir is passed to agy via --gemini_dir."""
    bridge_dir = tmp_path / "bridge"
    assert agy_gemini_dir(bridge_dir) == agy_home_dir(bridge_dir) / ".gemini"


def test_seeding_and_mcp_config_never_mutate_real_gemini_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Linux non-regression: the user's real ``~/.gemini`` is read-only to setup.

    This is the central promise of the ``--gemini_dir`` design: keeping the real
    ``HOME`` must NOT reintroduce the clobber/concurrency footgun the prior
    isolated-HOME design avoided. Both setup steps that touch the real
    ``~/.gemini`` — ``seed_isolated_agy_home`` (copies account/installation markers out) and
    ``write_mcp_config`` (writes the relay config) — must leave a fully populated
    real ``~/.gemini`` (interactive-agy user with their own MCP config) byte-for-byte
    untouched, writing only under the per-session isolated Gemini dir.
    """
    fake_home = tmp_path / "real-home"
    real_gemini = fake_home / ".gemini"
    (real_gemini / "antigravity-cli").mkdir(parents=True)
    (real_gemini / "config").mkdir(parents=True)
    # Populate the real tree as a logged-in interactive-agy user would have it,
    # including their OWN mcp_config.json the design must never clobber.
    seeded_files = {
        real_gemini / "oauth_creds.json": '{"access_token":"mac-token"}',
        real_gemini / "installation_id": "root-install-id",
        real_gemini / "antigravity-cli" / "antigravity-oauth-token": "linux-token",
        real_gemini / "antigravity-cli" / "installation_id": "cli-install-id",
        real_gemini / "config" / "mcp_config.json": '{"mcpServers":{"user_own":{}}}',
    }
    for path, body in seeded_files.items():
        path.write_text(body, encoding="utf-8")
    before = {path: path.read_text(encoding="utf-8") for path in seeded_files}
    real_listing_before = sorted(
        p.relative_to(real_gemini).as_posix() for p in real_gemini.rglob("*")
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: fake_home))

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    seed_isolated_agy_home(bridge_dir)
    write_mcp_config(bridge_dir, python_executable="python-test")

    # Every real file is byte-for-byte unchanged: nothing copied OUT was mutated,
    # and the user's own mcp_config.json was NOT overwritten by the relay config.
    for path, original in before.items():
        assert path.read_text(encoding="utf-8") == original
    # No new files leaked into the real tree (e.g. a relay config under real config/).
    real_listing_after = sorted(
        p.relative_to(real_gemini).as_posix() for p in real_gemini.rglob("*")
    )
    assert real_listing_after == real_listing_before
    # The relay config DID land in the isolated tree (positive control).
    assert (agy_gemini_dir(bridge_dir) / "config" / "mcp_config.json").is_file()


# ---------------------------------------------------------------------------
# Interaction-prompt TUI delivery (send_interaction_keys_via_tui) — #1200
# ---------------------------------------------------------------------------


def test_send_interaction_keys_via_tui_sends_one_send_keys_invocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The verdict keys go out as ONE ``send-keys`` so digit + Enter stay together."""
    bridge_dir = tmp_path / "bridge"
    write_tmux_target(bridge_dir, socket_path=Path("/tmp/ex/tmux.sock"), tmux_target="main")
    captured: list[list[str]] = []

    def _fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        del kwargs
        if "has-session" in cmd:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        captured.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", _fake_run)
    send_interaction_keys_via_tui(bridge_dir, "1", "Enter")

    assert captured == [
        ["tmux", "-S", "/tmp/ex/tmux.sock", "send-keys", "-t", "main", "1", "Enter"]
    ]


def test_send_interaction_keys_via_tui_reject_sequence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reject verdict's ``4`` + Enter sequence is forwarded verbatim."""
    bridge_dir = tmp_path / "bridge"
    write_tmux_target(bridge_dir, socket_path=Path("/tmp/ex/tmux.sock"), tmux_target="main")
    captured: list[list[str]] = []

    def _fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        del kwargs
        if "has-session" in cmd:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        captured.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", _fake_run)
    send_interaction_keys_via_tui(bridge_dir, "4", "Enter")

    assert captured == [
        ["tmux", "-S", "/tmp/ex/tmux.sock", "send-keys", "-t", "main", "4", "Enter"]
    ]


def test_send_interaction_keys_via_tui_raises_without_target(tmp_path: Path) -> None:
    """No advertised tmux target → a clear RuntimeError (no silent drop)."""
    with pytest.raises(RuntimeError, match="tmux target not advertised"):
        send_interaction_keys_via_tui(tmp_path / "bridge", "1", "Enter")


def test_send_interaction_keys_via_tui_raises_when_pane_gone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exited agy pane → RuntimeError so the bridge logs the best-effort miss."""
    bridge_dir = tmp_path / "bridge"
    write_tmux_target(bridge_dir, socket_path=Path("/tmp/ex/tmux.sock"), tmux_target="main")

    def _fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        del kwargs
        # has-session reports the pane is gone (non-zero exit).
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", _fake_run)
    with pytest.raises(RuntimeError, match="no longer running"):
        send_interaction_keys_via_tui(bridge_dir, "1", "Enter")


def test_send_interaction_keys_via_tui_rejects_empty_keys(tmp_path: Path) -> None:
    """No keys is a programming error, not an empty send-keys call."""
    with pytest.raises(RuntimeError, match="at least one key"):
        send_interaction_keys_via_tui(tmp_path / "bridge")


def _agy_settings_path(home: Path) -> Path:
    return home / ".gemini" / "antigravity-cli" / "settings.json"


def test_ensure_agy_feedback_survey_disabled_creates_missing_settings(tmp_path: Path) -> None:
    """A home with no settings.json gets one carrying just the disable key."""
    ensure_agy_feedback_survey_disabled(tmp_path)
    assert json.loads(_agy_settings_path(tmp_path).read_text(encoding="utf-8")) == {
        "showFeedbackSurvey": False
    }


def test_ensure_agy_settings_selects_gemini_for_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The isolated settings activate agy's direct Gemini API-key provider."""
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
    ensure_agy_feedback_survey_disabled(tmp_path)
    assert json.loads(_agy_settings_path(tmp_path).read_text(encoding="utf-8")) == {
        "modelProvider": "gemini",
        "showFeedbackSurvey": False,
    }


def test_ensure_agy_settings_clears_gemini_provider_when_api_key_disappears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resumed OAuth session is not trapped on stale API-key configuration."""
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
    ensure_agy_feedback_survey_disabled(tmp_path)
    monkeypatch.delenv("GEMINI_API_KEY")

    ensure_agy_feedback_survey_disabled(tmp_path)

    assert json.loads(_agy_settings_path(tmp_path).read_text(encoding="utf-8")) == {
        "showFeedbackSurvey": False
    }


def test_ensure_agy_settings_preserves_non_gemini_provider_without_api_key(
    tmp_path: Path,
) -> None:
    """Provider cleanup does not overwrite another explicitly selected backend."""
    settings = _agy_settings_path(tmp_path)
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps({"modelProvider": "vertex", "showFeedbackSurvey": False}),
        encoding="utf-8",
    )

    ensure_agy_feedback_survey_disabled(tmp_path)

    assert json.loads(settings.read_text(encoding="utf-8")) == {
        "modelProvider": "vertex",
        "showFeedbackSurvey": False,
    }


def test_ensure_agy_feedback_survey_disabled_merges_preserving_keys(tmp_path: Path) -> None:
    """The user's other settings (model/trustedWorkspaces/…) survive the merge."""
    settings = _agy_settings_path(tmp_path)
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps(
            {
                "enableTelemetry": False,
                "model": "Gemini 3.5 Flash (High)",
                "trustedWorkspaces": ["/work/a"],
            }
        ),
        encoding="utf-8",
    )
    ensure_agy_feedback_survey_disabled(tmp_path)
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["showFeedbackSurvey"] is False
    assert data["model"] == "Gemini 3.5 Flash (High)"
    assert data["trustedWorkspaces"] == ["/work/a"]
    assert data["enableTelemetry"] is False


def test_ensure_agy_feedback_survey_disabled_idempotent_no_rewrite(tmp_path: Path) -> None:
    """Already-false short-circuits before any (reformatting) rewrite."""
    settings = _agy_settings_path(tmp_path)
    settings.parent.mkdir(parents=True)
    original = '{"showFeedbackSurvey": false, "model": "x"}'  # compact + unsorted
    settings.write_text(original, encoding="utf-8")
    ensure_agy_feedback_survey_disabled(tmp_path)
    # Untouched byte-for-byte: the function returned at the already-disabled guard
    # rather than rewriting (which would indent + sort the keys).
    assert settings.read_text(encoding="utf-8") == original


def test_ensure_agy_feedback_survey_disabled_skips_malformed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A malformed settings.json is left untouched (never clobbered)."""
    settings = _agy_settings_path(tmp_path)
    settings.parent.mkdir(parents=True)
    settings.write_text("{ not valid json", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        ensure_agy_feedback_survey_disabled(tmp_path)
    assert settings.read_text(encoding="utf-8") == "{ not valid json"
    assert "not valid JSON" in caplog.text


def test_ensure_agy_feedback_survey_disabled_skips_non_dict(tmp_path: Path) -> None:
    """A JSON file that isn't an object is left untouched."""
    settings = _agy_settings_path(tmp_path)
    settings.parent.mkdir(parents=True)
    settings.write_text('["a", "b"]', encoding="utf-8")
    ensure_agy_feedback_survey_disabled(tmp_path)
    assert settings.read_text(encoding="utf-8") == '["a", "b"]'


def test_ensure_agy_feedback_survey_disabled_leaves_non_utf8_untouched(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Non-UTF-8 bytes must not crash the launch nor clobber the file (#1494 review)."""
    settings = _agy_settings_path(tmp_path)
    settings.parent.mkdir(parents=True)
    settings.write_bytes(b"\xff\xfe not valid utf-8 \x00")
    with caplog.at_level(logging.WARNING):
        ensure_agy_feedback_survey_disabled(tmp_path)  # must NOT raise
    assert settings.read_bytes() == b"\xff\xfe not valid utf-8 \x00"
    assert "not valid UTF-8" in caplog.text


def test_ensure_agy_feedback_survey_disabled_skips_unreadable_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An existing-but-unreadable file (OSError != FileNotFoundError) is not clobbered."""
    settings = _agy_settings_path(tmp_path)
    settings.parent.mkdir(parents=True)
    settings.write_text('{"model": "x"}', encoding="utf-8")

    def _boom(self: Path, *args: object, **kwargs: object) -> str:
        raise PermissionError("unreadable")

    monkeypatch.setattr(Path, "read_text", _boom)
    with caplog.at_level(logging.WARNING):
        ensure_agy_feedback_survey_disabled(tmp_path)
    # Path.read_text is patched, so read back via the builtin open.
    with open(settings, encoding="utf-8") as handle:
        assert handle.read() == '{"model": "x"}'
    assert "could not read" in caplog.text


def test_ensure_agy_feedback_survey_disabled_on_empty_file(tmp_path: Path) -> None:
    """An empty settings.json is treated as create-fresh."""
    settings = _agy_settings_path(tmp_path)
    settings.parent.mkdir(parents=True)
    settings.write_text("", encoding="utf-8")
    ensure_agy_feedback_survey_disabled(tmp_path)
    assert json.loads(settings.read_text(encoding="utf-8")) == {"showFeedbackSurvey": False}


def test_ensure_agy_feedback_survey_disabled_flips_true_to_false(tmp_path: Path) -> None:
    """An explicit ``true`` is flipped to ``false`` (other keys preserved)."""
    settings = _agy_settings_path(tmp_path)
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"showFeedbackSurvey": True, "model": "x"}), encoding="utf-8")
    ensure_agy_feedback_survey_disabled(tmp_path)
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data == {"showFeedbackSurvey": False, "model": "x"}


def test_ensure_agy_feedback_survey_disabled_follows_symlink(tmp_path: Path) -> None:
    """A symlinked settings.json (dotfiles) is followed, not replaced (agy review)."""
    real = tmp_path / "dotfiles" / "agy-settings.json"
    real.parent.mkdir(parents=True)
    real.write_text(json.dumps({"model": "x"}), encoding="utf-8")
    link = _agy_settings_path(tmp_path)
    link.parent.mkdir(parents=True)
    link.symlink_to(real)
    ensure_agy_feedback_survey_disabled(tmp_path)
    # The symlink is preserved (not clobbered into a regular file) and the real
    # target gained the key.
    assert link.is_symlink()
    assert json.loads(real.read_text(encoding="utf-8")) == {
        "model": "x",
        "showFeedbackSurvey": False,
    }


def test_ensure_agy_feedback_survey_disabled_write_failure_never_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A write-side OSError is swallowed + logged so the launch is never broken (#1494 review).

    The read of an existing-but-unreadable file is already covered; this exercises
    the WRITE phase guarantee — a failing atomic replace must not propagate out of
    a best-effort helper called inline on the launch path.
    """
    settings = _agy_settings_path(tmp_path)
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"model": "x"}), encoding="utf-8")

    def _boom(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    # os.replace is the last step of the atomic write; failing it stresses the
    # finally-cleanup + outer best-effort guard together.
    monkeypatch.setattr(_mod.os, "replace", _boom)
    with caplog.at_level(logging.WARNING):
        ensure_agy_feedback_survey_disabled(tmp_path)  # must NOT raise
    # The original file is untouched (the failed write never landed) and no stray
    # temp file is left behind in the settings dir.
    assert json.loads(settings.read_text(encoding="utf-8")) == {"model": "x"}
    leftover = [p.name for p in settings.parent.iterdir() if p.name != settings.name]
    assert leftover == []
    assert "could not write" in caplog.text


# ── owner-pid marker + orphan prune (bridge-dir reaping) ────────────────────


def test_prepare_bridge_dir_writes_owner_pid_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """prepare_bridge_dir records the creating pid so the periodic sweep can
    prune the dir only when its owner is provably dead."""
    import os

    monkeypatch.setattr(_mod, "_BRIDGE_ROOT", tmp_path / "antigravity-native")

    bridge_dir = _mod.prepare_bridge_dir("bridge_owner")

    assert (bridge_dir / "owner.pid").read_text(encoding="utf-8").strip() == str(os.getpid())


def test_prune_orphaned_bridge_dirs_only_removes_dead_owners(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prune removes only provably-dead-owner dirs held past the 70-day window."""
    import os
    import subprocess
    import sys
    import time

    root = tmp_path / "antigravity-native"
    root.mkdir(parents=True)
    monkeypatch.setattr(_mod, "_BRIDGE_ROOT", root)

    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    dead_dir = root / "deadowner"
    dead_dir.mkdir()
    dead_marker = dead_dir / "owner.pid"
    dead_marker.write_text(str(dead.pid), encoding="utf-8")
    now = time.time()
    expired_at = now - 71 * 24 * 60 * 60
    os.utime(dead_marker, (expired_at, expired_at))

    held_dir = root / "heldowner"
    held_dir.mkdir()
    held_marker = held_dir / "owner.pid"
    held_marker.write_text(str(dead.pid), encoding="utf-8")
    held_at = now - 8 * 24 * 60 * 60
    os.utime(held_marker, (held_at, held_at))

    live_dir = root / "liveowner"
    live_dir.mkdir()
    (live_dir / "owner.pid").write_text(str(os.getpid()), encoding="utf-8")

    unmarked_dir = root / "unmarked"
    unmarked_dir.mkdir()

    pruned = _mod.prune_orphaned_bridge_dirs()

    assert pruned == 1
    assert not dead_dir.exists()
    assert held_dir.exists()
    assert live_dir.exists()
    assert unmarked_dir.exists()


@pytest.mark.parametrize(
    "label,args,expected",
    [
        (None, [], "ask"),
        (None, ["--dangerously-skip-permissions"], "full"),
        ("invalid", [], "ask"),
        ("invalid", ["--dangerously-skip-permissions"], "full"),
        ("ask", ["--dangerously-skip-permissions"], "ask"),
        ("plan", [], "plan"),
        ("accept-edits", [], "accept-edits"),
        ("auto-strict", [], "auto-strict"),
        ("auto-standard", [], "auto-standard"),
        ("auto-relaxed", [], "auto-relaxed"),
        ("full", [], "full"),
    ],
)
def test_permission_tier_resolution(label: str | None, args: list[str], expected: str) -> None:
    labels = {_mod.AGY_PERMISSION_TIER_LABEL_KEY: label} if label is not None else None
    assert _mod.resolve_agy_permission_tier(labels, args) == expected


@pytest.mark.parametrize(
    "tier,expected",
    [
        ("ask", []),
        ("plan", ["--mode", "plan"]),
        ("accept-edits", ["--mode", "accept-edits"]),
        ("auto-strict", ["--mode", "accept-edits"]),
        ("auto-standard", ["--mode", "accept-edits"]),
        ("auto-relaxed", ["--mode", "accept-edits"]),
        ("full", ["--dangerously-skip-permissions"]),
    ],
)
def test_tier_launch_args(tier: str, expected: list[str]) -> None:
    assert _mod.agy_tier_launch_args(tier) == expected


@pytest.mark.parametrize("original", [None, False, True])
def test_permission_settings_snapshot_restore_and_hooks_merge(
    bridge_dir: Path, original: bool | None
) -> None:
    settings = agy_gemini_dir(bridge_dir) / "antigravity-cli" / "settings.json"
    settings.parent.mkdir(parents=True)
    user = {
        "custom": {"keep": 1},
        "agentMode": "plan",
        "toolPermission": "strict",
        "permissions": {
            "allow": ["command(echo safe)"],
            "ask": ["mcp(omnigent/sys_os_shell)"],
            "deny": ["mcp(omnigent/sys_os_write)"],
            "custom": True,
        },
    }
    if original is not None:
        user["allowNonWorkspaceAccess"] = original
    settings.write_text(json.dumps(user))
    hooks = agy_gemini_dir(bridge_dir) / "config" / "hooks.json"
    hooks.parent.mkdir(parents=True)
    user_hook = {
        "enabled": True,
        "SessionStart": [
            {"matcher": ".*", "hooks": [{"type": "command", "command": "echo hello"}]}
        ],
    }
    hooks.write_text(json.dumps({"user": user_hook, "omnigent-guard": {"enabled": False}}))
    _mod.apply_agy_permission_tier(bridge_dir, "auto-relaxed")
    merged = json.loads(settings.read_text())
    assert merged["toolPermission"] == "always-proceed"
    assert merged["allowNonWorkspaceAccess"] is True
    assert "agentMode" not in merged
    assert merged["custom"] == {"keep": 1}
    assert merged["permissions"] == {
        "allow": ["command(echo safe)", "mcp(omnigent/*)"],
        "ask": ["mcp(omnigent/sys_os_shell)"],
        "deny": ["mcp(omnigent/sys_os_write)"],
        "custom": True,
    }
    snapshot = bridge_dir / "agy_user_settings.json"
    saved = snapshot.read_bytes()
    assert json.loads(saved) == {
        "allowNonWorkspaceAccess": {"present": original is not None, "value": original}
    }
    guard_hook = json.loads(hooks.read_text())
    assert guard_hook["user"] == user_hook
    entry = guard_hook["omnigent-guard"]
    assert entry["enabled"] is True
    assert entry["PreToolUse"][0]["matcher"] == ".*"
    command_hook = entry["PreToolUse"][0]["hooks"][0]
    assert command_hook["type"] == "command"
    assert command_hook["timeout"] == 10
    import sys

    assert shlex.split(command_hook["command"]) == [
        sys.executable,
        "-I",
        str(Path(_mod.__file__).with_name("guard.py").resolve()),
        "--tier-file",
        str(bridge_dir / "permission_tier"),
    ]
    files = [settings, hooks, snapshot, bridge_dir / "permission_tier"]
    contents = [path.read_bytes() for path in files]
    _mod.apply_agy_permission_tier(bridge_dir, "auto-relaxed")
    assert [path.read_bytes() for path in files] == contents
    for tier in ["ask", "plan", "accept-edits", "full"]:
        _mod.apply_agy_permission_tier(bridge_dir, tier)
        merged = json.loads(settings.read_text())
        assert merged.get("toolPermission") == (None if tier == "full" else "request-review")
        assert ("allowNonWorkspaceAccess" in merged) == (original is not None)
        if original is not None:
            assert merged["allowNonWorkspaceAccess"] is original
        assert snapshot.read_bytes() == saved
        assert (bridge_dir / "permission_tier").read_text() == tier + "\n"
        assert json.loads(hooks.read_text())["user"] == user_hook
        assert ("omnigent-guard" in json.loads(hooks.read_text())) == (tier != "full")


@pytest.mark.parametrize(
    "relative",
    [
        "agy-home/.gemini/antigravity-cli/settings.json",
        "agy-home/.gemini/config/hooks.json",
        "agy_user_settings.json",
        "permission_tier",
    ],
)
def test_apply_tier_refuses_symlink_destination(
    bridge_dir: Path, tmp_path: Path, relative: str
) -> None:
    target = tmp_path / "user.json"
    target.write_text("{}")
    destination = bridge_dir / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        _mod.apply_agy_permission_tier(bridge_dir, "auto-relaxed")
    assert destination.is_symlink()
    assert target.read_text() == "{}"
    assert (
        not (bridge_dir / "agy_user_settings.json").exists()
        or relative == "agy_user_settings.json"
    )


def test_tier_reassert_repairs_drift_without_churning(bridge_dir: Path) -> None:
    tier = bridge_dir / "permission_tier"
    _mod.reassert_agy_permission_tier(bridge_dir, "auto-strict")
    assert tier.read_text() == "auto-strict\n"
    before = tier.stat().st_mtime_ns
    _mod.reassert_agy_permission_tier(bridge_dir, "auto-strict")
    assert tier.stat().st_mtime_ns == before
    tier.write_text("ask\n")
    _mod.reassert_agy_permission_tier(bridge_dir, "auto-strict")
    assert tier.read_text() == "auto-strict\n"


def test_apply_creates_missing_hooks(bridge_dir: Path) -> None:
    _mod.apply_agy_permission_tier(bridge_dir, "ask")
    hooks = agy_gemini_dir(bridge_dir) / "config" / "hooks.json"
    assert list(json.loads(hooks.read_text())) == ["omnigent-guard"]


def test_invalid_tier_and_malformed_settings_refused(bridge_dir: Path) -> None:
    with pytest.raises(ValueError, match="Unknown agy permission tier"):
        _mod.apply_agy_permission_tier(bridge_dir, "invalid")
    with pytest.raises(ValueError, match="Unknown agy permission tier"):
        _mod.agy_tier_launch_args("invalid")
    settings = agy_gemini_dir(bridge_dir) / "antigravity-cli" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("[]")
    with pytest.raises(ValueError, match="Expected JSON object"):
        _mod.apply_agy_permission_tier(bridge_dir, "auto-relaxed")
    assert settings.read_text() == "[]"


@pytest.fixture
def agy_controls_captures():
    return json.loads(
        (
            Path(__file__).resolve().parents[2] / "fixtures" / "antigravity" / "agy_controls.json"
        ).read_text()
    )


def test_agy_config_and_footer_captured_screens(agy_controls_captures):
    assert _mod.parse_agy_config_row(agy_controls_captures["settings"], "Agent Mode") == "default"
    assert (
        _mod.parse_agy_config_row(agy_controls_captures["tool-search"], "Tool Permission")
        == "request-review"
    )
    assert (
        _mod.parse_agy_config_row(agy_controls_captures["non-workspace"], "Non-Workspace Access")
        == "off"
    )
    modes = agy_controls_captures["modes"].split("--press ")[1:]
    assert [_mod.parse_agy_agent_mode(pane) for pane in modes] == [
        "accept-edits",
        "plan",
        "default",
    ]
    assert _mod.parse_agy_agent_mode("") is None


@pytest.mark.parametrize(
    "previous,tier,expected",
    [
        (
            "ask",
            "auto-relaxed",
            [
                ("tier", "auto-relaxed"),
                ("Tool Permission", "always-proceed"),
                ("Non-Workspace Access", "on"),
                ("mode", "accept-edits"),
            ],
        ),
        (
            "auto-standard",
            "plan",
            [
                ("Tool Permission", "request-review"),
                ("Non-Workspace Access", "off"),
                ("mode", "plan"),
                ("tier", "plan"),
            ],
        ),
        ("auto-strict", "auto-relaxed", [("tier", "auto-relaxed")]),
        ("ask", "plan", [("mode", "plan")]),
    ],
)
def test_agy_live_switch_order(tmp_path, monkeypatch, previous, tier, expected):
    calls = []
    (tmp_path / "agy_user_settings.json").write_text(
        '{"allowNonWorkspaceAccess": {"present": false, "value": null}}'
    )
    monkeypatch.setattr(_mod, "_require_agy_idle", lambda _: "? for shortcuts    Gemini · high")
    monkeypatch.setattr(
        _mod, "reassert_agy_permission_tier", lambda _, value: calls.append(("tier", value))
    )
    monkeypatch.setattr(
        _mod, "_set_agy_config_row", lambda _, row, target, **kw: calls.append((row, target))
    )
    monkeypatch.setattr(
        _mod, "_set_agy_agent_mode", lambda _, target, **kw: calls.append(("mode", target))
    )
    monkeypatch.setattr(
        _mod, "apply_agy_permission_tier", lambda _, value: calls.append(("settings", value))
    )
    assert (
        _mod.switch_agy_permission_tier(tmp_path, tier=tier, previous=previous, timeout_s=0.1)
        == tier
    )
    assert calls == [*expected, ("settings", tier)]


@pytest.mark.parametrize("undo_fails", [False, True])
def test_agy_live_switch_reverse_undo(tmp_path, monkeypatch, undo_fails):
    calls = []
    (tmp_path / "agy_user_settings.json").write_text(
        '{"allowNonWorkspaceAccess": {"present": false, "value": null}}'
    )
    monkeypatch.setattr(_mod, "_require_agy_idle", lambda _: "? for shortcuts    Gemini · high")
    monkeypatch.setattr(_mod, "_agy_control_pane", lambda _: "settings verification failed")

    def config(_, row, value, **kw):
        calls.append((row, value))
        if row == "Non-Workspace Access" and value == "off":
            raise RuntimeError("Non-Workspace Access: expected off, observed on")
        if undo_fails and row == "Tool Permission" and value == "always-proceed":
            raise RuntimeError("Tool Permission: expected always-proceed, observed request-review")

    monkeypatch.setattr(_mod, "_set_agy_config_row", config)
    monkeypatch.setattr(
        _mod, "apply_agy_permission_tier", lambda _, tier: calls.append(("settings", tier))
    )
    with pytest.raises(_mod.AgyPermissionRollbackError if undo_fails else RuntimeError):
        _mod.switch_agy_permission_tier(
            tmp_path, tier="ask", previous="auto-relaxed", timeout_s=0.1
        )
    assert calls[:4] == [
        ("Tool Permission", "request-review"),
        ("Non-Workspace Access", "off"),
        ("Non-Workspace Access", "on"),
        ("Tool Permission", "always-proceed"),
    ]
    if undo_fails:
        assert (tmp_path / "permission_tier").read_text() == "auto-strict\n"
    else:
        assert calls[-1] == ("settings", "auto-relaxed")


def test_agy_busy_and_full_refuse_before_mutation(tmp_path, monkeypatch):
    monkeypatch.setattr(_mod, "_agy_control_pane", lambda _: "esc to cancel")
    with pytest.raises(RuntimeError, match="busy"):
        _mod.switch_agy_permission_tier(
            tmp_path, tier="auto-relaxed", previous="ask", timeout_s=0.1
        )
    with pytest.raises(ValueError, match="create-only"):
        _mod.switch_agy_permission_tier(tmp_path, tier="ask", previous="full", timeout_s=0.1)
    assert not (tmp_path / "permission_tier").exists()


def test_agy_live_switch_requires_user_settings_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(_mod, "_require_agy_idle", lambda _: "? for shortcuts    Gemini · high")
    with pytest.raises(ValueError, match="missing user settings snapshot"):
        _mod.switch_agy_permission_tier(
            tmp_path, tier="auto-relaxed", previous="ask", timeout_s=0.1
        )
    assert not (tmp_path / "agy_user_settings.json").exists()


def test_agy_config_driver_verifies_reopen(tmp_path, monkeypatch, agy_controls_captures):
    calls = []
    state = {"opened": False, "filter": False, "value": "request-review"}

    def slash(_, command, **kw):
        calls.append(command)
        state["opened"] = True
        return "Settings"

    def keys(_, *keys):
        calls.extend(keys)
        if keys == ("Right",):
            state["value"] = {
                "request-review": "proceed-in-sandbox",
                "proceed-in-sandbox": "always-proceed",
            }[state["value"]]
        elif keys == ("Escape",):
            if state["filter"]:
                state["filter"] = False
            else:
                state["opened"] = False

    def run(_, *args):
        assert args[-2:] == ("-l", "tool perm")
        calls.append("tool perm")
        state["filter"] = True

    monkeypatch.setattr(_mod, "run_agy_slash_command", slash)
    monkeypatch.setattr(_mod, "send_interaction_keys_via_tui", keys)
    monkeypatch.setattr(
        _mod, "read_tmux_info", lambda _: {"socket_path": "socket", "tmux_target": "main"}
    )
    monkeypatch.setattr(_mod, "_run_tmux", run)
    monkeypatch.setattr(_mod.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        _mod,
        "_agy_control_pane",
        lambda _: (
            agy_controls_captures["tool-search"].replace("request-review", state["value"])
            if state["opened"]
            else "? for shortcuts    Gemini · high"
        ),
    )
    _mod._set_agy_config_row(tmp_path, "Tool Permission", "always-proceed", timeout_s=0.1)
    assert calls == [
        "/config",
        "tool perm",
        "Right",
        "Right",
        "Escape",
        "Escape",
        "/config",
        "tool perm",
        "Escape",
        "Escape",
    ]


def test_agy_slash_command_requires_fresh_confirmation(
    tmp_path, monkeypatch, agy_controls_captures
):
    baseline = "? for shortcuts    Gemini · high"
    panes = iter([baseline, agy_controls_captures["model-effort"]])
    monkeypatch.setattr(_mod, "_agy_control_pane", lambda _: next(panes))
    monkeypatch.setattr(
        _mod, "read_tmux_info", lambda _: {"socket_path": "socket", "tmux_target": "main"}
    )
    commands = []
    monkeypatch.setattr(_mod, "_paste_and_submit", lambda *a, **kw: commands.append(kw["content"]))
    assert (
        _mod.run_agy_slash_command(
            tmp_path, "/model claude-sonnet-5-5-medium", expect="Model set to", timeout_s=0.1
        )
        == "⎿  Model set to Claude Sonnet 5.5 (Medium)"
    )
    assert commands == ["/model claude-sonnet-5-5-medium"]


def test_agy_agent_mode_cycles_captured_footers(tmp_path, monkeypatch, agy_controls_captures):
    panes = agy_controls_captures["modes"].split("--press ")[1:]
    state = {"index": 2}
    calls = []

    def keys(_, *keys):
        assert keys == ("BTab",)
        calls.append(keys)
        state["index"] = (state["index"] + 1) % 3

    monkeypatch.setattr(_mod, "_agy_control_pane", lambda _: panes[state["index"]])
    monkeypatch.setattr(_mod, "send_interaction_keys_via_tui", keys)
    _mod._set_agy_agent_mode(tmp_path, "plan", timeout_s=0.1)
    assert calls == [("BTab",), ("BTab",)]


def test_agy_controls_do_not_treat_old_footer_as_idle(
    tmp_path, monkeypatch, agy_controls_captures
):
    pane = "? for shortcuts    Gemini · high\n" + agy_controls_captures["tool-search"]
    monkeypatch.setattr(_mod, "_agy_control_pane", lambda _: pane)
    assert _mod.parse_agy_agent_mode(pane) is None
    with pytest.raises(RuntimeError, match="input is not idle"):
        _mod.run_agy_slash_command(
            tmp_path, "/effort low", expect="Effort set to low", timeout_s=0.1
        )


def test_agy_unwritable_safe_tier_still_requests_cold_resume(tmp_path, monkeypatch):
    (tmp_path / "agy_user_settings.json").write_text(
        '{"allowNonWorkspaceAccess": {"present": false, "value": null}}'
    )
    monkeypatch.setattr(_mod, "_require_agy_idle", lambda _: "? for shortcuts    Gemini · high")

    def fail(*args, **kwargs):
        raise RuntimeError("Tool Permission verification failed")

    def fail_tier(*args, **kwargs):
        raise ValueError("Refusing symlink permission file")

    monkeypatch.setattr(_mod, "_set_agy_config_row", fail)
    monkeypatch.setattr(_mod, "reassert_agy_permission_tier", fail_tier)
    monkeypatch.setattr(_mod, "_agy_control_pane", fail)
    with pytest.raises(
        _mod.AgyPermissionRollbackError, match="deny-safe tier could not be written"
    ):
        _mod.switch_agy_permission_tier(
            tmp_path, tier="ask", previous="auto-relaxed", timeout_s=0.1
        )


def test_agy_old_active_marker_does_not_block_idle_footer(tmp_path, monkeypatch):
    pane = "Old tool output: esc to cancel\n? for shortcuts    Gemini · high"
    monkeypatch.setattr(_mod, "_agy_control_pane", lambda _: pane)
    assert _mod._require_agy_idle(tmp_path) == pane
