"""Tests for resolving the native app-server pid behind launch shims."""

from __future__ import annotations

from types import SimpleNamespace

import psutil
import pytest

from omnigent.inner import _proc


class _FakeProcess:
    """Minimal psutil.Process surface used by native_server_pid."""

    def __init__(
        self,
        pid: int,
        argv: list[str],
        parent_pids: tuple[int, ...] = (),
        *,
        cmdline_error: psutil.Error | None = None,
        parents_error: psutil.Error | None = None,
        children_error: psutil.Error | None = None,
    ) -> None:
        self.pid = pid
        self._argv = argv
        self._parent_pids = parent_pids
        self._cmdline_error = cmdline_error
        self._parents_error = parents_error
        self._children_error = children_error
        self._descendants: list[_FakeProcess] = []

    def cmdline(self) -> list[str]:
        if self._cmdline_error is not None:
            raise self._cmdline_error
        return list(self._argv)

    def parents(self) -> list[SimpleNamespace]:
        if self._parents_error is not None:
            raise self._parents_error
        return [SimpleNamespace(pid=pid) for pid in self._parent_pids]

    def children(self, recursive: bool = False) -> list[_FakeProcess]:
        if self._children_error is not None:
            raise self._children_error
        assert recursive
        return list(self._descendants)


def _install_tree(
    monkeypatch: pytest.MonkeyPatch,
    root: _FakeProcess,
    *descendants: _FakeProcess,
) -> None:
    """Serve *root* (with its descendants) from ``psutil.Process``."""
    root._descendants = list(descendants)
    by_pid = {proc.pid: proc for proc in (root, *descendants)}
    monkeypatch.setattr(_proc.psutil, "Process", by_pid.__getitem__)


def test_native_server_under_node_script(monkeypatch: pytest.MonkeyPatch) -> None:
    """The node shim keeps its script in argv[1]; the native server matches."""
    shim = _FakeProcess(1000, ["node", "/opt/work/bin/codex", "app-server", "-c", "x"])
    native = _FakeProcess(
        2000, ["/opt/work/vendor/codex", "app-server", "-c", "x"], parent_pids=(1000,)
    )
    _install_tree(monkeypatch, shim, native)

    assert _proc.native_server_pid(1000, "app-server") == 2000


