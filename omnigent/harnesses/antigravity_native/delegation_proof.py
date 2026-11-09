"""Host-owned, executable-bound evidence for native Antigravity delegation.

The record is an allowance, never inferred from a tool step or a hook receipt.
Bridge files follow the native bridge's existing ownership and cleanup lifetime.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from omnigent.server.permission_tiers import BoundToolCall
from omnigent.util.atomic_json import atomic_write_json

DelegationStatus = Literal["proven", "missing", "failed"]
FINGERPRINT_FILE = "agy_fingerprint"
DISABLED_FILE = "delegation-disabled"


def proof_path() -> Path:
    return Path.home() / ".omnigent" / "agy-delegation-proof.json"


def _read_object(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise ValueError("Refusing symlink delegation state")
    body = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(body, dict):
        raise ValueError("Delegation state must be an object")
    return body


def _digest(body: object) -> str:
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _fingerprint(bridge_dir: Path, executables: Sequence[Path]) -> dict[str, object]:
    from omnigent.harnesses.antigravity_native.bridge import _agy_guard_hook, agy_gemini_dir

    hook = _read_object(agy_gemini_dir(bridge_dir) / "config" / "hooks.json").get("omnigent-guard")
    expected = _agy_guard_hook(bridge_dir)
    if hook != expected:
        raise ValueError("Antigravity guard configuration changed")
    # Normalize argv before JSON encoding, including quoted and Unicode paths.
    normalized_hook = json.loads(json.dumps(expected))
    for phase in ("PreToolUse", "PostToolUse"):
        for matcher in normalized_hook[phase]:
            for command in matcher["hooks"]:
                command["command"] = [
                    argument.replace(str(bridge_dir.absolute()), "<bridge>")
                    for argument in shlex.split(command["command"])
                ]
    normalized = json.dumps(normalized_hook, sort_keys=True)
    payload: dict[str, object] = {
        "version": 1,
        "executables": {
            str(path.resolve(strict=True)): _file_sha256(path) for path in sorted(set(executables))
        },
        "hook_sha256": hashlib.sha256(normalized.encode()).hexdigest(),
        "guard_sha256": _file_sha256(Path(__file__).with_name("guard.py")),
        "adapter_sha256": _file_sha256(Path(__file__)),
    }
    if not payload["executables"]:
        raise ValueError("No Antigravity executable was observed")
    return {**payload, "fingerprint": _digest(payload)}


def prepare_launch(bridge_dir: Path) -> None:
    """Invalidate launch observations without altering the operator's proof."""
    atomic_write_json(
        bridge_dir / FINGERPRINT_FILE,
        {"version": 1, "live": False, "started_at": datetime.now(UTC).isoformat()},
    )


def observe_launch(bridge_dir: Path, port: int) -> None:
    """Bind the fingerprint to the running owner after authenticated discovery."""
    from omnigent.harnesses.antigravity_native import rpc

    previous = _read_object(bridge_dir / FINGERPRINT_FILE)
    executables = rpc.running_agy_executables(port)
    if not executables:
        raise ValueError("Antigravity executable ownership is unavailable")
    current = _fingerprint(bridge_dir, (*executables, Path(sys.executable)))
    atomic_write_json(
        bridge_dir / FINGERPRINT_FILE,
        {**current, "live": True, "started_at": previous["started_at"]},
    )


def delegation_status(bridge_dir: Path) -> DelegationStatus:
    """Missing/stale evidence preserves the old rule; damaged evidence denies."""
    try:
        if (bridge_dir / DISABLED_FILE).exists() or (bridge_dir / DISABLED_FILE).is_symlink():
            return "failed"
        try:
            record = _read_object(proof_path())
        except FileNotFoundError:
            record = None
        if record is not None and record.get("failed") is True:
            return "failed"
        if record is not None and (
            type(record.get("version")) is not int
            or record.get("version") != 1
            or not _sha256(record.get("fingerprint"))
            or not isinstance(record.get("evidence"), str)
            or not record["evidence"].strip()
            or not isinstance(record.get("date"), str)
            or ("failed" in record and type(record["failed"]) is not bool)
        ):
            return "failed"
        if record is not None:
            datetime.fromisoformat(record["date"])
        try:
            launch = _read_object(bridge_dir / FINGERPRINT_FILE)
        except FileNotFoundError:
            return "missing"
        if launch.get("live") is False:
            return "missing"
        if (
            launch.get("live") is not True
            or launch.get("version") != 1
            or any(
                not _sha256(launch.get(key))
                for key in (
                    "fingerprint",
                    "hook_sha256",
                    "guard_sha256",
                    "adapter_sha256",
                )
            )
        ):
            return "failed"
        files = launch.get("executables")
        if (
            not isinstance(files, dict)
            or not files
            or any(
                not isinstance(p, str) or not Path(p).is_absolute() or not _sha256(value)
                for p, value in files.items()
            )
        ):
            return "failed"
        current = _fingerprint(bridge_dir, tuple(Path(p) for p in files))
        if current["fingerprint"] != launch.get("fingerprint"):
            return "missing"
        return (
            "proven"
            if record is not None and current["fingerprint"] == record["fingerprint"]
            else "missing"
        )
    except (OSError, ValueError, TypeError, KeyError):
        return "failed"


