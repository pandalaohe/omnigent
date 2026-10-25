"""The host validates startup permission before configuration probes or spawn."""

import json
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from omnigent.host.connect import HostProcess, _RunnerHandle
from omnigent.host.frames import (
    CONTEXT_INVENTORY_V2,
    HOST_CAPABILITIES,
    HostContextInventoryV2Frame,
    HostContextInventoryV2ResultFrame,
    HostLaunchRunnerFrame,
    HostOwnerInventoryResultFrame,
    HostOwnerInventorySnapshot,
    HostStartupClaimFrame,
    HostStartupClaimResultFrame,
    decode_host_frame,
    encode_host_frame,
    host_context_inventory_evidence_v2,
)
from omnigent.host.identity import HostIdentity
from omnigent.startup_protocol import CONTEXT_INVENTORY_MAX_BYTES, StartupAttemptRef


def attempt():
    return StartupAttemptRef(
        startup_id=uuid4().hex,
        attempt_id=uuid4().hex,
        input_id=uuid4().hex,
        runtime_id="runtime-generation",
        host_generation="connection",
    )


def test_claim_and_launch_frames_keep_exact_attempt_metadata():
    ref = attempt()
    frames = [
        HostStartupClaimFrame(
            request_id="claim", session_id=uuid4().hex, runner_id="runner", attempt=ref
        ),
        HostStartupClaimResultFrame(request_id="claim", granted=False),
        HostLaunchRunnerFrame(
            request_id="launch",
            binding_token="test-binding",
            workspace="/opt/work/project",
            session_id=uuid4().hex,
            startup=ref,
        ),
    ]
    for frame in frames:
        assert decode_host_frame(encode_host_frame(frame)) == frame


@pytest.mark.asyncio
async def test_refused_startup_does_no_configuration_probe_or_spawn(monkeypatch, tmp_path):
    host = HostProcess(
        HostIdentity(uuid4().hex, "sample-host"),
        "http://localhost:8000",
        config_path=tmp_path / "config.yaml",
        interactive_shells=["bash"],
    )
    claim = AsyncMock(return_value=False)
    monkeypatch.setattr(host, "_claim_startup", claim)
    probe = Mock(side_effect=AssertionError("preflight must follow admission"))
    monkeypatch.setattr("omnigent.host.connect.harness_is_configured", probe)
    spawn = Mock(side_effect=AssertionError("spawn must follow admission"))
    monkeypatch.setattr(host, "_spawn_runner_proc", spawn)
    result = await host._handle_launch_impl(
        HostLaunchRunnerFrame(
            request_id="launch",
            binding_token="test-binding",
            workspace="/opt/work/project",
            session_id=uuid4().hex,
            harness="codex-native",
            startup=attempt(),
        )
    )
    assert result.status == "failed" and result.error_code == "startup_not_admitted"
    assert claim.await_count == 1
    probe.assert_not_called()
    spawn.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("replace_connection", [False, True])
async def test_claim_response_is_bound_to_its_authenticated_connection(
    monkeypatch, tmp_path, replace_connection
):
    host = HostProcess(
        HostIdentity(uuid4().hex, "sample-host"),
        "http://localhost:8000",
        config_path=tmp_path / "config.yaml",
        interactive_shells=["bash"],
    )
    ws = object()
    host._ws = ws
    host._startup_generation = "connection"

    async def reply(connection, raw):
        request = decode_host_frame(raw)
        assert isinstance(request, HostStartupClaimFrame)
        assert connection is ws
        if replace_connection:
            host._startup_generation = "replacement"
        await host._dispatch_host_frame(ws, HostStartupClaimResultFrame(request.request_id, True))

    monkeypatch.setattr(host, "_send_text", reply)
    frame = HostLaunchRunnerFrame(
        request_id="launch",
        binding_token="test-binding",
        workspace="/opt/work/project",
        session_id=uuid4().hex,
        startup=attempt(),
    )
    assert await host._claim_startup(frame) is not replace_connection
    assert host._startup_claims == {}


def _host_process(tmp_path) -> HostProcess:
    return HostProcess(
        HostIdentity(uuid4().hex, "sample-host"),
        "http://localhost:8000",
        config_path=tmp_path / "config.yaml",
        interactive_shells=["bash"],
    )