def test_native_server_under_cmd_exe(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``cmd /c`` shim carries flags in argv[1]; the native server matches."""
    shim = _FakeProcess(
        1000,
        ["C:\\Windows\\system32\\cmd.exe", "/d", "/s", "/c", "codex.CMD app-server"],
    )
    native = _FakeProcess(
        2000, ["C:\\opt\\work\\vendor\\codex.exe", "app-server"], parent_pids=(1000,)
    )
    _install_tree(monkeypatch, shim, native)

    assert _proc.native_server_pid(1000, "app-server") == 2000


def test_native_server_under_python_liveness_shim(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sandbox liveness shim keeps the script in argv[1]; the native matches."""
    shim = _FakeProcess(
        1000,
        [
            "python",
            "_liveness_exec.py",
            "--liveness-fd",
            "5",
            "/opt/work/codex",
            "app-server",
        ],
    )
    native = _FakeProcess(2000, ["/opt/work/codex", "app-server"], parent_pids=(1000,))
    _install_tree(monkeypatch, shim, native)

    assert _proc.native_server_pid(1000, "app-server") == 2000


def test_spawned_root_is_the_native_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unshimmed spawn is itself the native server."""
    native = _FakeProcess(4321, ["/opt/work/vendor/codex", "app-server"])
    _install_tree(monkeypatch, native)

    assert _proc.native_server_pid(4321, "app-server") == 4321


def test_no_match_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only an exact argv[1] match counts; shims and near misses are ignored."""
    shim = _FakeProcess(1000, ["node", "/opt/work/bin/codex", "app-server"])
    short_argv = _FakeProcess(1001, ["/opt/work/vendor/codex"], parent_pids=(1000,))
    near_miss = _FakeProcess(
        1002, ["/opt/work/vendor/codex", "app-server-extra"], parent_pids=(1000,)
    )
    _install_tree(monkeypatch, shim, short_argv, near_miss)

    assert _proc.native_server_pid(1000, "app-server") is None


def test_two_sibling_innermost_matches_return_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two candidates that are not ancestors of each other are ambiguous."""
    shim = _FakeProcess(1000, ["node", "/opt/work/bin/codex", "app-server"])
    first = _FakeProcess(2000, ["/opt/work/a/codex", "app-server"], parent_pids=(1000,))
    second = _FakeProcess(2001, ["/opt/work/b/codex", "app-server"], parent_pids=(1000,))
    _install_tree(monkeypatch, shim, first, second)

    assert _proc.native_server_pid(1000, "app-server") is None


def test_nested_matches_select_the_innermost(monkeypatch: pytest.MonkeyPatch) -> None:
    """A matching intermediate process is an ancestor, not the answer."""
    shim = _FakeProcess(1000, ["node", "/opt/work/bin/codex", "app-server"])
    outer = _FakeProcess(2000, ["/opt/work/codex", "app-server"], parent_pids=(1000,))
    inner = _FakeProcess(3000, ["/opt/work/vendor/codex", "app-server"], parent_pids=(2000, 1000))
    _install_tree(monkeypatch, shim, outer, inner)

    assert _proc.native_server_pid(1000, "app-server") == 3000


def test_unreadable_cmdline_on_the_native_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """An inaccessible candidate fails the view closed, not a silent skip."""
    shim = _FakeProcess(1000, ["node", "/opt/work/bin/codex", "app-server"])
    native = _FakeProcess(2000, [], parent_pids=(1000,), cmdline_error=psutil.AccessDenied(2000))
    _install_tree(monkeypatch, shim, native)

    assert _proc.native_server_pid(1000, "app-server") is None


def test_inner_match_with_unreadable_cmdline_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreadable inner match must not fall back to the outer launcher."""
    shim = _FakeProcess(1000, ["node", "/opt/work/bin/codex", "app-server"])
    outer = _FakeProcess(2000, ["/opt/work/codex", "app-server"], parent_pids=(1000,))
    inner = _FakeProcess(
        3000, [], parent_pids=(2000, 1000), cmdline_error=psutil.AccessDenied(3000)
    )
    _install_tree(monkeypatch, shim, outer, inner)

    assert _proc.native_server_pid(1000, "app-server") is None


def test_children_access_denied_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreadable descendant list fails closed, even for a matching root."""
    native = _FakeProcess(
        2000,
        ["/opt/work/vendor/codex", "app-server"],
        children_error=psutil.AccessDenied(2000),
    )
    _install_tree(monkeypatch, native)

    assert _proc.native_server_pid(2000, "app-server") is None


def test_unreadable_root_process_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """A root the walk cannot even open fails closed."""

    def _deny(pid: int) -> _FakeProcess:
        raise psutil.AccessDenied(pid)

    monkeypatch.setattr(_proc.psutil, "Process", _deny)

    assert _proc.native_server_pid(1000, "app-server") is None


@pytest.mark.parametrize(
    "vanished_error",
    [psutil.NoSuchProcess(1500), psutil.ZombieProcess(1500)],
)
def test_vanished_sibling_cmdline_fails_closed(
    monkeypatch: pytest.MonkeyPatch, vanished_error: psutil.Error
) -> None:
    """A sibling that exited between walk and cmdline makes the view incomplete."""
    shim = _FakeProcess(1000, ["node", "/opt/work/bin/codex", "app-server"])
    vanished = _FakeProcess(1500, [], parent_pids=(1000,), cmdline_error=vanished_error)
    native = _FakeProcess(2000, ["/opt/work/vendor/codex", "app-server"], parent_pids=(1000,))
    _install_tree(monkeypatch, shim, vanished, native)

    assert _proc.native_server_pid(1000, "app-server") is None


@pytest.mark.parametrize(
    "vanished_error",
    [psutil.NoSuchProcess(3000), psutil.ZombieProcess(3000)],
)
def test_inner_match_with_vanished_cmdline_returns_none(
    monkeypatch: pytest.MonkeyPatch, vanished_error: psutil.Error
) -> None:
    """A vanished inner match must not fall back to the outer launcher."""
    shim = _FakeProcess(1000, ["node", "/opt/work/bin/codex", "app-server"])
    outer = _FakeProcess(2000, ["/opt/work/codex", "app-server"], parent_pids=(1000,))
    inner = _FakeProcess(3000, [], parent_pids=(2000, 1000), cmdline_error=vanished_error)
    _install_tree(monkeypatch, shim, outer, inner)

    assert _proc.native_server_pid(1000, "app-server") is None


def test_unreadable_parents_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """A match whose parent chain cannot be read is unusable."""
    native = _FakeProcess(
        2000, ["/opt/work/vendor/codex", "app-server"], parents_error=psutil.AccessDenied(2000)
    )
    _install_tree(monkeypatch, native)

    assert _proc.native_server_pid(2000, "app-server") is None


@pytest.mark.parametrize("root_pid", [None, 0, 1])
def test_invalid_root_returns_none_without_walking(
    monkeypatch: pytest.MonkeyPatch, root_pid: int | None
) -> None:
    """No pid to anchor the walk means no native server is reported."""

    def _fail_process(_pid: int) -> _FakeProcess:
        raise AssertionError("psutil.Process must not be called")

    monkeypatch.setattr(_proc.psutil, "Process", _fail_process)
    assert _proc.native_server_pid(root_pid, "app-server") is None
