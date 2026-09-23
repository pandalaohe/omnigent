"""Tests for crash-safe native Codex process registry reconciliation."""

from __future__ import annotations

import hashlib
import json
import os
import signal
from pathlib import Path

import pytest

from omnigent.harnesses.codex_native import process_registry as registry

fcntl = pytest.importorskip("fcntl")
_REAL_PROCESS_START_IDENTITY = registry._process_start_identity
_REAL_PROCESS_GROUP_MATCHES_ENTRY = registry._process_group_matches_entry


@pytest.fixture(autouse=True)
def _fake_processes_have_no_kernel_identity(monkeypatch) -> None:
    """Keep synthetic PID fixtures independent of processes on the test host."""
    monkeypatch.setattr(registry, "_process_start_identity", lambda _pid: None)
    monkeypatch.setattr(registry, "_process_group_matches_entry", lambda _entry: True)
    # Synthetic groups never actually die, so reconciliation's escalation
    # would otherwise burn the real grace before recording SIGKILL.
    monkeypatch.setattr(registry, "_PROCESS_GROUP_GRACE_S", 0.0, raising=False)
    monkeypatch.setattr(registry, "_ps_output", lambda _columns: "")
    monkeypatch.setattr(
        registry.os,
        "killpg",
        lambda _pgid, _sig: pytest.fail("unexpected real process-group signal"),
    )


def _fake_tagged_groups(monkeypatch, groups: dict[int, tuple[int, str]], killed: list) -> None:
    """Expose a synthetic ps snapshot and remove groups after synthetic KILL."""
    alive = dict(groups)

    def _ps(columns: str) -> str:
        if columns == "pid=,pgid=,uid=,command=":
            return "".join(
                f" {pid} {pgid} {os.getuid()} codex omnigent_crash_teardown_tag={tag}\n"
                for pgid, (pid, tag) in alive.items()
            )
        return ""

    def _killpg(pgid: int, sig: signal.Signals) -> None:
        killed.append((pgid, sig))
        if sig == signal.SIGKILL:
            alive.pop(pgid, None)

    monkeypatch.setattr(registry, "_ps_output", _ps)
    monkeypatch.setattr(registry.os, "killpg", _killpg)


def _registry_payload(path: Path) -> list[dict[str, object]]:
    """
    Return the raw JSON registry payload.

    :param path: Registry file path.
    :returns: Parsed registry entries.
    """
    return json.loads(path.read_text(encoding="utf-8"))


def _reaped_signals(
    killed: list[tuple[int, signal.Signals]],
) -> list[tuple[int, signal.Signals]]:
    """Drop the ``killpg(pgid, 0)`` liveness probes the escalation path issues."""
    return [entry for entry in killed if entry[1] != 0]


def test_registry_add_remove_round_trip(tmp_path: Path) -> None:
    """Registry writes and removes a tagged codex child entry."""
    path = tmp_path / "registry.json"

    registry.register_codex_native_process(
        pid=123,
        pgid=456,
        tmux_session_name="omnigent-codex-123",
        session_tag="tag-123",
        owner_lock_path=tmp_path / "owner.lock",
        registry_path=path,
    )

    assert _registry_payload(path) == [
        {
            "pid": 123,
            "pgid": 456,
            "tmux_session_name": "omnigent-codex-123",
            "session_tag": "tag-123",
            "owner_lock_path": str(tmp_path / "owner.lock"),
        }
    ]

    registry.unregister_codex_native_process("tag-123", registry_path=path)

    assert _registry_payload(path) == []


def test_reconciliation_reaps_alive_tagged_process(tmp_path: Path, monkeypatch) -> None:
    """A live process with the matching cmdline tag is reaped by process group."""
    path = tmp_path / "registry.json"
    registry.register_codex_native_process(
        pid=123,
        pgid=456,
        session_tag="tag-123",
        owner_lock_path=None,
        registry_path=path,
    )
    killed: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(registry, "_pid_alive", lambda pid: pid == 123)
    monkeypatch.setattr(
        registry,
        "_process_cmdline",
        lambda _pid: "codex omnigent_crash_teardown_tag=tag-123 app-server",
    )
    _fake_tagged_groups(monkeypatch, {456: (123, "tag-123")}, killed)

    registry.reconcile_codex_native_process_registry(registry_path=path)

    # The synthetic group never dies, so the TERM is followed by the
    # SIGKILL escalation.
    assert _reaped_signals(killed) == [(456, signal.SIGTERM), (456, signal.SIGKILL)]
    assert _registry_payload(path) == []