class _LiveProcess:
    """A resident runner process stand-in: alive, no output, never reaped here."""

    returncode = None

    def poll(self):
        return None


def test_context_inventory_v2_frames_round_trip_and_reject_a_foreign_read():
    request = HostContextInventoryV2Frame(
        request_id="read",
        connection_id="connection",
        host_generation="connection",
        read_id="read-one",
        session_id="session-one",
    )
    assert decode_host_frame(encode_host_frame(request)) == request
    assert CONTEXT_INVENTORY_V2 in HOST_CAPABILITIES

    evidence = host_context_inventory_evidence_v2(
        read_id=request.read_id,
        session_id=request.session_id,
        host_id="sample-host",
        connection_id=request.connection_id,
        host_generation=request.host_generation,
    )
    result = HostContextInventoryV2ResultFrame(
        request_id="read",
        connection_id="connection",
        host_generation="connection",
        read_id="read-one",
        session_id="session-one",
        evidence=evidence.model_dump(mode="json"),
        inventory=HostOwnerInventorySnapshot((), (), False, False, "token"),
    )
    decoded = decode_host_frame(encode_host_frame(result))
    assert decoded == result

    foreign = HostContextInventoryV2ResultFrame(
        request_id="read",
        connection_id="connection",
        host_generation="connection",
        read_id="other-read",
        session_id="session-one",
        evidence=evidence.model_dump(mode="json"),
        inventory=result.inventory,
    )
    rejected = decode_host_frame(encode_host_frame(foreign))
    assert rejected.evidence is None and rejected.inventory is None

    unknown_field = dict(evidence.model_dump(mode="json"), invented=True)
    rejected = decode_host_frame(
        encode_host_frame(
            HostContextInventoryV2ResultFrame(
                request_id="read",
                connection_id="connection",
                host_generation="connection",
                read_id="read-one",
                session_id="session-one",
                evidence=unknown_field,
                inventory=None,
            )
        )
    )
    assert rejected.evidence is None


@pytest.mark.asyncio
async def test_host_answers_context_inventory_v2_only_on_its_connection(monkeypatch, tmp_path):
    host = _host_process(tmp_path)
    ws = object()
    host._ws = ws
    host._startup_generation = "connection"
    host._runners["runner-one"] = _RunnerHandle(
        proc=_LiveProcess(),
        log_path=tmp_path / "runner-one.log",
        session_id="session-one",
    )
    replies = []

    async def reply(connection, raw):
        assert connection is ws
        replies.append(decode_host_frame(raw))

    monkeypatch.setattr(host, "_send_text", reply)
    await host._dispatch_host_frame(
        ws,
        HostContextInventoryV2Frame(
            "read-one", "connection", "connection", "read-one", "session-one"
        ),
    )
    result = replies[0]
    assert isinstance(result, HostContextInventoryV2ResultFrame)
    assert result.evidence is not None and result.inventory is not None
    assert result.inventory.runners[0].session_id == "session-one"
    assert result.evidence["sources"][0]["coverage"] == "unsupported"

    await host._dispatch_host_frame(
        ws,
        HostContextInventoryV2Frame(
            "read-two", "replacement", "replacement", "read", "session-one"
        ),
    )
    assert replies[1].evidence is None and replies[1].inventory is None


def _v2_result_wire(*unresolved: dict[str, object]) -> str:
    """Raw wire text for a valid host v2 result, optionally with extra unresolved rows."""
    from omnigent.host.frames import host_context_inventory_evidence_v2

    evidence = host_context_inventory_evidence_v2(
        read_id="read-one",
        session_id="session-one",
        host_id="sample-host",
        connection_id="connection",
        host_generation="connection",
    ).model_dump(mode="json")
    if unresolved:
        source = evidence["sources"][0]
        source["unresolved"] = list(unresolved)
    frame = {
        "kind": "host.context_inventory_v2_result",
        "request_id": "read",
        "connection_id": "connection",
        "host_generation": "connection",
        "read_id": "read-one",
        "session_id": "session-one",
        "evidence": evidence,
        "inventory": None,
    }
    return json.dumps(frame, ensure_ascii=False)


def _duplicate_member(text: str, member: str, value: str, *, occurrence: int) -> str:
    """Repeat one member in raw JSON text before its current value."""
    needle = f'"{member}": "{value}"'
    index = -1
    for _ in range(occurrence):
        index = text.find(needle, index + 1)
    assert index >= 0, needle
    return text[:index] + f'"{member}": "foreign", ' + text[index:]


