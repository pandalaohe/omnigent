"""Tests for the sanitized Codex rate-limit boundary."""

from __future__ import annotations

import asyncio

import pytest

from omnigent.codex_rate_limits import (
    normalize_codex_rate_limits_response,
    read_codex_rate_limits_snapshot,
    validate_codex_rate_limits_snapshot,
)


def test_normalize_keeps_only_display_windows() -> None:
    snapshot = normalize_codex_rate_limits_response(
        {
            "result": {
                "account": {"email": "must-not-cross@example.com"},
                "credits": {"balance": 123},
                "rateLimitsByLimitId": {
                    "codex": {
                        "limitName": "Codex",
                        "primary": {
                            "usedPercent": 11.4,
                            "windowDurationMins": 300,
                            "resetsAt": 2_000_000_000,
                        },
                        "secondary": {
                            "usedPercent": 6,
                            "windowDurationMins": 10_080,
                        },
                    }
                },
            }
        },
        captured_at=1_900_000_000,
    )

    assert snapshot == {
        "captured_at": 1_900_000_000,
        "limits": [
            {
                "limit_id": "codex",
                "limit_name": "Codex",
                "windows": [
                    {
                        "kind": "primary",
                        "used_percent": 11.4,
                        "window_duration_mins": 300,
                        "resets_at": 2_000_000_000,
                    },
                    {
                        "kind": "secondary",
                        "used_percent": 6.0,
                        "window_duration_mins": 10_080,
                    },
                ],
            }
        ],
    }
    assert "account" not in snapshot
    assert "credits" not in snapshot


def test_normalize_supports_legacy_single_bucket_and_omits_missing_month() -> None:
    snapshot = normalize_codex_rate_limits_response(
        {
            "result": {
                "rateLimits": {
                    "primary": {"usedPercent": 3, "windowDurationMins": 300},
                    "secondary": {"usedPercent": 8, "windowDurationMins": 10_080},
                }
            }
        },
        captured_at=1,
    )

    assert snapshot is not None
    assert snapshot["limits"][0]["limit_id"] == "codex"
    assert len(snapshot["limits"][0]["windows"]) == 2


@pytest.mark.parametrize("used_percent", [-1, 101, True, "5", float("nan"), 10**400])
def test_normalize_rejects_invalid_percentages(used_percent: object) -> None:
    assert (
        normalize_codex_rate_limits_response(
            {
                "result": {
                    "rateLimits": {
                        "primary": {
                            "usedPercent": used_percent,
                            "windowDurationMins": 300,
                        }
                    }
                }
            },
            captured_at=1,
        )
        is None
    )


def test_normalize_filters_unbounded_ids_and_reset_timestamps() -> None:
    snapshot = normalize_codex_rate_limits_response(
        {
            "result": {
                "rateLimitsByLimitId": {
                    "x" * 129: {"primary": {"usedPercent": 1, "windowDurationMins": 300}},
                    " codex ": {
                        "primary": {
                            "usedPercent": 5,
                            "windowDurationMins": 300,
                            "resetsAt": 1 << 80,
                        }
                    },
                }
            }
        },
        captured_at=1,
    )

    assert snapshot == {
        "captured_at": 1,
        "limits": [
            {
                "limit_id": "codex",
                "windows": [
                    {
                        "kind": "primary",
                        "used_percent": 5.0,
                        "window_duration_mins": 300,
                    }
                ],
            }
        ],
    }


def test_normalize_rejects_oversized_legacy_id_and_capture_time() -> None:
    oversized_id_response = {
        "result": {
            "rateLimits": {
                "limitId": "x" * 129,
                "primary": {"usedPercent": 5, "windowDurationMins": 300},
            }
        }
    }
    valid_response = {
        "result": {
            "rateLimits": {
                "limitId": "codex",
                "primary": {"usedPercent": 5, "windowDurationMins": 300},
            }
        }
    }
    assert normalize_codex_rate_limits_response(oversized_id_response, captured_at=1) is None
    assert normalize_codex_rate_limits_response(valid_response, captured_at=1 << 80) is None