def test_reconciliation_skips_pid_reuse_without_matching_tag(tmp_path: Path, monkeypatch) -> None:
    """A reused PID is never killed when the cmdline lacks the session tag."""
    path = tmp_path / "registry.json"
    registry.register_codex_native_process(
        pid=123,
        pgid=456,
        session_tag="tag-123",
        owner_lock_path=None,
        registry_path=path,
    )
    killed: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(registry, "_pid_alive", lambda pid: pid == 123)
    monkeypatch.setattr(registry, "_process_cmdline", lambda _pid: "python unrelated.py")
    monkeypatch.setattr(registry.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))

    registry.reconcile_codex_native_process_registry(registry_path=path)

    assert killed == []
    assert _registry_payload(path) == []


def test_reconciliation_uses_process_start_identity_after_argv0_is_lost(
    tmp_path: Path, monkeypatch
) -> None:
    """A birth identity alone cannot authorize signalling after the tag is lost."""
    path = tmp_path / "registry.json"
    monkeypatch.setattr(registry, "_process_start_identity", lambda _pid: "linux:boot:123")
    registry.register_codex_native_process(
        pid=123,
        pgid=456,
        session_tag="tag-123",
        owner_lock_path=None,
        registry_path=path,
    )
    killed: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(registry, "_process_cmdline", lambda _pid: "codex app-server")
    monkeypatch.setattr(registry.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))

    registry.reconcile_codex_native_process_registry(registry_path=path)

    assert killed == []
    assert _registry_payload(path)[0]["session_tag"] == "tag-123"


def test_reconciliation_skips_reused_pid_with_different_process_start_identity(
    tmp_path: Path, monkeypatch
) -> None:
    """A recycled PID cannot make reconciliation kill the replacement process."""
    path = tmp_path / "registry.json"
    identities = iter(("linux:boot:123", "linux:boot:456"))
    monkeypatch.setattr(registry, "_process_start_identity", lambda _pid: next(identities))
    registry.register_codex_native_process(
        pid=123,
        pgid=456,
        session_tag="tag-123",
        owner_lock_path=None,
        registry_path=path,
    )
    killed: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(registry.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))

    registry.reconcile_codex_native_process_registry(registry_path=path)

    assert killed == []
    assert _registry_payload(path) == []


def test_reconciliation_retains_alive_process_when_identity_is_unreadable(
    tmp_path: Path, monkeypatch
) -> None:
    """A transient identity read failure must not discard a live child."""
    path = tmp_path / "registry.json"
    monkeypatch.setattr(registry, "_process_start_identity", lambda _pid: "linux:boot:123")
    registry.register_codex_native_process(
        pid=123,
        pgid=456,
        session_tag="tag-123",
        owner_lock_path=None,
        registry_path=path,
    )
    monkeypatch.setattr(registry, "_process_start_identity", lambda _pid: None)
    monkeypatch.setattr(registry, "_pid_alive", lambda _pid: True)
    killed: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(registry.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))

    registry.reconcile_codex_native_process_registry(registry_path=path)

    assert killed == []
    assert _registry_payload(path)[0]["process_start_identity"] == "linux:boot:123"


def test_reconciliation_retains_process_when_pgid_changed(tmp_path: Path, monkeypatch) -> None:
    """A matching PID is not signaled through a stale process-group id."""
    path = tmp_path / "registry.json"
    monkeypatch.setattr(registry, "_process_start_identity", lambda _pid: "linux:boot:123")
    registry.register_codex_native_process(
        pid=123,
        pgid=456,
        session_tag="tag-123",
        owner_lock_path=None,
        registry_path=path,
    )
    monkeypatch.setattr(registry, "_process_group_matches_entry", lambda _entry: False)
    killed: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(registry.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))

    registry.reconcile_codex_native_process_registry(registry_path=path)

    assert killed == []
    assert _registry_payload(path)[0]["pgid"] == 456