def test_context_inventory_v2_rejects_duplicate_wire_fields():
    text = _v2_result_wire()
    accepted = decode_host_frame(text)
    assert accepted.evidence is not None

    top_level = _duplicate_member(text, "read_id", "read-one", occurrence=1)
    rejected = decode_host_frame(top_level)
    assert rejected.evidence is None and rejected.inventory is None

    nested = _duplicate_member(text, "read_id", "read-one", occurrence=2)
    assert nested != text
    rejected = decode_host_frame(nested)
    assert rejected.evidence is None and rejected.inventory is None


def test_v1_owner_inventory_wire_stays_lenient_about_repeats():
    snapshot = HostOwnerInventorySnapshot((), (), False, False, "token")
    result = HostOwnerInventoryResultFrame("read", "connection", "connection", snapshot)
    text = encode_host_frame(result)
    repeated = _duplicate_member(text, "request_id", "read", occurrence=1)
    decoded = decode_host_frame(repeated)
    assert isinstance(decoded, HostOwnerInventoryResultFrame)
    assert decoded.inventory is not None


def test_context_inventory_v2_bounds_evidence_whitespace_bytes():
    text = _v2_result_wire()
    marker = '"evidence": {'
    assert marker in text
    inflated = text.replace(marker, marker + " " * (CONTEXT_INVENTORY_MAX_BYTES + 1), 1)
    assert len(inflated) > CONTEXT_INVENTORY_MAX_BYTES
    with pytest.raises(ValueError, match="wire byte budget"):
        decode_host_frame(inflated)


def test_context_inventory_v2_budget_rejects_before_the_frame_is_parsed(monkeypatch):
    text = _v2_result_wire()
    marker = '"evidence": {'
    inflated = text.replace(marker, marker + " " * (CONTEXT_INVENTORY_MAX_BYTES + 1), 1)
    parsed_whole_frame: list[int] = []
    original = json.loads

    def spy(payload, *args, **kwargs):
        if isinstance(payload, str):
            parsed_whole_frame.append(len(payload))
        return original(payload, *args, **kwargs)

    monkeypatch.setattr("omnigent.host.frames.json.loads", spy)
    with pytest.raises(ValueError, match="wire byte budget"):
        decode_host_frame(inflated)
    assert len(text) not in parsed_whole_frame
    assert all(length < 64 for length in parsed_whole_frame)


@pytest.mark.parametrize("escaped", [False, True])
@pytest.mark.parametrize("oversized_first", [False, True])
def test_v2_budget_checks_every_duplicate_evidence_before_parsing(
    monkeypatch, escaped, oversized_first
):
    text = _v2_result_wire()
    key = '"eviden\\u0063e"' if escaped else '"evidence"'
    oversized = '{"padding":"' + "x" * CONTEXT_INVENTORY_MAX_BYTES + '"}'
    copies = (
        f'{key}: {oversized}, "evidence": {{'
        if oversized_first
        else f'"evidence": null, {key}: {{' + " " * CONTEXT_INVENTORY_MAX_BYTES
    )
    text = text.replace('"evidence": {', copies, 1)

    def parse_forbidden(_text):
        pytest.fail("oversized duplicate evidence reached the full-frame parser")

    monkeypatch.setattr("omnigent.host.frames._parse_frame_object", parse_forbidden)
    with pytest.raises(ValueError, match="wire byte budget"):
        decode_host_frame(text)


def test_v2_budget_uses_the_last_kind_before_parsing(monkeypatch):
    text = _v2_result_wire().replace(
        '"kind":', '"kind": "host.owner_inventory_result", "kind":', 1
    )
    text = text.replace('"evidence": {', '"evidence": {' + " " * CONTEXT_INVENTORY_MAX_BYTES, 1)

    def parse_forbidden(_text):
        pytest.fail("last-kind v2 evidence reached the full-frame parser")

    monkeypatch.setattr("omnigent.host.frames._parse_frame_object", parse_forbidden)
    with pytest.raises(ValueError, match="wire byte budget"):
        decode_host_frame(text)


