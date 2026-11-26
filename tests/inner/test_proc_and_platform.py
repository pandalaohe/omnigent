"""Tests for the cross-platform process/platform primitives and Windows backend.

Covers omnigent._platform, omnigent.inner._proc, the harness IPC endpoint
abstraction, and the windows_jobobject sandbox backend. The platform-specific
assertions are gated with ``posix_only`` / ``windows_only`` markers so the
file runs on both Linux CI and a Windows box.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import Mock

import psutil
import pytest

from omnigent import _platform
from omnigent.inner import _proc


def _spin_cmd() -> list[str]:
    """A short-lived child process that does nothing but sleep."""
    if os.name == "nt":
        return ["cmd", "/c", "ping -n 30 127.0.0.1 >NUL"]
    return ["sleep", "30"]


# --------------------------------------------------------------------------
# _platform
# --------------------------------------------------------------------------


def test_platform_flags_are_mutually_consistent() -> None:
    assert (os.name == "nt") == _platform.IS_WINDOWS
    assert (os.name == "posix") == _platform.IS_POSIX
    # Exactly one OS family is true.
    assert _platform.IS_WINDOWS != _platform.IS_POSIX


def test_default_shell_argv_runs_an_echo() -> None:
    argv = _platform.default_shell_argv("echo omnigent-shell-ok")
    out = subprocess.run(argv, capture_output=True, text=True, check=True)
    assert "omnigent-shell-ok" in out.stdout


@pytest.mark.posix_only
def test_default_interactive_shell_honors_known_shell_on_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``$SHELL`` naming a known shell that resolves on PATH is kept.

    Returns the basename (not the ``$SHELL`` path) so it stays resolvable
    when the terminal launches under a runner on a different host.
    """
    # Patch shutil.which on the shared module so the function's local
    # ``import shutil`` sees the fake; make every known shell "installed".
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    for shell_path, expected in (
        ("/bin/zsh", "zsh"),
        ("/usr/local/bin/fish", "fish"),
        ("/bin/bash", "bash"),
    ):
        monkeypatch.setenv("SHELL", shell_path)
        assert _platform.default_interactive_shell() == expected


@pytest.mark.posix_only
def test_default_interactive_shell_falls_back_to_bash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset / unknown / uninstalled ``$SHELL`` all fall back to bash."""
    # Resolution is fully hermetic: neither PATH nor the standard shell dirs
    # yield anything, so only $SHELL's own absolute path could rescue a value.
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr(_platform, "_resolve_interactive_shell", lambda name: None)
    monkeypatch.setattr(os.path, "isabs", lambda path: False)
    # Unset $SHELL → bash.
    monkeypatch.delenv("SHELL", raising=False)
    assert _platform.default_interactive_shell() == "bash"
    # An unknown shell name is never honored, even if it resolved.
    monkeypatch.setenv("SHELL", "/opt/weird/nushell")
    assert _platform.default_interactive_shell() == "bash"
    # A known shell that resolves nowhere (not on PATH, not in a standard dir,
    # $SHELL not usable as an absolute path) also falls back.
    monkeypatch.setenv("SHELL", "/bin/zsh")
    assert _platform.default_interactive_shell() == "bash"


def test_normalize_interactive_shells_filters_and_deduplicates() -> None:
    """Host and server accept only supported shell basenames."""
    assert _platform.normalize_interactive_shells(["zsh", "bash", "zsh", "not-a-shell", 42]) == [
        "zsh",
        "bash",
    ]
    assert _platform.normalize_interactive_shells("bash") == []


@pytest.mark.posix_only
def test_default_interactive_shell_trusts_shell_absolute_path_off_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A known ``$SHELL`` whose absolute path is executable is honored even
    when it does not resolve on a stripped ``PATH``.

    Reproduces the frozen-PATH host daemon (GUI/launchd launch): ``$SHELL`` is
    ``/bin/zsh`` but ``PATH`` omits ``/bin``, so a bare ``shutil.which("zsh")``
    misses it. The login shell must still be honored.
    """
    zsh = tmp_path / "zsh"
    zsh.write_text("#!/bin/sh\n")
    zsh.chmod(0o755)
    # Nothing resolves on PATH or in the standard shell dirs.
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr(_platform, "_INTERACTIVE_SHELL_DIRS", ())
    monkeypatch.setenv("SHELL", str(zsh))
    assert _platform.default_interactive_shell() == "zsh"