def test_reconciliation_keeps_entry_when_ps_read_fails(tmp_path: Path, monkeypatch) -> None:
    """An unreadable group is not evidence that its members exited."""
    path = tmp_path / "registry.json"
    registry.register_codex_native_process(
        pid=123,
        pgid=456,
        session_tag="tag-123",
        owner_lock_path=None,
        registry_path=path,
    )
    monkeypatch.setattr(registry, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(registry, "_ps_output", lambda _columns: None)

    assert registry.reconcile_codex_native_process_registry(registry_path=path) == 0
    assert _registry_payload(path)[0]["session_tag"] == "tag-123"


def test_reconciliation_skips_live_sibling_when_owner_lock_is_held(
    tmp_path: Path, monkeypatch
) -> None:
    """A healthy sibling child is not reaped while its launcher owns the lock."""
    path = tmp_path / "registry.json"
    owner_lock = tmp_path / "owner.lock"
    registry.register_codex_native_process(
        pid=123,
        pgid=456,
        session_tag="tag-123",
        owner_lock_path=owner_lock,
        registry_path=path,
    )
    killed: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(registry, "_owner_lock_held", lambda value: value == str(owner_lock))
    monkeypatch.setattr(registry, "_pid_alive", lambda pid: pid == 123)
    monkeypatch.setattr(
        registry,
        "_process_cmdline",
        lambda _pid: "codex omnigent_crash_teardown_tag=tag-123 app-server",
    )
    monkeypatch.setattr(registry.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))

    registry.reconcile_codex_native_process_registry(registry_path=path)

    assert killed == []
    assert _registry_payload(path) == [
        {
            "pid": 123,
            "pgid": 456,
            "tmux_session_name": None,
            "session_tag": "tag-123",
            "owner_lock_path": str(owner_lock),
        }
    ]


def test_reconciliation_reaps_when_owner_lock_is_not_held(tmp_path: Path, monkeypatch) -> None:
    """A tagged child is reaped after its owning launcher lock is gone."""
    path = tmp_path / "registry.json"
    owner_lock = tmp_path / "owner.lock"
    registry.register_codex_native_process(
        pid=123,
        pgid=456,
        session_tag="tag-123",
        owner_lock_path=owner_lock,
        registry_path=path,
    )
    killed: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(registry, "_owner_lock_held", lambda _value: False)
    monkeypatch.setattr(registry, "_pid_alive", lambda pid: pid == 123)
    monkeypatch.setattr(
        registry,
        "_process_cmdline",
        lambda _pid: "codex omnigent_crash_teardown_tag=tag-123 app-server",
    )
    _fake_tagged_groups(monkeypatch, {456: (123, "tag-123")}, killed)

    registry.reconcile_codex_native_process_registry(registry_path=path)

    # The synthetic group never dies, so the TERM is followed by the
    # SIGKILL escalation.
    assert _reaped_signals(killed) == [(456, signal.SIGTERM), (456, signal.SIGKILL)]
    assert _registry_payload(path) == []


def test_reconciliation_drops_dead_pid_with_empty_group(tmp_path: Path, monkeypatch) -> None:
    """A dead recorded pid with no group survivor is discarded without signals."""
    path = tmp_path / "registry.json"
    registry.register_codex_native_process(
        pid=123,
        pgid=456,
        session_tag="tag-123",
        owner_lock_path=None,
        registry_path=path,
    )
    killed: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(registry, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(registry, "_ps_output", lambda _columns: "")
    monkeypatch.setattr(registry.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))

    registry.reconcile_codex_native_process_registry(registry_path=path)

    assert killed == []
    assert _registry_payload(path) == []


def test_reconciliation_reaps_tagged_group_survivor_after_leader_exit(
    tmp_path: Path, monkeypatch
) -> None:
    """A dead recorded pid still reaps a tagged member left in its group."""
    path = tmp_path / "registry.json"
    registry.register_codex_native_process(
        pid=123,
        pgid=456,
        session_tag="tag-123",
        owner_lock_path=None,
        registry_path=path,
    )
    killed: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(registry, "_pid_alive", lambda _pid: False)
    _fake_tagged_groups(monkeypatch, {456: (321, "tag-123")}, killed)

    registry.reconcile_codex_native_process_registry(registry_path=path)

    # The group survivor carries the entry's tag, so the group is TERM'd and
    # escalated (the synthetic group never actually dies).
    assert _reaped_signals(killed) == [(456, signal.SIGTERM), (456, signal.SIGKILL)]
    assert _registry_payload(path) == []