def test_v1_last_kind_keeps_ignoring_oversized_evidence_extensions():
    snapshot = HostOwnerInventorySnapshot((), (), False, False, "token")
    text = encode_host_frame(
        HostOwnerInventoryResultFrame("read", "connection", "connection", snapshot)
    ).replace('"kind":', '"kind": "host.context_inventory_v2_result", "kind":', 1)
    text = text[:-1] + ', "evidence": {"padding":"' + "x" * CONTEXT_INVENTORY_MAX_BYTES + '"}}'
    decoded = decode_host_frame(text)
    assert isinstance(decoded, HostOwnerInventoryResultFrame)
    assert decoded.inventory == snapshot


def _evidence_span_bytes(text: str) -> int:
    """Independent measurement of the raw evidence value's UTF-8 size."""
    start = text.index('"evidence": ') + len('"evidence": ')
    depth = 0
    index = start
    while index < len(text):
        char = text[index]
        if char == '"':
            index += 1
            while text[index] != '"':
                index += 2 if text[index] == "\\" else 1
            index += 1
            continue
        if char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
            if depth == 0:
                return len(text[start : index + 1].encode("utf-8"))
        index += 1
    raise AssertionError("evidence span not found")


def test_context_inventory_v2_budget_holds_the_exact_boundary():
    text = _v2_result_wire()
    marker = '"evidence": {'
    head, tail = text.split(marker, 1)
    padding = CONTEXT_INVENTORY_MAX_BYTES - _evidence_span_bytes(text)
    exact = head + marker + " " * padding + tail
    assert _evidence_span_bytes(exact) == CONTEXT_INVENTORY_MAX_BYTES
    assert decode_host_frame(exact).evidence is not None

    over = head + marker + " " * (padding + 1) + tail
    with pytest.raises(ValueError, match="wire byte budget"):
        decode_host_frame(over)


def test_context_inventory_v2_budget_finds_an_escaped_evidence_key():
    text = _v2_result_wire()
    escaped = text.replace('"evidence":', '"eviden\\u0063e":', 1)
    assert escaped != text
    assert decode_host_frame(escaped).evidence is not None

    marker = '"eviden\\u0063e": {'
    inflated = escaped.replace(marker, marker + " " * (CONTEXT_INVENTORY_MAX_BYTES + 1), 1)
    with pytest.raises(ValueError, match="wire byte budget"):
        decode_host_frame(inflated)


def test_context_inventory_v2_budget_ignores_a_nested_lookalike_member():
    text = _v2_result_wire()
    full = text.replace(
        '"request_id": "read"',
        '"request_id": "read", "note": {"evidence": "' + "é" * 600_000 + '"}',
        1,
    )
    assert len(full.encode("utf-8")) > CONTEXT_INVENTORY_MAX_BYTES * 1.1
    assert decode_host_frame(full).evidence is not None


def test_context_inventory_v2_budget_does_not_count_the_envelope():
    text = _v2_result_wire()
    marker = '"evidence": {'
    near_budget = text.replace(marker, marker + " " * (CONTEXT_INVENTORY_MAX_BYTES - 4096), 1)
    padded = near_budget.replace(
        '"request_id": "read"', '"request_id": "read", "note": "' + "é" * 4000 + '"', 1
    )
    assert len(near_budget) < CONTEXT_INVENTORY_MAX_BYTES
    assert len(padded.encode("utf-8")) > CONTEXT_INVENTORY_MAX_BYTES
    accepted = decode_host_frame(padded)
    assert accepted.evidence is not None


def test_context_inventory_v2_budget_counts_multibyte_evidence_bytes():
    rows = [{"candidate": "é" * 256, "reason": "é" * 256} for _ in range(1000)]
    text = _v2_result_wire(*rows)
    assert len(text) < CONTEXT_INVENTORY_MAX_BYTES
    assert len(text.encode("utf-8")) > CONTEXT_INVENTORY_MAX_BYTES
    with pytest.raises(ValueError, match="wire byte budget"):
        decode_host_frame(text)

    lighter = _v2_result_wire(*rows[:300])
    accepted = decode_host_frame(lighter)
    assert accepted.evidence is not None


def test_context_inventory_v2_budget_fails_closed_on_a_truncated_value():
    text = _v2_result_wire()
    marker = '"evidence": {'
    head, tail = text.split(marker, 1)
    truncated = head + marker + tail[: len(tail) // 2]
    with pytest.raises(ValueError):
        decode_host_frame(truncated)