def _sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def control_paths(bridge_dir: Path) -> tuple[str, ...]:
    package = Path(__file__).resolve().parents[2]
    paths = [
        proof_path(),
        Path(__file__).parent,
        package / "server" / "permission_tiers.py",
        package / "util" / "atomic_json.py",
        Path(sys.executable).resolve(),
    ]
    try:
        files = _read_object(bridge_dir / FINGERPRINT_FILE).get("executables", {})
        if not isinstance(files, dict) or any(not isinstance(path, str) for path in files):
            raise ValueError("Invalid executable paths")
        paths.extend(Path(path) for path in files)
    except FileNotFoundError:
        pass
    return tuple(str(path) for path in paths)


def hook_receipts(bridge_dir: Path, conversation_id: str) -> list[dict[str, Any]]:
    path = bridge_dir / "hook-seen" / hashlib.sha256(conversation_id.encode()).hexdigest()
    try:
        if path.is_symlink():
            raise ValueError("Refusing symlink hook receipts")
        records = [_read_receipt(line) for line in path.read_text(encoding="utf-8").splitlines()]
        return list({record["call_id"]: record for record in records}.values())
    except FileNotFoundError:
        return []


def _read_receipt(line: str) -> dict[str, Any]:
    record = json.loads(line)
    if (
        not isinstance(record, dict)
        or not isinstance(record.get("name"), str)
        or type(record.get("step")) is not int
        or not isinstance(record.get("call_id"), str)
        or type(record.get("revision")) is not int
    ):
        raise ValueError("Invalid native hook receipt")
    return record


def record_hook(
    bridge_dir: Path,
    payload: Mapping[str, object],
    *,
    session_id: str,
    host_id: str,
    revision: int,
    call_id: str,
    finished: bool = False,
) -> None:
    """Diagnostic receipts contain no arguments and grant no execution authority."""
    cid, step, call = (
        payload.get("conversationId"),
        payload.get("stepIdx"),
        payload.get("toolCall"),
    )
    if (
        not isinstance(cid, str)
        or not cid
        or type(step) is not int
        or step < 0
        or not isinstance(call, dict)
    ):
        raise ValueError("Missing native hook identity")
    name, args = call.get("name"), call.get("args")
    if not isinstance(name, str) or not isinstance(args, dict):
        raise ValueError("Invalid native hook call")
    binding = BoundToolCall.bind(
        session_id=session_id,
        host_id=host_id,
        call_id=call_id,
        name=name,
        arguments=args,
        tier_revision=revision,
    )
    record = {
        "name": name,
        "step": step,
        "call_id": call_id,
        "revision": revision,
        "finished": finished,
        "approval_id": binding.approval_id(),
        "policy_id": "elicit_evaluate_" + call_id.partition(":")[2][:32],
    }
    path = bridge_dir / "hook-seen" / hashlib.sha256(cid.encode()).hexdigest()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, (json.dumps(record) + "\n").encode())
    finally:
        os.close(fd)


def mark_failed(bridge_dir: Path, reason: str) -> None:
    """Fence this launch first, then revoke host-wide delegation evidence."""
    atomic_write_json(bridge_dir / DISABLED_FILE, {"reason": reason})
    path = proof_path()
    try:
        record = _read_object(path)
    except (OSError, ValueError):
        record = {"version": 1}
    atomic_write_json(path, {**record, "failed": True, "failure": reason})


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("record",))
    parser.add_argument("--bridge-dir", type=Path, required=True)
    parser.add_argument("--evidence", required=True)
    args = parser.parse_args(argv)
    from omnigent.harnesses.antigravity_native.bridge import read_bridge_state
    from omnigent.harnesses.antigravity_native.rpc import resolve_language_server_port
    from omnigent.permission_tiers.host import (
        HOST_PERMISSION_STATE_FILE,
        read_host_permission_state,
    )

    if not args.evidence.strip():
        parser.error("Nonempty evidence from a passing proof run is required")
    bridge_dir = args.bridge_dir.absolute()
    if (bridge_dir / DISABLED_FILE).is_symlink():
        raise ValueError("Refusing symlink delegation fence")
    state = read_host_permission_state(bridge_dir / HOST_PERMISSION_STATE_FILE)
    native = read_bridge_state(bridge_dir)
    if (
        state.tier == "full"
        or state.status != "ready"
        or native is None
        or native.session_id != state.session_id
    ):
        parser.error("A ready restricted native session is required")
    port = resolve_language_server_port(native.conversation_id)
    if port is None:
        parser.error("Cannot verify the running Antigravity owner")
    observe_launch(bridge_dir, port)
    launch = _read_object(bridge_dir / FINGERPRINT_FILE)
    atomic_write_json(
        proof_path(),
        {
            "version": 1,
            "fingerprint": launch["fingerprint"],
            "date": datetime.now(UTC).isoformat(),
            "evidence": args.evidence,
            "failed": False,
        },
    )
    # An operator-authorized successful re-proof is the only operation lifting this fence.
    marker = bridge_dir / DISABLED_FILE
    if marker.is_symlink():
        raise ValueError("Refusing symlink delegation fence")
    marker.unlink(missing_ok=True)
    print("Antigravity delegation proof recorded for the running fingerprint")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