def test_tmux_session_reaped_only_when_recorded_name_exists(tmp_path: Path, monkeypatch) -> None:
    """Matching tagged process reaps only the recorded existing tmux session."""
    path = tmp_path / "registry.json"
    registry.register_codex_native_process(
        pid=123,
        pgid=456,
        tmux_session_name="omnigent-codex-live",
        session_tag="tag-live",
        owner_lock_path=None,
        registry_path=path,
    )
    registry.register_codex_native_process(
        pid=124,
        pgid=457,
        tmux_session_name="omnigent-codex-missing",
        session_tag="tag-missing",
        owner_lock_path=None,
        registry_path=path,
    )
    killed_tmux: list[str] = []
    monkeypatch.setattr(registry, "_pid_alive", lambda pid: pid in {123, 124})
    monkeypatch.setattr(
        registry,
        "_process_cmdline",
        lambda pid: (
            "codex "
            f"omnigent_crash_teardown_tag=tag-{'live' if pid == 123 else 'missing'} "
            "app-server"
        ),
    )
    _fake_tagged_groups(monkeypatch, {456: (123, "tag-live"), 457: (124, "tag-missing")}, [])
    monkeypatch.setattr(
        registry,
        "_tmux_session_exists",
        lambda name: name == "omnigent-codex-live",
    )
    monkeypatch.setattr(registry, "_kill_tmux_session", lambda name: killed_tmux.append(name))

    registry.reconcile_codex_native_process_registry(registry_path=path)

    assert killed_tmux == ["omnigent-codex-live"]
    assert _registry_payload(path) == []


def test_owner_lock_liveness_round_trip(tmp_path: Path, monkeypatch) -> None:
    """A held owner lock reads as held; releasing it makes the entry reapable."""
    monkeypatch.setattr(registry, "_codex_native_state_root", lambda: tmp_path)
    lock = registry.acquire_codex_native_process_owner_lock()
    assert lock is not None
    assert registry._owner_lock_held(str(lock.path)) is True
    lock.close()
    assert registry._owner_lock_held(str(lock.path)) is False


def test_registry_lock_serializes_read_modify_write(tmp_path: Path) -> None:
    """The registry lock is exclusive across the read-modify-write window."""
    path = tmp_path / "registry.json"
    with registry._registry_lock(path):
        fd = registry.os.open(str(path) + ".lock", registry.os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            registry.os.close(fd)


def test_reap_state_dir_kills_matching_app_server_and_spares_others(
    tmp_path: Path, monkeypatch
) -> None:
    """
    Reaping by state dir kills only processes carrying dir + "app-server".

    The stale-holder incident shape: an app-server from a dead runner still
    runs with the session state dir in its command line. A process carrying
    the dir WITHOUT the "app-server" marker (e.g. the pytest process's own
    tree) must survive.
    """
    state_dir = tmp_path / "deadbeefdeadbeefdeadbeefdeadbeef"
    monkeypatch.setattr(
        registry,
        "_ps_output",
        lambda _columns: f" 222 222 node {state_dir} app-server\n 333 333 node {state_dir}\n",
    )
    monkeypatch.setattr(registry, "_pid_alive", lambda _pid: False)
    killed: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(registry.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))

    assert registry.reap_codex_native_processes_for_state_dir(state_dir, grace_s=0) == 1
    assert killed == [(222, signal.SIGTERM)]


def test_reap_state_dir_without_matches_is_a_noop(tmp_path: Path) -> None:
    """A state dir no live process references reaps nothing."""
    assert registry.reap_codex_native_processes_for_state_dir(tmp_path / "no-match") == 0


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("00:05", 5.0),
        ("10:00", 600.0),
        ("1:02:03", 3723.0),
        ("2-11:49:33", 215373.0),
        ("", None),
        ("bogus", None),
    ],
)
def test_ps_elapsed_seconds_parsing(value: str, expected: float | None) -> None:
    """``ps -o etime=`` values parse to seconds, unknown shapes to None."""
    assert registry._parse_ps_elapsed_seconds(value) == expected