@pytest.mark.posix_only
def test_resolve_interactive_shell_uses_matching_absolute_login_shell(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A Nix-style login shell remains executable under a stripped PATH."""
    fish = tmp_path / "profiles" / "default" / "bin" / "fish"
    fish.parent.mkdir(parents=True)
    fish.write_text("#!/bin/sh\n")
    fish.chmod(0o755)
    monkeypatch.setenv("SHELL", str(fish))
    monkeypatch.setattr("shutil.which", lambda _name: None)
    monkeypatch.setattr(_platform, "_INTERACTIVE_SHELL_DIRS", ())

    assert _platform.default_interactive_shell() == "fish"
    assert _platform._resolve_interactive_shell("fish") == str(fish)


@pytest.mark.parametrize("name", ["/bin/bash", "../bash", "unknown"])
def test_resolve_interactive_shell_rejects_paths_and_unknown_names(
    name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The resolver accepts allowlisted basenames only."""
    monkeypatch.setattr("shutil.which", lambda value: pytest.fail(f"resolved {value}"))
    assert _platform._resolve_interactive_shell(name) is None


def _fake_resolver(installed: set[str]):
    """A ``_resolve_interactive_shell`` stand-in: resolve only *installed*.

    Fully hermetic — the real helper probes both PATH and standard shell dirs,
    so patching it (not just ``shutil.which``) is what keeps a test from leaking
    whatever shells the host machine actually has installed.
    """
    return lambda name: f"/usr/bin/{name}" if name in installed else None


@pytest.mark.posix_only
def test_installed_interactive_shells_lists_default_first_then_alternatives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default ($SHELL) leads; installed alternatives follow, deduped."""
    monkeypatch.setattr(
        _platform, "_resolve_interactive_shell", _fake_resolver({"bash", "zsh", "fish"})
    )
    monkeypatch.setenv("SHELL", "/bin/zsh")
    # zsh is the default → first; bash/fish follow in offer order; no dupes.
    assert _platform.installed_interactive_shells() == ["zsh", "bash", "fish"]


@pytest.mark.posix_only
def test_installed_interactive_shells_skips_uninstalled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Alternatives that resolve nowhere are omitted."""
    # Only bash and zsh installed; fish absent.
    monkeypatch.setattr(_platform, "_resolve_interactive_shell", _fake_resolver({"bash", "zsh"}))
    monkeypatch.setenv("SHELL", "/bin/bash")
    assert _platform.installed_interactive_shells() == ["bash", "zsh"]


@pytest.mark.posix_only
def test_installed_interactive_shells_offers_shells_off_stripped_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shells in a standard dir are offered even when ``PATH`` omits it.

    Regression for the frozen-PATH host daemon: with ``SHELL=/bin/zsh`` but a
    ``PATH`` missing ``/bin``, ``shutil.which`` finds nothing, yet the standard
    shell dirs still resolve zsh/bash — so the picker must offer both instead of
    collapsing to a lone (phantom) ``bash``.
    """
    monkeypatch.setattr("shutil.which", lambda name: None)  # nothing on PATH

    # Both live in /bin, off the stripped PATH.
    def in_bin(path: str) -> bool:
        return os.path.basename(path) in {"zsh", "bash"} and path.startswith("/bin/")

    monkeypatch.setattr("os.path.isfile", in_bin)
    monkeypatch.setattr("os.access", lambda p, mode: True)
    monkeypatch.setenv("SHELL", "/bin/zsh")
    assert _platform.installed_interactive_shells() == ["zsh", "bash"]


@pytest.mark.posix_only
def test_installed_interactive_shells_always_nonempty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With nothing resolvable anywhere the bash fallback is still offered alone."""
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr(_platform, "_resolve_interactive_shell", lambda name: None)
    monkeypatch.setattr(os.path, "isabs", lambda path: False)
    monkeypatch.delenv("SHELL", raising=False)
    assert _platform.installed_interactive_shells() == ["bash"]


# --------------------------------------------------------------------------
# _platform: Windows shell inventory and interactive argv (TS36)
# --------------------------------------------------------------------------

_WINDOWS_SHELLS = ("pwsh", "powershell", "cmd")


def _which_mapping(**paths: str):
    """A ``shutil.which`` stand-in resolving only the explicitly mapped names."""
    return lambda name: paths.get(name)


def test_normalize_interactive_shells_accepts_windows_shell_names() -> None:
    """Host/server normalization accepts a Windows host's native inventory."""
    assert _platform.normalize_interactive_shells(
        ["pwsh", "powershell", "cmd", "pwsh", "not-a-shell"]
    ) == ["pwsh", "powershell", "cmd"]


@pytest.mark.parametrize(
    ("standalone", "expected"),
    [(True, ["-l"]), (False, [])],
)
def test_interactive_shell_args_preserves_posix_argv(
    standalone: bool, expected: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """TS36: a POSIX standalone host shell logs in; session shells take no args."""
    monkeypatch.setattr(_platform, "IS_WINDOWS", False)
    assert _platform.interactive_shell_args("bash", standalone=standalone) == expected


@pytest.mark.parametrize("name", ["pwsh", "powershell"])
@pytest.mark.parametrize("standalone", [True, False])
def test_interactive_shell_args_suppresses_windows_powershell_banner(
    name: str, standalone: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows PowerShell skips its logo in both standalone and session modes."""
    monkeypatch.setattr(_platform, "IS_WINDOWS", True)
    assert _platform.interactive_shell_args(name, standalone=standalone) == ["-NoLogo"]


@pytest.mark.parametrize("name", ["cmd", "bash"])
@pytest.mark.parametrize("standalone", [True, False])
def test_interactive_shell_args_adds_nothing_for_other_windows_shells(
    name: str, standalone: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cmd (and any non-PowerShell name) takes no extra argv on Windows."""
    monkeypatch.setattr(_platform, "IS_WINDOWS", True)
    assert _platform.interactive_shell_args(name, standalone=standalone) == []


def test_installed_interactive_shells_windows_orders_native_shells(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pwsh, then powershell, then cmd — never bash/WSL; ``$SHELL`` ignored."""
    monkeypatch.setattr(_platform, "IS_WINDOWS", True)
    monkeypatch.setattr(
        "shutil.which",
        _which_mapping(
            pwsh=r"C:\Program Files\PowerShell\7\pwsh.exe",
            powershell=r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
            cmd=r"C:\Windows\System32\cmd.exe",
            bash=r"C:\Program Files\Git\bin\bash.exe",
            wsl=r"C:\Windows\System32\wsl.exe",
        ),
    )
    monkeypatch.setenv("SHELL", "/bin/zsh")
    assert _platform.installed_interactive_shells() == list(_WINDOWS_SHELLS)
    assert _platform.default_interactive_shell() == "pwsh"


def test_installed_interactive_shells_windows_skips_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only resolvable native shells appear, in canonical order."""
    monkeypatch.setattr(_platform, "IS_WINDOWS", True)
    monkeypatch.setattr(
        "shutil.which",
        _which_mapping(
            powershell=r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
            cmd=r"C:\Windows\System32\cmd.exe",
        ),
    )
    assert _platform.installed_interactive_shells() == ["powershell", "cmd"]
    assert _platform.default_interactive_shell() == "powershell"


@pytest.mark.parametrize("resolved", [r"C:\tools\pwsh.exe", r"C:\tools\pwsh.COM"])
def test_windows_shell_inventory_accepts_native_executables(
    resolved: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_platform, "IS_WINDOWS", True)
    monkeypatch.setattr("shutil.which", _which_mapping(pwsh=resolved))
    assert _platform.installed_interactive_shells() == ["pwsh"]
    assert _platform._resolve_interactive_shell("pwsh") == resolved


@pytest.mark.parametrize("resolved", [r"C:\tools\pwsh.cmd", r"C:\tools\pwsh.bat"])
def test_windows_shell_inventory_rejects_non_native_shims(
    resolved: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """npm-style ``.cmd``/``.bat`` shims are not spawnable shells."""
    monkeypatch.setattr(_platform, "IS_WINDOWS", True)
    monkeypatch.setattr("shutil.which", _which_mapping(pwsh=resolved))
    assert _platform.installed_interactive_shells() == []
    assert _platform._resolve_interactive_shell("pwsh") is None


def test_windows_shell_inventory_ignores_bash_and_wsl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_platform, "IS_WINDOWS", True)
    monkeypatch.setattr(
        "shutil.which",
        _which_mapping(
            bash=r"C:\Program Files\Git\bin\bash.exe",
            wsl=r"C:\Windows\System32\wsl.exe",
            cmd=r"C:\Windows\System32\cmd.exe",
        ),
    )
    assert _platform.installed_interactive_shells() == ["cmd"]
    assert _platform._resolve_interactive_shell("bash") is None


def test_windows_shell_inventory_is_empty_without_native_shell(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No native shell resolves: empty inventory, no phantom bash; dirs/$SHELL ignored."""
    stale = tmp_path / "pwsh.exe"
    stale.write_text("")
    monkeypatch.setattr(_platform, "IS_WINDOWS", True)
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr(_platform, "_INTERACTIVE_SHELL_DIRS", (str(tmp_path),))
    monkeypatch.setenv("SHELL", str(stale))
    assert _platform.installed_interactive_shells() == []


@pytest.mark.posix_only
@pytest.mark.parametrize("name", ["pwsh", "powershell", "cmd"])
def test_windows_shell_names_do_not_resolve_as_posix_login_shells(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adding Windows names to the allowlist must not make them POSIX shells."""
    monkeypatch.setattr("shutil.which", lambda value: f"/usr/local/bin/{value}")
    assert _platform._resolve_interactive_shell(name) is None
    monkeypatch.setenv("SHELL", f"/usr/local/bin/{name}")
    assert _platform.default_interactive_shell() == "bash"


@pytest.mark.posix_only
def test_posix_inventory_never_offers_windows_shells(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda value: f"/usr/bin/{value}")
    monkeypatch.setenv("SHELL", "/bin/bash")
    inventory = _platform.installed_interactive_shells()
    assert inventory[0] == "bash"
    assert not set(inventory) & set(_WINDOWS_SHELLS)


def test_stable_user_id_is_stable_and_path_safe() -> None:
    uid = _platform.stable_user_id()
    assert uid == _platform.stable_user_id()
    assert uid and not set(uid) & set("/\\: ")


@pytest.mark.windows_only
def test_resolve_repo_symlink_dereferences_git_stub(tmp_path: Path) -> None:
    # Real target the Git symlink points at.
    target = tmp_path / "examples" / "polly"
    target.mkdir(parents=True)
    (target / "config.yaml").write_text("name: polly\n", encoding="utf-8")
    # Stub file Git leaves on a no-symlink Windows checkout: content is the
    # relative link target, no trailing newline.
    stub = tmp_path / "resources" / "polly"
    stub.parent.mkdir(parents=True)
    stub.write_text("../examples/polly", encoding="utf-8")

    resolved = _platform.resolve_repo_symlink(stub)
    assert resolved == target.resolve()


@pytest.mark.windows_only
def test_resolve_repo_symlink_leaves_real_specs_untouched(tmp_path: Path) -> None:
    # A genuine single-file YAML spec must not be mistaken for a symlink stub.
    spec = tmp_path / "agent.yaml"
    spec.write_text("name: hello\nharness: claude-sdk\n", encoding="utf-8")
    assert _platform.resolve_repo_symlink(spec) == spec
    # A real directory is returned unchanged.
    d = tmp_path / "bundle"
    d.mkdir()
    assert _platform.resolve_repo_symlink(d) == d


# --------------------------------------------------------------------------
# _proc
# --------------------------------------------------------------------------


def test_spawn_kwargs_shape_matches_platform() -> None:
    kw = _proc.spawn_kwargs()
    if os.name == "nt":
        assert kw == {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        }
    else:
        assert kw == {"start_new_session": True}


def test_windows_spawn_and_no_window_kwargs_with_platform_fakes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    no_window = 0x08000000
    new_group = 0x00000200
    monkeypatch.setattr(_proc, "IS_POSIX", True)
    assert _proc.windows_no_window_flags() == 0
    assert _proc.no_window_kwargs() == {}
    assert _proc.spawn_kwargs() == {"start_new_session": True}

    monkeypatch.setattr(_proc, "IS_POSIX", False)
    monkeypatch.setattr(_proc, "_CREATE_NEW_PROCESS_GROUP", new_group)
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", no_window, raising=False)
    assert _proc.windows_no_window_flags() == no_window
    assert _proc.no_window_kwargs() == {"creationflags": no_window}
    assert _proc.spawn_kwargs() == {"creationflags": new_group | no_window}


def test_windows_taskkill_path_requires_absolute_system_dir_or_systemroot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_proc, "_windows_system_directory", lambda: r"D:\WinOS\System32")
    monkeypatch.setenv("SYSTEMROOT", r"C:\Ignored")
    assert _proc._windows_taskkill_path() == r"D:\WinOS\System32\taskkill.exe"

    monkeypatch.setattr(_proc, "_windows_system_directory", lambda: None)
    monkeypatch.setenv("SYSTEMROOT", r"D:\WinOS")
    assert _proc._windows_taskkill_path() == r"D:\WinOS\System32\taskkill.exe"

    for invalid in (
        "Windows",
        r"\Windows",
        r"D:Windows",
        r"\\host\share\Windows",
        r"\\?\UNC\host\share\Windows",
        r"9:\Windows",
        "",
        "   ",
    ):
        monkeypatch.setenv("SYSTEMROOT", invalid)
        assert _proc._windows_taskkill_path() is None

    monkeypatch.delenv("SYSTEMROOT", raising=False)
    assert _proc._windows_taskkill_path() is None


def test_kill_tree_windows_skips_taskkill_when_psutil_reaps_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeProc:
        def __init__(self, pid: int, create_time: float) -> None:
            self.pid = pid
            self._create_time = create_time
            self.alive = True

        def create_time(self) -> float:
            return self._create_time

        def status(self) -> str:
            if not self.alive:
                raise psutil.NoSuchProcess(self.pid)
            return psutil.STATUS_RUNNING

        def children(self, *, recursive: bool = False) -> list[_FakeProc]:
            return []

        def kill(self) -> None:
            self.alive = False

        def wait(self, timeout: float | None = None) -> int:
            return 0

    fake_root = _FakeProc(50100, 10.0)
    monkeypatch.setattr(_proc, "IS_POSIX", False)
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(_proc.psutil, "Process", lambda pid: fake_root)
    run_mock = Mock()
    monkeypatch.setattr(_proc.subprocess, "run", run_mock)

    handle = Mock(pid=50100, returncode=None)
    _proc.kill_tree(handle)

    assert not fake_root.alive
    run_mock.assert_not_called()


def test_kill_tree_windows_falls_back_to_taskkill_for_surviving_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _StubbornProc:
        def __init__(
            self,
            pid: int,
            create_time: float,
            children: list[_StubbornProc] | None = None,
        ) -> None:
            self.pid = pid
            self._create_time = create_time
            self._children = children or []
            self.alive = True

        def create_time(self) -> float:
            return self._create_time

        def status(self) -> str:
            if not self.alive:
                raise psutil.NoSuchProcess(self.pid)
            return psutil.STATUS_RUNNING

        def children(self, *, recursive: bool = False) -> list[_StubbornProc]:
            return list(self._children)

        def kill(self) -> None:
            # Simulate a process that psutil fails to terminate cleanly.
            pass

        def wait(self, timeout: float | None = None) -> int:
            raise psutil.TimeoutExpired(timeout or 0.0, self.pid)

    child = _StubbornProc(50202, 20.0)
    root = _StubbornProc(50201, 10.0, children=[child])
    table = {root.pid: root, child.pid: child}

    def _lookup(pid: int) -> _StubbornProc:
        proc = table[pid]
        if not proc.alive:
            raise psutil.NoSuchProcess(pid)
        return proc

    taskkill_calls: list[tuple[list[str], dict[str, object]]] = []

    def _fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        taskkill_calls.append((argv, kwargs))
        # /T on the root clears both root and child, so child needs no second call.
        if argv[-1] == str(root.pid):
            root.alive = False
            child.alive = False
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(_proc, "IS_POSIX", False)
    monkeypatch.setattr(_proc, "_WINDOWS_REAP_SETTLE_SECONDS", 0.001)
    monkeypatch.setattr(_proc, "_windows_system_directory", lambda: None)
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setenv("SYSTEMROOT", r"C:\Windows")
    monkeypatch.setattr(_proc.psutil, "Process", _lookup)
    monkeypatch.setattr(_proc.subprocess, "run", _fake_run)

    handle = Mock(pid=root.pid, returncode=None)
    _proc.kill_tree(handle)

    assert len(taskkill_calls) == 1
    argv, kwargs = taskkill_calls[0]
    assert argv == [r"C:\Windows\System32\taskkill.exe", "/T", "/F", "/PID", "50201"]
    assert kwargs["creationflags"] == 0x08000000
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.DEVNULL


def test_kill_tree_windows_taskkills_surviving_descendant_after_root_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Node:
        def __init__(
            self,
            pid: int,
            create_time: float,
            *,
            dies_on_kill: bool,
            children: list[_Node] | None = None,
        ) -> None:
            self.pid = pid
            self._create_time = create_time
            self._dies_on_kill = dies_on_kill
            self._children = children or []
            self.alive = True

        def create_time(self) -> float:
            return self._create_time

        def status(self) -> str:
            if not self.alive:
                raise psutil.NoSuchProcess(self.pid)
            return psutil.STATUS_RUNNING

        def children(self, *, recursive: bool = False) -> list[_Node]:
            return list(self._children)

        def kill(self) -> None:
            if self._dies_on_kill:
                self.alive = False

        def wait(self, timeout: float | None = None) -> int:
            if self.alive:
                raise psutil.TimeoutExpired(timeout or 0.0, self.pid)
            return 0

    survivor = _Node(50302, 22.0, dies_on_kill=False)
    root = _Node(50301, 11.0, dies_on_kill=True, children=[survivor])
    table = {root.pid: root, survivor.pid: survivor}

    def _lookup(pid: int) -> _Node:
        proc = table[pid]
        if not proc.alive:
            raise psutil.NoSuchProcess(pid)
        return proc

    taskkill_pids: list[str] = []

    def _fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        taskkill_pids.append(argv[-1])
        survivor.alive = False
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(_proc, "IS_POSIX", False)
    monkeypatch.setattr(_proc, "_WINDOWS_REAP_SETTLE_SECONDS", 0.001)
    monkeypatch.setattr(_proc, "_windows_system_directory", lambda: None)
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setenv("SYSTEMROOT", r"C:\Windows")
    monkeypatch.setattr(_proc.psutil, "Process", _lookup)
    monkeypatch.setattr(_proc.subprocess, "run", _fake_run)

    _proc.kill_tree(Mock(pid=root.pid, returncode=None))

    assert taskkill_pids == ["50302"]


def test_kill_tree_windows_skips_taskkill_on_access_denied_and_retains_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Unreadable root must never trigger a raw-PID taskkill.
    def _deny(pid: int) -> psutil.Process:
        raise psutil.AccessDenied(pid)

    run_mock = Mock()
    monkeypatch.setattr(_proc, "IS_POSIX", False)
    monkeypatch.setattr(_proc, "_WINDOWS_REAP_SETTLE_SECONDS", 0.001)
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setenv("SYSTEMROOT", r"C:\Windows")
    monkeypatch.setattr(_proc.psutil, "Process", _deny)
    monkeypatch.setattr(_proc.subprocess, "run", run_mock)

    denied_root = Mock(pid=50401, returncode=None)
    _proc.kill_tree(denied_root)
    denied_root.kill.assert_called_once_with()
    run_mock.assert_not_called()

    # A previously observed descendant that becomes AccessDenied is skipped by
    # taskkill and retained in _owned_process_identities for later retry.
    handle = Mock(pid=50402, returncode=0)
    _proc._owned_process_identities[handle] = {50403: 55.0}
    _proc.kill_tree(handle)
    run_mock.assert_not_called()
    assert _proc.has_unresolved_identities(handle)
    assert _proc._owned_process_identities.get(handle) == {50403: 55.0}

    # An OSError during final survivor inspection is swallowed and retains identities.
    handle_err = Mock(pid=50404, returncode=0)
    _proc._owned_process_identities[handle_err] = {50405: 66.0}
    monkeypatch.setattr(_proc.psutil, "Process", Mock(side_effect=OSError("probe failed")))
    _proc.kill_tree(handle_err)
    run_mock.assert_not_called()
    assert _proc._owned_process_identities.get(handle_err) == {50405: 66.0}


def test_kill_tree_windows_skips_unpinned_target_and_enforces_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @contextlib.contextmanager
    def _unpinned(_pid: int):
        yield False

    survivor = Mock(pid=50421)
    survivor.create_time.return_value = 33.0
    survivor.status.return_value = psutil.STATUS_RUNNING
    survivor.children.return_value = []
    survivor.wait.side_effect = psutil.TimeoutExpired(0.001, 50421)

    run_mock = Mock()
    monkeypatch.setattr(_proc, "IS_POSIX", False)
    monkeypatch.setattr(_proc, "_WINDOWS_REAP_SETTLE_SECONDS", 0.001)
    monkeypatch.setattr(_proc, "_windows_system_directory", lambda: None)
    monkeypatch.setenv("SYSTEMROOT", r"C:\Windows")
    monkeypatch.setattr(_proc.psutil, "Process", lambda pid: survivor)
    monkeypatch.setattr(_proc.subprocess, "run", run_mock)
    monkeypatch.setattr(_proc, "_pin_windows_process", _unpinned)

    handle = Mock(pid=50421, returncode=None)
    _proc.kill_tree(handle)
    run_mock.assert_not_called()
    assert _proc.has_unresolved_identities(handle)


def test_kill_tree_windows_skips_pid_reused_during_escalation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Node:
        def __init__(
            self,
            pid: int,
            create_time: float,
            children: list[_Node] | None = None,
        ) -> None:
            self.pid = pid
            self._create_time = create_time
            self._children = children or []
            self.alive = True

        def create_time(self) -> float:
            return self._create_time

        def status(self) -> str:
            if not self.alive:
                raise psutil.NoSuchProcess(self.pid)
            return psutil.STATUS_RUNNING

        def children(self, *, recursive: bool = False) -> list[_Node]:
            return list(self._children)

        def kill(self) -> None:
            pass

        def wait(self, timeout: float | None = None) -> int:
            raise psutil.TimeoutExpired(timeout or 0.0, self.pid)

    child = _Node(50412, 20.0)
    root = _Node(50411, 10.0, children=[child])
    table = {root.pid: root, child.pid: child}

    def _lookup(pid: int) -> _Node:
        proc = table[pid]
        if not proc.alive:
            raise psutil.NoSuchProcess(pid)
        return proc

    taskkill_pids: list[str] = []

    def _fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        taskkill_pids.append(argv[-1])
        root.alive = False
        # Simulate child exiting and its PID being recycled before its own turn.
        child._create_time = 999.0
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(_proc, "IS_POSIX", False)
    monkeypatch.setattr(_proc, "_WINDOWS_REAP_SETTLE_SECONDS", 0.001)
    monkeypatch.setattr(_proc, "_windows_system_directory", lambda: None)
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setenv("SYSTEMROOT", r"C:\Windows")
    monkeypatch.setattr(_proc.psutil, "Process", _lookup)
    monkeypatch.setattr(_proc.subprocess, "run", _fake_run)

    _proc.kill_tree(Mock(pid=root.pid, returncode=None))

    assert taskkill_pids == ["50411"]


def test_kill_tree_windows_never_taskkills_reused_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Reaped:
        pid = 50501
        returncode = 0

        def kill(self) -> None:
            raise AssertionError("must not kill a reused PID")

    reused = Mock(pid=50501)
    reused.create_time.return_value = 999.0
    reused.status.return_value = psutil.STATUS_RUNNING
    reused.children.return_value = []
    process = _Reaped()
    _proc._owned_process_identities[process] = {50501: 100.0}
    monkeypatch.setattr(_proc, "IS_POSIX", False)
    monkeypatch.setattr(_proc.psutil, "Process", lambda pid: reused)
    run_mock = Mock()
    monkeypatch.setattr(_proc.subprocess, "run", run_mock)

    _proc.kill_tree(process)

    run_mock.assert_not_called()
    reused.kill.assert_not_called()


def test_process_alive_is_a_nondestructive_probe() -> None:
    proc = subprocess.Popen(_spin_cmd(), **_proc.spawn_kwargs())
    # Bind to the live PID so psutil pins its creation time; a recycled PID
    # after teardown can't masquerade as the reaped child, so the final
    # liveness check is free of the process_alive(pid) TOCTOU race.
    handle = psutil.Process(proc.pid)
    try:
        assert _proc.process_alive(proc.pid) is True
        # Probing repeatedly must NOT kill the process (the os.kill(pid, 0)
        # bug on Windows would terminate it here).
        for _ in range(3):
            assert _proc.process_alive(proc.pid) is True
    finally:
        _proc.kill_tree(proc)
        proc.wait(timeout=5)
    # A reaped PID raises NoSuchProcess, which also means it isn't running.
    with contextlib.suppress(psutil.NoSuchProcess):
        assert not handle.is_running() or handle.status() == psutil.STATUS_ZOMBIE


def test_process_alive_false_for_bogus_pid() -> None:
    assert _proc.process_alive(2_000_000_000) is False
    assert _proc.process_alive(-1) is False


@pytest.mark.posix_only
@pytest.mark.parametrize("exit_code", [0, 7])
def test_pipe_supervisor_preserves_command_io_and_status(tmp_path: Path, exit_code: int) -> None:
    """Both output pipes can exceed capacity without deadlocking the supervisor."""
    script = (
        "import os, sys; "
        "data = sys.stdin.buffer.read(); "
        "sys.stdout.buffer.write(data + b'o' * 200000); "
        "sys.stderr.buffer.write(b'e' * 200000); "
        "print(os.getcwd(), sys.argv[1], os.environ['PROBE_TEST_VALUE'], flush=True); "
        "sys.exit(int(sys.argv[2]))"
    )
    literal_arg = "spaces; $literal `argument`"
    result = subprocess.run(
        _proc.pipe_supervised_command(sys.executable, "-c", script, literal_arg, str(exit_code)),
        input=b"probe input\n",
        capture_output=True,
        cwd=tmp_path,
        env={**os.environ, "PROBE_TEST_VALUE": "inherited"},
        timeout=5,
        check=False,
        **_proc.spawn_kwargs(),
    )
    assert result.returncode == exit_code
    assert result.stdout == (
        b"probe input\n" + b"o" * 200000 + f"{tmp_path} {literal_arg} inherited\n".encode()
    )
    assert result.stderr == b"e" * 200000


@pytest.mark.posix_only
def test_pipe_supervisor_reports_missing_command(tmp_path: Path) -> None:
    missing = str(tmp_path / "missing-command")
    result = subprocess.run(
        _proc.pipe_supervised_command(missing), capture_output=True, timeout=5, check=False
    )
    assert result.returncode == 127
    assert missing.encode() in result.stderr


def test_pipe_supervisor_keeps_windows_direct_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_proc, "IS_POSIX", False)
    assert _proc.pipe_supervised_command("claude", "-p", "/model") == ("claude", "-p", "/model")


def test_process_create_time_is_stable_for_a_live_process() -> None:
    proc = subprocess.Popen(_spin_cmd(), **_proc.spawn_kwargs())
    try:
        identity = _proc.process_create_time(proc.pid)
        assert identity is not None and identity > 0
        assert _proc.process_create_time(proc.pid) == identity
    finally:
        _proc.kill_tree(proc)
        proc.wait(timeout=5)


def test_process_create_time_is_none_for_a_bogus_pid() -> None:
    assert _proc.process_create_time(2_000_000_000) is None


def test_process_create_time_swallows_access_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    def _deny(pid: int) -> psutil.Process:
        raise psutil.AccessDenied(pid)

    monkeypatch.setattr(_proc.psutil, "Process", _deny)
    assert _proc.process_create_time(1234) is None


def test_killpg_refuses_the_broadcast_group(monkeypatch: pytest.MonkeyPatch) -> None:
    """A target pgid of 1 must never be signalled.

    ``killpg(1, sig)`` is ``kill(-1, sig)`` -- a broadcast to every process
    the user can signal (a mocked pid that coerces to 1 killed the CI runner).
    """
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(_proc, "_getpgid_fn", lambda pid: 1 if pid else 54321)
    monkeypatch.setattr(_proc, "_killpg_fn", lambda pgid, sig: sent.append((pgid, sig)))
    assert _proc._killpg(1, signal.SIGTERM) is False
    assert sent == []


def test_identity_census_skips_reused_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    reused = Mock()
    reused.create_time.return_value = 200.0
    monkeypatch.setattr(_proc.psutil, "Process", lambda pid: reused)

    _proc._signal_identities({43210: 100.0}, force=True)

    reused.kill.assert_not_called()


@pytest.mark.parametrize(("create_time", "expected"), [(100.0, True), (200.0, False)])
def test_owned_zombie_leader_keeps_group_identity(
    monkeypatch: pytest.MonkeyPatch, create_time: float, expected: bool
) -> None:
    """An unreaped leader reserves its group even when getpgid cannot find it."""
    leader = Mock()
    leader.create_time.return_value = create_time
    leader.status.return_value = psutil.STATUS_ZOMBIE
    monkeypatch.setattr(_proc.psutil, "Process", lambda pid: leader)
    monkeypatch.setattr(_proc, "_getpgid_fn", Mock(side_effect=ProcessLookupError))

    assert _proc._identity_in_group({43210: 100.0}, 43210) is expected


@pytest.mark.parametrize("group_support", [True, False])
def test_kill_tree_does_not_adopt_reused_reaped_leader_pid(
    monkeypatch: pytest.MonkeyPatch,
    group_support: bool,
) -> None:
    class _ReapedProcess:
        pid = 43210
        returncode = 0

        def kill(self) -> None:
            raise AssertionError("must not kill a reused PID")

    process = _ReapedProcess()
    reused = Mock(pid=process.pid)
    reused.create_time.return_value = 200.0
    reused.children.return_value = []
    sent: list[tuple[int, int]] = []
    if group_support:
        _proc._owned_process_groups[process] = process.pid
    _proc._owned_process_identities[process] = {process.pid: 100.0}
    monkeypatch.setattr(_proc.psutil, "Process", lambda pid: reused)
    monkeypatch.setattr(
        _proc,
        "_getpgid_fn",
        lambda pid: 99999 if pid == 0 else process.pid,
    )
    monkeypatch.setattr(
        _proc,
        "_killpg_fn",
        lambda pgid, sig: sent.append((pgid, sig)),
    )

    _proc.kill_tree(process)

    assert sent == []
    reused.kill.assert_not_called()


def test_terminate_tree_stops_the_process() -> None:
    proc = subprocess.Popen(_spin_cmd(), **_proc.spawn_kwargs())
    # Bind to the live PID so psutil pins its creation time; a recycled PID
    # after teardown can't masquerade as the reaped child, so the final
    # liveness check is free of the process_alive(pid) TOCTOU race.
    handle = psutil.Process(proc.pid)
    _proc.terminate_tree(proc, grace=5)
    proc.wait(timeout=5)
    # A reaped PID raises NoSuchProcess, which also means it isn't running.
    with contextlib.suppress(psutil.NoSuchProcess):
        assert not handle.is_running() or handle.status() == psutil.STATUS_ZOMBIE


@pytest.mark.skipif(os.name != "posix", reason="setsid is POSIX-only")
@pytest.mark.parametrize("snapshot", [False, True])
def test_kill_tree_reaps_descendant_that_left_process_group(
    tmp_path: Path, snapshot: bool
) -> None:
    child_pid_path = tmp_path / "setsid-child.pid"
    script = (
        "import os, pathlib, time\n"
        "path = pathlib.Path(os.environ['CHILD_PID_PATH'])\n"
        "if os.fork() == 0:\n"
        "    os.setsid()\n"
        # Publish atomically: the poll below must never read a created-but-empty file.
        "    pending = path.with_suffix('.pending')\n"
        "    pending.write_text(str(os.getpid()))\n"
        "    pending.replace(path)\n"
        "    while True: time.sleep(1)\n"
        "while True: time.sleep(1)\n"
    )
    env = {**os.environ, "CHILD_PID_PATH": str(child_pid_path)}
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        env=env,
        **_proc.spawn_kwargs(),
    )
    _proc.remember_process_group(proc)
    child_pid: int | None = None
    try:
        deadline = time.monotonic() + 5
        while not child_pid_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert child_pid_path.exists()
        child_pid = int(child_pid_path.read_text())

        if snapshot:
            identities = _proc.snapshot_process_tree(
                proc.pid, _proc._process_start_stamp(psutil.Process(proc.pid))
            )
            assert child_pid in identities
            proc.kill()
            proc.wait(timeout=5)
            assert _proc.process_alive(child_pid)
            _proc._signal_identities(identities, force=False)
            _proc._signal_identities(identities, force=True)
        else:
            _proc.kill_tree(proc)
            proc.wait(timeout=5)

        deadline = time.monotonic() + 5
        while _proc.process_alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not _proc.process_alive(child_pid)
        if snapshot:
            assert not _proc.surviving_process_snapshot(identities)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        if child_pid is not None and _proc.process_alive(child_pid):
            with contextlib.suppress(psutil.Error):
                psutil.Process(child_pid).kill()


@pytest.mark.skipif(os.name != "posix", reason="requires a POSIX shell")
def test_run_isolated_timeout_kills_the_whole_tree(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "child.pid"
    # The direct child forks a long-lived grandchild, then hangs past the timeout.
    script = f"sleep 300 & echo $! > '{child_pid_path}'; exec sleep 300"
    child_pid: int | None = None
    try:
        # A 2s timeout keeps the grandchild's pid write ahead of the kill even
        # on a loaded machine, so this does not race the process teardown.
        with pytest.raises(subprocess.TimeoutExpired):
            _proc.run_isolated(["sh", "-c", script], timeout=2, capture_output=True, text=True)
        assert child_pid_path.exists()
        child_pid = int(child_pid_path.read_text())
        deadline = time.monotonic() + 5
        while _proc.process_alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not _proc.process_alive(child_pid)
    finally:
        if child_pid is not None and _proc.process_alive(child_pid):
            with contextlib.suppress(psutil.Error):
                psutil.Process(child_pid).kill()


@pytest.mark.skipif(os.name != "posix", reason="requires a POSIX shell")
@pytest.mark.parametrize("drain_fails", [False, True], ids=["drained", "drain-error"])
def test_run_isolated_kills_the_tree_on_non_timeout_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drain_fails: bool
) -> None:
    # A non-timeout failure from communicate() must still tear down the tree and
    # propagate the original exception, not just the timeout path.
    child_pid_path = tmp_path / "child.pid"
    script = f"sleep 300 & echo $! > '{child_pid_path}'; exec sleep 300"

    real_communicate = subprocess.Popen.communicate
    state = {"raised": False}

    def flaky_communicate(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if not state["raised"]:
            # Let the grandchild come up so there is a real tree to tear down.
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if child_pid_path.exists() and child_pid_path.read_text().strip():
                    break
                time.sleep(0.02)
            state["raised"] = True
            raise RuntimeError("boom")
        if drain_fails:
            raise RuntimeError("cleanup drain failed")
        return real_communicate(self, *args, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "communicate", flaky_communicate)

    child_pid: int | None = None
    try:
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="boom"):
            _proc.run_isolated(["sh", "-c", script], timeout=30, capture_output=True, text=True)
        assert time.monotonic() - started < 10
        assert child_pid_path.exists()
        child_pid = int(child_pid_path.read_text())
        deadline = time.monotonic() + 5
        while _proc.process_alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not _proc.process_alive(child_pid)
    finally:
        if child_pid is not None and _proc.process_alive(child_pid):
            with contextlib.suppress(psutil.Error):
                psutil.Process(child_pid).kill()


@pytest.mark.skipif(os.name != "posix", reason="requires a POSIX shell")
def test_run_isolated_timeout_kills_a_same_group_child_after_the_leader_exits(
    tmp_path: Path,
) -> None:
    # The leader exits right away, but a same-group child keeps the stdout/stderr
    # pipes open so communicate() blocks until the timeout; cleanup must still
    # reach that orphaned child through the remembered process group.
    child_pid_path = tmp_path / "child.pid"
    script = f"sleep 300 & echo $! > '{child_pid_path}'"
    child_pid: int | None = None
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            _proc.run_isolated(["sh", "-c", script], timeout=2, capture_output=True, text=True)
        assert child_pid_path.exists()
        child_pid = int(child_pid_path.read_text())
        deadline = time.monotonic() + 5
        while _proc.process_alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not _proc.process_alive(child_pid)
    finally:
        if child_pid is not None and _proc.process_alive(child_pid):
            with contextlib.suppress(psutil.Error):
                psutil.Process(child_pid).kill()


# POSIX interrupts a genuinely hung waitpid() with SIGALRM for a clean per-test
# failure; Windows has no SIGALRM and skips these tests anyway, so fall back to
# the default thread timer there to avoid crashing pytest-timeout's setup.
_HANG_GUARD = pytest.mark.timeout(30, method="signal" if os.name == "posix" else "thread")


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process semantics")
@_HANG_GUARD
def test_run_isolated_timeout_kills_the_leader_when_group_teardown_is_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # If kill_tree cannot reach the leader (a denied killpg, say), the bounded
    # reap also times out, so run_isolated must still kill the leader directly;
    # otherwise Popen.__exit__'s unbounded wait() hangs past the probe timeout.
    seen: dict[str, int] = {}

    def noop_kill_tree(process: object) -> None:
        pid = getattr(process, "pid", None)
        if isinstance(pid, int):
            seen["pid"] = pid

    monkeypatch.setattr(_proc, "kill_tree", noop_kill_tree)

    leader_pid: int | None = None
    try:
        start = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            _proc.run_isolated(["sleep", "300"], timeout=1, capture_output=True, text=True)
        # Without the direct kill, __exit__ would wait on the live leader for the
        # whole sleep; finishing in seconds proves the fallback bounded teardown.
        assert time.monotonic() - start < 15
        assert "pid" in seen
        leader_pid = seen["pid"]
        deadline = time.monotonic() + 5
        while _proc.process_alive(leader_pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not _proc.process_alive(leader_pid)
    finally:
        if leader_pid is not None and _proc.process_alive(leader_pid):
            with contextlib.suppress(psutil.Error):
                psutil.Process(leader_pid).kill()


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process semantics")
@_HANG_GUARD
def test_run_isolated_kills_the_leader_even_if_kill_tree_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # kill_tree can raise an unexpected error from its psutil descendant walk;
    # the teardown must still run so the live leader is killed and __exit__'s
    # unbounded wait() cannot hang. The original kill_tree error propagates.
    seen: dict[str, int] = {}

    def boom_kill_tree(process: object) -> None:
        pid = getattr(process, "pid", None)
        if isinstance(pid, int):
            seen["pid"] = pid
        raise OSError("descendant walk failed")

    monkeypatch.setattr(_proc, "kill_tree", boom_kill_tree)

    leader_pid: int | None = None
    try:
        start = time.monotonic()
        with pytest.raises(OSError, match="descendant walk failed"):
            _proc.run_isolated(["sleep", "300"], timeout=1, capture_output=True, text=True)
        assert time.monotonic() - start < 15
        assert "pid" in seen
        leader_pid = seen["pid"]
        deadline = time.monotonic() + 5
        while _proc.process_alive(leader_pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not _proc.process_alive(leader_pid)
    finally:
        if leader_pid is not None and _proc.process_alive(leader_pid):
            with contextlib.suppress(psutil.Error):
                psutil.Process(leader_pid).kill()


def test_run_isolated_returns_the_completed_process() -> None:
    completed = _proc.run_isolated(
        [sys.executable, "-c", "import sys; print('out'); sys.exit(3)"],
        timeout=30,
        capture_output=True,
        text=True,
    )
    assert (completed.returncode, completed.stdout.strip()) == (3, "out")


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc/stat btime is Linux-only")
@pytest.mark.parametrize("step", [-1.0, 1.0])
def test_kill_tree_survives_a_wall_clock_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: float
) -> None:
    """An NTP/hypervisor clock step moves btime; kill_tree must still reap the tree."""
    import psutil._pslinux as pslinux

    child_pid_path = tmp_path / "setsid-child.pid"
    script = (
        "import os, pathlib, time\n"
        "path = pathlib.Path(os.environ['CHILD_PID_PATH'])\n"
        "if os.fork() == 0:\n"
        "    os.setsid()\n"
        "    pending = path.with_suffix('.pending')\n"
        "    pending.write_text(str(os.getpid()))\n"
        "    pending.replace(path)\n"
        "    while True: time.sleep(1)\n"
        "while True: time.sleep(1)\n"
    )
    env = {**os.environ, "CHILD_PID_PATH": str(child_pid_path)}
    proc = subprocess.Popen([sys.executable, "-c", script], env=env, **_proc.spawn_kwargs())
    _proc.remember_process_group(proc)
    child: psutil.Process | None = None
    try:
        identity = _proc.process_create_time(proc.pid)
        deadline = time.monotonic() + 10
        while not child_pid_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        child = psutil.Process(int(child_pid_path.read_text()))
        _proc.refresh_process_tree(proc)
        real_boot_time = pslinux.boot_time
        monkeypatch.setattr(pslinux, "boot_time", lambda: real_boot_time() + step)
        assert _proc.process_create_time(proc.pid) == identity

        _proc.kill_tree(proc)

        proc.wait(timeout=5)
        child.wait(timeout=5)
    finally:
        monkeypatch.undo()
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        if child is not None:
            with contextlib.suppress(psutil.Error):
                child.kill()


# --------------------------------------------------------------------------
# Harness IPC endpoint abstraction
# --------------------------------------------------------------------------


def test_endpoint_uds_variant_shape() -> None:
    from omnigent.runtime.harnesses.process_manager import _HarnessEndpoint

    ep = _HarnessEndpoint(socket_path=Path("/tmp/x/conv.sock"))
    assert ep.is_uds is True
    assert ep.spawn_args() == ["--socket", str(Path("/tmp/x/conv.sock"))]
    assert ep.base_url == "http://harness.local"


@pytest.mark.parametrize(
    "host, authority", [("127.0.0.1", "127.0.0.1:54321"), ("::1", "[::1]:54321")]
)
def test_endpoint_tcp_variant_shape(host: str, authority: str) -> None:
    from omnigent.runtime.harnesses.process_manager import _HarnessEndpoint

    ep = _HarnessEndpoint(host=host, port=54321)
    assert ep.is_uds is False
    assert ep.spawn_args() == ["--bind", authority]
    assert ep.base_url == f"http://{authority}"


def test_endpoint_create_picks_platform_transport() -> None:
    from omnigent.runtime.harnesses.process_manager import _HarnessEndpoint

    ep = _HarnessEndpoint.create(Path("/tmp/inst"), "conv_x")
    assert ep.is_uds == (os.name != "nt")


# --------------------------------------------------------------------------
# windows_jobobject backend
# --------------------------------------------------------------------------


@pytest.mark.windows_only
def test_windows_jobobject_is_platform_default() -> None:
    from omnigent.inner import sandbox

    assert sandbox._default_sandbox_for_platform().type == "windows_jobobject"


@pytest.mark.windows_only
def test_windows_jobobject_kill_on_close_terminates_tree() -> None:
    from omnigent.inner.sandbox import SandboxPolicy
    from omnigent.inner.windows_jobobject_sandbox import WindowsJobObjectSandboxBackend

    backend = WindowsJobObjectSandboxBackend()
    policy = SandboxPolicy(
        backend_type="windows_jobobject",
        active=True,
        read_roots=None,
        write_roots=[],
        write_files=[],
        allow_network=True,
    )
    proc = subprocess.Popen(_spin_cmd())
    handle = backend.post_spawn(policy, proc.pid)
    assert handle is not None
    assert _proc.process_alive(proc.pid) is True
    # Closing the job handle must terminate the contained process.
    handle.close()
    time.sleep(0.5)
    assert _proc.process_alive(proc.pid) is False
    if _proc.process_alive(proc.pid):
        proc.kill()


@pytest.mark.windows_only
def test_explicit_bwrap_errors_loudly_on_windows() -> None:
    from omnigent.inner import sandbox
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

    sandbox._ensure_builtin_backends()
    backend = sandbox.get_backend("linux_bwrap")
    with pytest.raises(OSError):
        backend.resolve(OSEnvSpec(sandbox=OSEnvSandboxSpec(type="linux_bwrap")), Path("."))


@pytest.mark.posix_only
def test_posix_default_sandbox_is_not_jobobject() -> None:
    from omnigent.inner import sandbox

    assert sandbox._default_sandbox_for_platform().type in {"linux_bwrap", "darwin_seatbelt"}


@pytest.mark.windows_only
def test_helper_env_keeps_systemroot_so_child_can_import_asyncio() -> None:
    # Regression: a filtered (active-sandbox) helper env that drops SYSTEMROOT
    # makes any spawned `python -m omnigent...` die at `import asyncio` with
    # WinError 10106 (Winsock loads providers from %SystemRoot%). The os_env
    # allowlist must carry the Windows system vars.
    from omnigent.inner.os_env import build_helper_env
    from omnigent.inner.sandbox import SandboxPolicy

    policy = SandboxPolicy(
        backend_type="windows_jobobject",
        active=True,
        read_roots=None,
        write_roots=[],
        write_files=[],
        allow_network=True,
    )
    env = build_helper_env(os.environ, policy)
    assert "SYSTEMROOT" in env
    result = subprocess.run(
        [__import__("sys").executable, "-c", "import asyncio"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("env_filter", ["agent", "runner", "helper"])
def test_windows_program_paths_survive_env_filters(env_filter: str) -> None:
    from omnigent.host.connect import _build_runner_env
    from omnigent.inner.agent_env import clean_agent_env
    from omnigent.inner.os_env import build_helper_env
    from omnigent.inner.sandbox import SandboxPolicy

    source = {
        "PATH": r"C:\Windows\System32",
        "SYSTEMROOT": r"C:\Windows",
        "PROGRAMDATA": r"C:\ProgramData",
        "PROGRAMFILES": r"C:\Program Files",
        "PROGRAMFILES(X86)": r"C:\Program Files (x86)",
        "PROGRAMW6432": r"C:\Program Files",
        "SOME_SECRET_TOKEN": "x",
    }
    if env_filter == "agent":
        env = clean_agent_env(source=source)
    elif env_filter == "runner":
        env = _build_runner_env(
            base_env=source,
            server_url="http://x",
            runner_id="r1",
            binding_token="t",
            workspace=".",
            parent_pid=os.getpid(),
        )
    else:
        policy = SandboxPolicy(
            backend_type="none",
            active=True,
            read_roots=None,
            write_roots=[],
            write_files=[],
            allow_network=True,
        )
        env = build_helper_env(source, policy)

    for name in ("PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432"):
        assert env[name] == source[name]
    assert "SOME_SECRET_TOKEN" not in env


@pytest.mark.windows_only
def test_parent_death_watchdog_does_not_false_fire_on_windows() -> None:
    # Regression: on Windows os.getppid() does not match the spawner (the venv
    # launcher breaks the parent link), so the getppid-based orphan check
    # reported the runner orphaned the instant it started and tore it down
    # (clean exit 0). With a live parent_pid the runner must NOT be orphaned.
    from omnigent.runner._entry import _parent_is_orphaned

    assert _parent_is_orphaned(os.getpid()) is False
    assert _parent_is_orphaned(2_000_000_000) is True


@pytest.mark.windows_only
def test_host_runner_env_lets_child_import_asyncio_and_resolve_home() -> None:
    # Regression: the host->runner env allowlist dropped SYSTEMROOT (asyncio /
    # WinError 10106) and USERPROFILE (Path.home() -> "Could not determine home
    # directory"). Both must pass through so a spawned runner can boot.
    import sys

    from omnigent.host.connect import _build_runner_env

    env = _build_runner_env(
        base_env=os.environ,
        server_url="http://x",
        runner_id="r1",
        binding_token="t",
        workspace=".",
        parent_pid=os.getpid(),
    )
    assert "SYSTEMROOT" in env
    assert "USERPROFILE" in env
    result = subprocess.run(
        [sys.executable, "-c", "import asyncio; from pathlib import Path; Path.home()"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# resolve_cli_binary — CLIs that may live off the daemon's frozen PATH
# ---------------------------------------------------------------------------


def test_resolve_cli_binary_prefers_path(monkeypatch):
    monkeypatch.delenv("OMNIGENT_TESTCLI_PATH", raising=False)
    monkeypatch.setattr(
        _platform.shutil, "which", lambda name: "/usr/bin/tool" if name == "tool" else None
    )
    monkeypatch.setattr(_platform, "_cli_fallback_dirs", lambda: ())
    assert _platform.resolve_cli_binary("tool", env_var="OMNIGENT_TESTCLI_PATH") == "/usr/bin/tool"


def test_resolve_cli_binary_env_override_wins(monkeypatch, tmp_path):
    override = tmp_path / "tool"
    override.write_text("#!/bin/sh\n")
    override.chmod(0o755)
    monkeypatch.setenv("OMNIGENT_TESTCLI_PATH", str(override))
    # PATH would resolve elsewhere, but the override takes precedence.
    monkeypatch.setattr(
        _platform.shutil, "which", lambda name: "/usr/bin/tool" if name == "tool" else None
    )
    assert _platform.resolve_cli_binary("tool", env_var="OMNIGENT_TESTCLI_PATH") == str(override)


def test_resolve_cli_binary_falls_back_to_global_dir(monkeypatch, tmp_path):
    """A binary off PATH is still found in a common global install dir.

    This is the nvm case: the host daemon's frozen PATH omits the bin dir,
    but the binary is present on disk.
    """
    fallback_dir = tmp_path / "bin"
    fallback_dir.mkdir()
    tool = fallback_dir / "tool"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    monkeypatch.delenv("OMNIGENT_TESTCLI_PATH", raising=False)
    monkeypatch.setattr(_platform.shutil, "which", lambda name: None)
    monkeypatch.setattr(_platform, "_cli_fallback_dirs", lambda: (fallback_dir,))
    assert _platform.resolve_cli_binary("tool", env_var="OMNIGENT_TESTCLI_PATH") == str(tool)


def test_resolve_cli_binary_returns_none_when_absent(monkeypatch, tmp_path):
    monkeypatch.delenv("OMNIGENT_TESTCLI_PATH", raising=False)
    monkeypatch.setattr(_platform.shutil, "which", lambda name: None)
    monkeypatch.setattr(_platform, "_cli_fallback_dirs", lambda: (tmp_path / "empty",))
    assert _platform.resolve_cli_binary("tool", env_var="OMNIGENT_TESTCLI_PATH") is None


def test_resolve_cli_binary_warns_on_bad_override(monkeypatch, tmp_path, caplog):
    """A set-but-unresolvable override warns (so a misconfig surfaces) and then
    falls back to PATH rather than launching nothing."""
    monkeypatch.setenv("OMNIGENT_TESTCLI_PATH", str(tmp_path / "does-not-exist"))
    monkeypatch.setattr(
        _platform.shutil, "which", lambda name: "/usr/bin/tool" if name == "tool" else None
    )
    monkeypatch.setattr(_platform, "_cli_fallback_dirs", lambda: ())
    with caplog.at_level("WARNING"):
        resolved = _platform.resolve_cli_binary("tool", env_var="OMNIGENT_TESTCLI_PATH")
    assert resolved == "/usr/bin/tool"
    assert "OMNIGENT_TESTCLI_PATH" in caplog.text


def test_resolve_cli_binary_no_env_var(monkeypatch):
    """Without an env_var, resolution is PATH then fallback dirs only."""
    monkeypatch.setattr(_platform.shutil, "which", lambda name: "/usr/bin/tool")
    assert _platform.resolve_cli_binary("tool") == "/usr/bin/tool"


def test_cli_fallback_dirs_includes_windows_npm(monkeypatch, tmp_path):
    monkeypatch.delenv("OMNIGENT_CLI_FALLBACK_DIRS", raising=False)
    monkeypatch.setattr(_platform, "IS_WINDOWS", True)
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path / "Program Files"))
    dirs = _platform._cli_fallback_dirs()
    assert dirs[:2] == (tmp_path / "Roaming" / "npm", tmp_path / "Program Files" / "nodejs")


def test_resolve_cli_binary_windows_prefers_cmd_over_posix_shim(monkeypatch, tmp_path):
    posix_shim = tmp_path / "pi"
    posix_shim.write_text("#!/bin/sh\n")
    posix_shim.chmod(0o755)
    cmd_shim = tmp_path / "pi.cmd"
    cmd_shim.write_text("@echo off\n")
    monkeypatch.setattr(_platform, "IS_WINDOWS", True)
    monkeypatch.setattr(_platform, "_cli_fallback_dirs", lambda: (tmp_path,))
    monkeypatch.setattr(
        _platform.shutil, "which", lambda name: str(cmd_shim) if name == str(posix_shim) else None
    )
    assert _platform.resolve_cli_binary("pi", which=lambda _: None) == str(cmd_shim)


def test_cli_fallback_dirs_includes_nvm_version_bins(monkeypatch, tmp_path):
    """nvm keeps global bins under ~/.nvm/versions/node/<ver>/bin; the ladder
    must include those (newest first) since that's the reported nvm case.

    Ordering is by parsed numeric version, so double-digit majors (v10) beat
    single-digit ones (v9) — a lexicographic sort would get this backwards.
    """
    monkeypatch.delenv("OMNIGENT_CLI_FALLBACK_DIRS", raising=False)
    nvm = tmp_path / ".nvm" / "versions" / "node"
    for ver in ("v9.11.0", "v10.2.0", "v20.5.0"):
        (nvm / ver / "bin").mkdir(parents=True)
    monkeypatch.setattr(_platform.Path, "home", staticmethod(lambda: tmp_path))
    dirs = _platform._cli_fallback_dirs()
    for ver in ("v9.11.0", "v10.2.0", "v20.5.0"):
        assert nvm / ver / "bin" in dirs
    # newest → oldest, numerically: v20 > v10 > v9 (not the lexicographic
    # order where "v9" would sort after "v10"/"v20").
    order = [dirs.index(nvm / v / "bin") for v in ("v20.5.0", "v10.2.0", "v9.11.0")]
    assert order == sorted(order)


def test_cli_fallback_dirs_no_nvm_dir_is_safe(monkeypatch, tmp_path):
    """Absent ~/.nvm, the ladder still returns the static dirs without error."""
    monkeypatch.delenv("OMNIGENT_CLI_FALLBACK_DIRS", raising=False)
    monkeypatch.setattr(_platform.Path, "home", staticmethod(lambda: tmp_path))
    dirs = _platform._cli_fallback_dirs()
    assert tmp_path / ".local" / "bin" in dirs


@pytest.mark.parametrize("override", ["", os.pathsep])
def test_empty_cli_fallback_override_disables_global_discovery(monkeypatch, tmp_path, override):
    global_bin = tmp_path / ".local" / "bin"
    global_bin.mkdir(parents=True)
    cli = global_bin / "claude"
    cli.write_text("#!/bin/sh\nexit 99\n")
    cli.chmod(0o755)
    monkeypatch.setattr(_platform.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(_platform.shutil, "which", lambda _name: None)
    monkeypatch.setenv("OMNIGENT_CLI_FALLBACK_DIRS", override)

    assert _platform._cli_fallback_dirs() == ()
    assert _platform.resolve_cli_binary("claude") is None


def test_cli_fallback_override_preserves_order_and_path_priority(monkeypatch, tmp_path):
    directories = (tmp_path / "first bin", tmp_path / "second bin")
    for directory in directories:
        directory.mkdir()
        cli = directory / "claude"
        cli.write_text("#!/bin/sh\nexit 99\n")
        cli.chmod(0o755)
    monkeypatch.setenv("OMNIGENT_CLI_FALLBACK_DIRS", os.pathsep.join(map(str, directories)))
    monkeypatch.setattr(_platform.shutil, "which", lambda _name: None)

    assert _platform._cli_fallback_dirs() == directories
    assert _platform.resolve_cli_binary("claude") == str(directories[0] / "claude")
    monkeypatch.setattr(_platform.shutil, "which", lambda _name: "/opt/work/pinned/claude")
    assert _platform.resolve_cli_binary("claude") == "/opt/work/pinned/claude"


@pytest.mark.parametrize("value", ["", "/opt/work/isolated/bin"])
def test_cli_fallback_override_survives_environment_filters(monkeypatch, value):
    from omnigent.cli import _build_host_daemon_env
    from omnigent.host.connect import _build_runner_env
    from omnigent.inner.agent_env import clean_agent_env
    from omnigent.inner.os_env import build_helper_env
    from omnigent.inner.sandbox import SandboxPolicy

    source = {"OMNIGENT_CLI_FALLBACK_DIRS": value}
    runner = _build_runner_env(
        base_env=source,
        server_url="http://example.test",
        runner_id="runner",
        binding_token="test",
        workspace=".",
        parent_pid=os.getpid(),
    )
    helper = build_helper_env(
        source,
        SandboxPolicy(
            backend_type="none",
            active=True,
            read_roots=None,
            write_roots=[],
            write_files=[],
            allow_network=True,
        ),
    )
    monkeypatch.setenv("OMNIGENT_CLI_FALLBACK_DIRS", value)
    daemon = _build_host_daemon_env(server_url="https://example.test")
    for filtered in (daemon, runner, clean_agent_env(source=source), helper):
        assert filtered["OMNIGENT_CLI_FALLBACK_DIRS"] == value


def test_malloc_tuning_env_empty_off_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    """Off Linux the tuning helper is a no-op so callers need no platform branch."""
    monkeypatch.setattr(_proc, "IS_LINUX", False)
    assert _proc.malloc_tuning_env() == {}


def test_malloc_tuning_env_defaults_on_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    """On Linux the helper caps arenas and sets a trim threshold by default."""
    monkeypatch.setattr(_proc, "IS_LINUX", True)
    monkeypatch.delenv("OMNIGENT_RUNNER_MALLOC_ARENA_MAX", raising=False)
    monkeypatch.delenv("OMNIGENT_RUNNER_MALLOC_TRIM_THRESHOLD", raising=False)
    assert _proc.malloc_tuning_env() == {
        "MALLOC_ARENA_MAX": "2",
        "MALLOC_TRIM_THRESHOLD_": "134217728",
    }


def test_malloc_tuning_env_arena_max_zero_disables_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``OMNIGENT_RUNNER_MALLOC_ARENA_MAX=0`` drops the arena cap (revert knob)."""
    monkeypatch.setattr(_proc, "IS_LINUX", True)
    monkeypatch.setenv("OMNIGENT_RUNNER_MALLOC_ARENA_MAX", "0")
    monkeypatch.delenv("OMNIGENT_RUNNER_MALLOC_TRIM_THRESHOLD", raising=False)
    env = _proc.malloc_tuning_env()
    assert "MALLOC_ARENA_MAX" not in env
    assert env["MALLOC_TRIM_THRESHOLD_"] == "134217728"


def test_malloc_tuning_env_honors_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Operator overrides flow through to the child env values."""
    monkeypatch.setattr(_proc, "IS_LINUX", True)
    monkeypatch.setenv("OMNIGENT_RUNNER_MALLOC_ARENA_MAX", "1")
    monkeypatch.setenv("OMNIGENT_RUNNER_MALLOC_TRIM_THRESHOLD", "65536")
    assert _proc.malloc_tuning_env() == {
        "MALLOC_ARENA_MAX": "1",
        "MALLOC_TRIM_THRESHOLD_": "65536",
    }


# --------------------------------------------------------------------------
# posix_shell_path
# --------------------------------------------------------------------------


# Spelled as Windows stores them: ``os.environ`` keys are uppercased there.
_GIT_INSTALL_ENV_VARS = ("PROGRAMFILES", "PROGRAMW6432", "PROGRAMFILES(X86)", "LOCALAPPDATA")


def _fake_shell(path: Path) -> Path:
    """Create an executable stand-in at *path* and its ``.exe`` twin.

    ``shutil.which`` matches the bare name on POSIX and the ``PATHEXT`` name on
    Windows, so both spellings exist for the lookup to work on either host.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    for candidate in (path, path.with_name(f"{path.name}.exe")):
        candidate.write_text("#!/bin/sh\n")
        candidate.chmod(0o755)
    return path


def _shell_lookup_env(
    monkeypatch: pytest.MonkeyPatch, *path_dirs: Path, is_windows: bool = True
) -> None:
    """Pin ``PATH`` to *path_dirs*, hide every Git install location, set the OS flag."""
    monkeypatch.setattr(_platform, "IS_WINDOWS", is_windows)
    monkeypatch.setenv("PATH", os.pathsep.join(str(d) for d in path_dirs))
    for var in _GIT_INSTALL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_posix_shell_path_prefers_git_bash_over_wsl_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PowerShell PATH (System32 first, only Git's ``cmd`` dir) yields Git's bash."""
    system32 = tmp_path / "Windows" / "System32"
    _fake_shell(system32 / "bash")
    _fake_shell(tmp_path / "Git" / "cmd" / "git")
    _fake_shell(tmp_path / "Git" / "bin" / "bash")
    _shell_lookup_env(monkeypatch, system32, tmp_path / "Git" / "cmd")

    assert _platform.posix_shell_path() == str(tmp_path / "Git" / "bin" / "bash.exe")


@pytest.mark.parametrize("is_windows", [True, False])
def test_posix_shell_path_keeps_non_launcher_bash_later_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, is_windows: bool
) -> None:
    """A bash behind the launcher on PATH wins on either OS: the launcher is known by location."""
    system32 = tmp_path / "Windows" / "System32"
    msys_bin = tmp_path / "msys64" / "usr" / "bin"
    _fake_shell(system32 / "bash")
    _fake_shell(msys_bin / "bash")
    _shell_lookup_env(monkeypatch, system32, msys_bin, is_windows=is_windows)

    resolved = _platform.posix_shell_path()

    assert resolved is not None
    assert Path(resolved).parent == msys_bin


def test_posix_shell_path_falls_back_to_program_files_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no ``git`` on PATH, the per-machine Git install is still found."""
    system32 = tmp_path / "Windows" / "System32"
    program_files = tmp_path / "Program Files"
    _fake_shell(system32 / "bash")
    _fake_shell(program_files / "Git" / "bin" / "bash")
    _shell_lookup_env(monkeypatch, system32)
    monkeypatch.setenv("PROGRAMFILES", str(program_files))

    assert _platform.posix_shell_path() == str(program_files / "Git" / "bin" / "bash.exe")


@pytest.mark.parametrize(
    "launcher_dir",
    ["Windows/System32", "Windows/Sysnative", "AppData/Local/Microsoft/WindowsApps"],
)
def test_posix_shell_path_none_when_only_wsl_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launcher_dir: str
) -> None:
    """Every WSL launcher location is skipped, and nothing else is guessed."""
    launcher = tmp_path.joinpath(*launcher_dir.split("/"))
    _fake_shell(launcher / "bash")
    _shell_lookup_env(monkeypatch, launcher)

    assert _platform.posix_shell_path() is None