def test_wire_validator_rejects_extra_or_malformed_values() -> None:
    with pytest.raises(ValueError, match="snapshot"):
        validate_codex_rate_limits_snapshot({"captured_at": 1, "limits": []})
    with pytest.raises(ValueError, match="window values"):
        validate_codex_rate_limits_snapshot(
            {
                "captured_at": 1,
                "limits": [
                    {
                        "limit_id": "codex",
                        "windows": [
                            {
                                "kind": "primary",
                                "used_percent": 500,
                                "window_duration_mins": 300,
                            }
                        ],
                    }
                ],
            }
        )
    with pytest.raises(ValueError, match="reset timestamp"):
        validate_codex_rate_limits_snapshot(
            {
                "captured_at": 1,
                "limits": [
                    {
                        "limit_id": "codex",
                        "windows": [
                            {
                                "kind": "primary",
                                "used_percent": 5,
                                "window_duration_mins": 300,
                                "resets_at": 1 << 80,
                            }
                        ],
                    }
                ],
            }
        )
    with pytest.raises(ValueError, match="snapshot"):
        validate_codex_rate_limits_snapshot(
            {
                "captured_at": 1 << 80,
                "limits": [
                    {
                        "limit_id": "codex",
                        "windows": [
                            {
                                "kind": "primary",
                                "used_percent": 5,
                                "window_duration_mins": 300,
                            }
                        ],
                    }
                ],
            }
        )


@pytest.mark.parametrize("kind", [[], {}, None, True])
def test_wire_validator_rejects_non_string_window_kind(kind: object) -> None:
    with pytest.raises(ValueError, match="window kind"):
        validate_codex_rate_limits_snapshot(
            {
                "captured_at": 1,
                "limits": [
                    {
                        "limit_id": "codex",
                        "windows": [
                            {
                                "kind": kind,
                                "used_percent": 5,
                                "window_duration_mins": 300,
                            }
                        ],
                    }
                ],
            }
        )


@pytest.mark.asyncio
async def test_probe_teardown_completes_when_cancelled_during_grace_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation during the SIGTERM grace wait still kills and cleans up.

    The child ignores SIGTERM, so ``wait()`` hangs; cancelling the probe
    while it waits must still escalate to ``kill_tree``, close stdin, and
    close the transport before the ``CancelledError`` propagates.
    """
    import omnigent.codex_rate_limits as probe_mod
    from omnigent.inner import _proc

    terminate_called = asyncio.Event()
    kill_called = asyncio.Event()
    killed = asyncio.Event()
    stdin_closed = asyncio.Event()
    transport_closed = asyncio.Event()

    def fake_terminate(process: object) -> None:
        terminate_called.set()

    def fake_kill(process: object) -> None:
        kill_called.set()
        killed.set()

    def fake_close_transport(proc: object) -> None:
        transport_closed.set()

    class _FakeStdin:
        def write(self, data: bytes) -> None:
            pass

        async def drain(self) -> None:
            pass

        def close(self) -> None:
            stdin_closed.set()

        async def wait_closed(self) -> None:
            pass

    class _FakeStdout:
        def __init__(self) -> None:
            self._lines = [
                b'{"id":1,"result":{}}\n',
                b'{"id":2,"result":{"rateLimits":{"primary":'
                b'{"usedPercent":5,"windowDurationMins":300}}}}\n',
            ]

        async def readline(self) -> bytes:
            if self._lines:
                return self._lines.pop(0)
            await asyncio.sleep(3600)
            return b""

    class _FakeProcess:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.pid = 1234567
            self.stdin = _FakeStdin()
            self.stdout = _FakeStdout()

        async def wait(self) -> int:
            if killed.is_set():
                return 0
            await killed.wait()
            return 0

    fake_proc = _FakeProcess()

    async def fake_create(*args: object, **kwargs: object) -> object:
        return fake_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    monkeypatch.setattr(_proc, "terminate_tree", fake_terminate)
    monkeypatch.setattr(_proc, "kill_tree", fake_kill)
    monkeypatch.setattr(probe_mod, "close_subprocess_transport", fake_close_transport)

    task = asyncio.create_task(read_codex_rate_limits_snapshot(codex_path="codex"))
    await asyncio.wait_for(terminate_called.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert terminate_called.is_set()
    assert kill_called.is_set()
    assert stdin_closed.is_set()
    assert transport_closed.is_set()


@pytest.mark.asyncio
async def test_probe_teardown_reraises_cancel_during_stdin_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation during the stdin close wait still cleans up and re-raises.

    The child exits on SIGTERM, so the grace wait succeeds; cancelling the
    probe while it waits for stdin to close must still close the transport
    before the ``CancelledError`` propagates.
    """
    import omnigent.codex_rate_limits as probe_mod
    from omnigent.inner import _proc

    closing_started = asyncio.Event()
    release_close = asyncio.Event()
    transport_closed = asyncio.Event()

    def fake_terminate(process: object) -> None:
        process.returncode = 0  # type: ignore[attr-defined]

    def fake_kill(process: object) -> None:
        raise AssertionError("kill_tree must not run when the child exits cleanly")

    def fake_close_transport(proc: object) -> None:
        transport_closed.set()

    class _FakeStdin:
        def write(self, data: bytes) -> None:
            pass

        async def drain(self) -> None:
            pass

        def close(self) -> None:
            pass

        async def wait_closed(self) -> None:
            closing_started.set()
            await release_close.wait()

    class _FakeStdout:
        def __init__(self) -> None:
            self._lines = [
                b'{"id":1,"result":{}}\n',
                b'{"id":2,"result":{"rateLimits":{"primary":'
                b'{"usedPercent":5,"windowDurationMins":300}}}}\n',
            ]

        async def readline(self) -> bytes:
            if self._lines:
                return self._lines.pop(0)
            await asyncio.sleep(3600)
            return b""

    class _FakeProcess:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.pid = 1234567
            self.stdin = _FakeStdin()
            self.stdout = _FakeStdout()

        async def wait(self) -> int:
            return 0

    fake_proc = _FakeProcess()

    async def fake_create(*args: object, **kwargs: object) -> object:
        return fake_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    monkeypatch.setattr(_proc, "terminate_tree", fake_terminate)
    monkeypatch.setattr(_proc, "kill_tree", fake_kill)
    monkeypatch.setattr(probe_mod, "close_subprocess_transport", fake_close_transport)

    task = asyncio.create_task(read_codex_rate_limits_snapshot(codex_path="codex"))
    await asyncio.wait_for(closing_started.wait(), timeout=5)
    task.cancel()
    release_close.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
    assert transport_closed.is_set()