def test_orphaned_model_probe_backstop_reaps_only_stale_orphans(monkeypatch) -> None:
    """The registry-independent scan matches only stale, tagged, owned probes."""
    own_uid = os.getuid()
    prefix = registry._model_probe_tag_prefix()
    alive = {222: True}
    table = (
        # Fresh orphan: still inside the probe budget.
        f"  111  1  111  {own_uid}  00:10  node "
        f"omnigent_crash_teardown_tag={prefix}fresh app-server\n"
        # Stale orphan: the only victim.
        f"  222  1  222  {own_uid}  2-11:49:33  node "
        f"omnigent_crash_teardown_tag={prefix}stale app-server\n"
        # Stale but a different tag family.
        f"  333  1  333  {own_uid}  2-11:49:33  node "
        "omnigent_crash_teardown_tag=codex-native-other app-server\n"
        # Stale and tagged, but still parented to a live launcher.
        f"  444  55  444  {own_uid}  2-11:49:33  node "
        f"omnigent_crash_teardown_tag={prefix}parented app-server\n"
        # Stale and tagged, but owned by another user.
        f"  555  1  555  {own_uid + 1}  2-11:49:33  node "
        f"omnigent_crash_teardown_tag={prefix}notmine app-server\n"
        f"  666  1  1  {own_uid}  2-11:49:33  node "
        f"omnigent_crash_teardown_tag={prefix}unsafe app-server\n"
        f"  777  1  777  {own_uid}  2-11:49:33  node "
        f"not_omnigent_crash_teardown_tag={prefix}substring app-server\n"
    )
    killed: list[tuple[int, signal.Signals]] = []

    def _ps(columns: str) -> str:
        if columns == "pid=,ppid=,pgid=,uid=,etime=,command=":
            return table
        return (
            f" 222 222 {own_uid} node omnigent_crash_teardown_tag={prefix}stale\n"
            if alive[222]
            else ""
        )

    def _killpg(pgid: int, sig: signal.Signals) -> None:
        killed.append((pgid, sig))
        if sig == signal.SIGKILL:
            alive[222] = False

    monkeypatch.setattr(registry, "_ps_output", _ps)
    monkeypatch.setattr(registry.os, "killpg", _killpg)

    assert registry.reap_orphaned_codex_model_probes() == 1
    assert killed == [(222, signal.SIGTERM), (222, signal.SIGKILL)]


def test_probe_tag_uses_resolved_state_root(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "state"
    monkeypatch.setattr(registry, "_codex_native_state_root", lambda: root)
    root8 = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:8]
    assert registry.codex_model_probe_session_tag().startswith(f"codex-model-probe-{root8}-")


def test_tagged_group_requires_whole_token_same_uid_and_safe_pgid(monkeypatch) -> None:
    uid = os.getuid()
    own_pgid = os.getpgrp()
    monkeypatch.setattr(
        registry,
        "_ps_output",
        lambda _columns: (
            f" 11 456 {uid} node not_omnigent_crash_teardown_tag=tag-123\n"
            f" 12 456 {uid} node omnigent_crash_teardown_tag=tag-123-extra\n"
            f" 13 456 {uid + 1} node omnigent_crash_teardown_tag=tag-123\n"
            f" 14 456 {uid} node omnigent_crash_teardown_tag=tag-123\n"
            f" 15 1 {uid} node omnigent_crash_teardown_tag=tag-123\n"
            f" 16 {own_pgid} {uid} node omnigent_crash_teardown_tag=tag-123\n"
        ),
    )
    assert registry._tagged_process_group_members(456, "tag-123") == [14]
    assert registry._tagged_process_group_members(1, "tag-123") == []
    assert registry._tagged_process_group_members(own_pgid, "tag-123") == []