@pytest.mark.asyncio
async def test_probe_teardown_reraises_cancel_during_post_kill_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation during the post-kill wait still closes pipes and re-raises.

    The grace wait succeeds but the child stays alive, so the probe
    escalates to ``kill_tree``; cancelling while it waits for the killed
    child must still close stdin and the transport before re-raising.
    """
    import omnigent.codex_rate_limits as probe_mod
    from omnigent.inner import _proc

    kill_called = asyncio.Event()
    post_kill_wait_started = asyncio.Event()
    release_post_kill = asyncio.Event()
    stdin_closed = asyncio.Event()
    transport_closed = asyncio.Event()

    def fake_terminate(process: object) -> None:
        pass

    def fake_kill(process: object) -> None:
        kill_called.set()

    def fake_close_transport(proc: object) -> None:
        transport_closed.set()

    class _FakeStdin:
        def write(self, data: bytes) -> None:
            pass

        async def drain(self) -> None:
            pass

        def close(self) -> None:
            stdin_closed.set()

        async def wait_closed(self) -> None:
            pass

    class _FakeStdout:
        def __init__(self) -> None:
            self._lines = [
                b'{"id":1,"result":{}}\n',
                b'{"id":2,"result":{"rateLimits":{"primary":'
                b'{"usedPercent":5,"windowDurationMins":300}}}}\n',
            ]

        async def readline(self) -> bytes:
            if self._lines:
                return self._lines.pop(0)
            await asyncio.sleep(3600)
            return b""

    class _FakeProcess:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.pid = 1234567
            self.stdin = _FakeStdin()
            self.stdout = _FakeStdout()
            self.waits = 0

        async def wait(self) -> int:
            self.waits += 1
            if self.waits == 1:
                return 0
            post_kill_wait_started.set()
            await release_post_kill.wait()
            return 0

    fake_proc = _FakeProcess()

    async def fake_create(*args: object, **kwargs: object) -> object:
        return fake_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    monkeypatch.setattr(_proc, "terminate_tree", fake_terminate)
    monkeypatch.setattr(_proc, "kill_tree", fake_kill)
    monkeypatch.setattr(probe_mod, "close_subprocess_transport", fake_close_transport)

    task = asyncio.create_task(read_codex_rate_limits_snapshot(codex_path="codex"))
    await asyncio.wait_for(post_kill_wait_started.wait(), timeout=5)
    task.cancel()
    release_post_kill.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
    assert kill_called.is_set()
    assert stdin_closed.is_set()
    assert transport_closed.is_set()


@pytest.mark.asyncio
async def test_probe_env_excludes_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """The probe inherits CODEX_HOME but never ambient API credentials."""
    for key in ("OPENAI_API_KEY", "UNRELATED_PROVIDER_SECRET"):
        monkeypatch.setenv(key, "must-not-cross")
    monkeypatch.setenv("CODEX_HOME", "C:/synthetic/codex-home")
    captured: dict[str, object] = {}

    async def capture(*_args: object, **kwargs: object) -> None:
        captured.update(kwargs)
        raise RuntimeError("captured")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    with pytest.raises(RuntimeError, match="captured"):
        await read_codex_rate_limits_snapshot(codex_path="codex.exe")
    env = captured["env"]
    assert isinstance(env, dict) and env["CODEX_HOME"] == "C:/synthetic/codex-home"
    assert "OPENAI_API_KEY" not in env and "UNRELATED_PROVIDER_SECRET" not in env