def test_reconciliation_refuses_reused_group_before_each_signal(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "registry.json"
    registry.register_codex_native_process(
        pid=123, pgid=456, session_tag="tag-123", owner_lock_path=None, registry_path=path
    )
    monkeypatch.setattr(registry, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(
        registry, "_process_cmdline", lambda _pid: "omnigent_crash_teardown_tag=tag-123"
    )
    snapshots = iter(("tag-123", "unrelated"))

    def _ps(_columns: str) -> str:
        tag = next(snapshots, "unrelated")
        return f" 123 456 {os.getuid()} node omnigent_crash_teardown_tag={tag}\n"

    monkeypatch.setattr(registry, "_ps_output", _ps)
    killed: list[int] = []
    monkeypatch.setattr(registry.os, "killpg", lambda _pgid, sig: killed.append(sig))

    assert registry.reconcile_codex_native_process_registry(registry_path=path) == 1
    assert killed == [signal.SIGTERM]
    assert _registry_payload(path) == []


def test_reconciliation_refuses_reused_group_before_term(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "registry.json"
    registry.register_codex_native_process(
        pid=123, pgid=456, session_tag="tag-123", owner_lock_path=None, registry_path=path
    )
    monkeypatch.setattr(registry, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(
        registry,
        "_ps_output",
        lambda _columns: f" 321 456 {os.getuid()} node omnigent_crash_teardown_tag=unrelated\n",
    )
    killed: list[int] = []
    monkeypatch.setattr(registry.os, "killpg", lambda _pgid, sig: killed.append(sig))

    assert registry.reconcile_codex_native_process_registry(registry_path=path) == 0
    assert killed == []


def test_reconciliation_signal_failure_keeps_entry(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "registry.json"
    registry.register_codex_native_process(
        pid=123, pgid=456, session_tag="tag-123", owner_lock_path=None, registry_path=path
    )
    monkeypatch.setattr(registry, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(
        registry,
        "_ps_output",
        lambda _columns: f" 321 456 {os.getuid()} node omnigent_crash_teardown_tag=tag-123\n",
    )
    monkeypatch.setattr(
        registry.os, "killpg", lambda _pgid, _sig: (_ for _ in ()).throw(PermissionError())
    )

    assert registry.reconcile_codex_native_process_registry(registry_path=path) == 0
    assert _registry_payload(path)[0]["session_tag"] == "tag-123"


def test_backstop_ignores_other_root_and_old_tag(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(registry, "_codex_native_state_root", lambda: tmp_path / "root-a")
    other_root8 = hashlib.sha256(str((tmp_path / "root-b").resolve()).encode()).hexdigest()[:8]
    uid = os.getuid()

    def _ps(columns: str) -> str:
        if columns == "pid=,ppid=,pgid=,uid=,etime=,command=":
            return (
                f" 222 1 222 {uid} 10:00 node "
                f"omnigent_crash_teardown_tag=codex-model-probe-{other_root8}-abcd\n"
                f" 333 1 333 {uid} 10:00 node "
                "omnigent_crash_teardown_tag=codex-model-probe-oldformat\n"
            )
        return (
            f" 222 222 {uid} node "
            f"omnigent_crash_teardown_tag=codex-model-probe-{other_root8}-abcd\n"
            f" 333 333 {uid} node "
            "omnigent_crash_teardown_tag=codex-model-probe-oldformat\n"
        )

    monkeypatch.setattr(
        registry,
        "_ps_output",
        _ps,
    )
    killed: list[int] = []
    monkeypatch.setattr(registry.os, "killpg", lambda _pgid, sig: killed.append(sig))

    assert registry.reap_orphaned_codex_model_probes() == 0
    assert killed == []


def test_backstop_escalates_after_orphan_parent_exits(monkeypatch) -> None:
    prefix = registry._model_probe_tag_prefix()
    uid = os.getuid()
    phase = "parent"
    killed: list[int] = []

    def _ps(columns: str) -> str:
        if columns == "pid=,ppid=,pgid=,uid=,etime=,command=":
            return f" 222 1 456 {uid} 10:00 node omnigent_crash_teardown_tag={prefix}abcd\n"
        if phase == "gone":
            return ""
        pid = 222 if phase == "parent" else 333
        return f" {pid} 456 {uid} node omnigent_crash_teardown_tag={prefix}abcd\n"

    def _killpg(_pgid: int, sig: int) -> None:
        nonlocal phase
        killed.append(sig)
        phase = "child" if sig == signal.SIGTERM else "gone"

    monkeypatch.setattr(registry, "_ps_output", _ps)
    monkeypatch.setattr(registry.os, "killpg", _killpg)
    assert registry.reap_orphaned_codex_model_probes(grace_s=0) == 1
    assert killed == [signal.SIGTERM, signal.SIGKILL]
