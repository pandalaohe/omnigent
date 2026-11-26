"""Unit tests for the unified Windows CLI launcher base and per-harness wiring.

All tests run cross-platform on macOS/Linux using platform fakes and neutral
fixture paths (``tmp_path`` / ``C:\\Users\\sample\\...``), while pinning POSIX
resolution unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from omnigent import _platform
from omnigent import _windows_launcher as wl


def _write_fake_exe(path: Path, payload: bytes = b"MZ00fake") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    path.chmod(0o755)
    return path


def _write_npm_js_cmd(shim_path: Path, rel_js: str) -> Path:
    shim_path.parent.mkdir(parents=True, exist_ok=True)
    body = (
        "@ECHO off\r\n"
        "GOTO start\r\n"
        ":find_dp0\r\n"
        "SET dp0=%~dp0\r\n"
        "EXIT /b\r\n"
        ":start\r\n"
        "SETLOCAL\r\n"
        "CALL :find_dp0\r\n"
        'IF EXIST "%dp0%\\node.exe" (\r\n'
        '  SET "_prog=%dp0%\\node.exe"\r\n'
        ") ELSE (\r\n"
        '  SET "_prog=node"\r\n'
        "  SET PATHEXT=%PATHEXT:;.JS;=;%\r\n"
        ")\r\n"
        'endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  '
        f'"%dp0%\\{rel_js}" %*\r\n'
    )
    shim_path.write_bytes(body.encode("utf-8"))
    return shim_path


def _write_pnpm_js_cmd(shim_path: Path, rel_js: str, *, node_path: str | None = None) -> Path:
    shim_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "@SETLOCAL",
        "@SET PATHEXT=%PATHEXT:;.JS;=;%",
    ]
    if node_path is not None:
        lines.append(f'@SET "NODE_PATH={node_path}"')
    lines.append(f'node "%~dp0\\{rel_js}" %*')
    shim_path.write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8"))
    return shim_path


def _write_native_cmd(shim_path: Path, rel_exe: str) -> Path:
    shim_path.parent.mkdir(parents=True, exist_ok=True)
    body = (
        "@ECHO off\r\n"
        "GOTO start\r\n"
        ":find_dp0\r\n"
        "SET dp0=%~dp0\r\n"
        "EXIT /b\r\n"
        ":start\r\n"
        "SETLOCAL\r\n"
        "CALL :find_dp0\r\n"
        f'"%dp0%\\{rel_exe}" %*\r\n'
    )
    shim_path.write_bytes(body.encode("utf-8"))
    return shim_path


# ---------------------------------------------------------------------------
# Base: three-layer PATH merge, PATHEXT, and shim unwrapping
# ---------------------------------------------------------------------------


def test_merged_windows_path_layers_expands_and_dedupes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        wl,
        "_read_registry_path",
        lambda scope: (
            r"C:\Windows\System32;%PROGRAMFILES%\nodejs"
            if scope == "machine"
            else r"%LOCALAPPDATA%\pnpm;%LOCALAPPDATA%\Volta\bin;C:\windows\system32\\"
        ),
    )
    env = {
        "PATH": r"C:\Users\sample\custom;C:\WINDOWS\System32",
        "PROGRAMFILES": r"C:\Program Files",
        "APPDATA": r"C:\Users\sample\AppData\Roaming",
        "LOCALAPPDATA": r"C:\Users\sample\AppData\Local",
        "USERPROFILE": r"C:\Users\sample",
    }

    merged = wl.merged_windows_path(env, is_windows=True, home=Path(r"C:\Users\sample"))
    entries = merged.split(";")
    assert entries[0] == r"C:\Users\sample\custom"
    assert entries[1] == r"C:\WINDOWS\System32"
    # Case-insensitive duplicate from Machine/User registry is dropped.
    assert sum(1 for e in entries if e.lower().rstrip("\\") == r"c:\windows\system32") == 1
    assert r"C:\Program Files\nodejs" in entries
    assert r"C:\Users\sample\AppData\Local\pnpm" in entries
    assert r"C:\Users\sample\AppData\Local\Volta\bin" in entries
    assert r"C:\Users\sample\AppData\Roaming\npm" in entries
    assert r"C:\Users\sample\AppData\Local\Microsoft\WinGet\Links" in entries
    assert r"C:\Users\sample\.local\bin" in entries
    assert r"C:\Users\sample\.bun\bin" in entries
    assert r"C:\Users\sample\scoop\shims" in entries


def test_merged_windows_path_honors_empty_fallback_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _unexpected_registry(_scope: str) -> str:
        raise AssertionError("registry must not be read when OMNIGENT_CLI_FALLBACK_DIRS is set")

    monkeypatch.setattr(wl, "_read_registry_path", _unexpected_registry)
    env = {
        "PATH": r"C:\Users\sample\bin",
        "OMNIGENT_CLI_FALLBACK_DIRS": "",
    }
    assert wl.merged_windows_path(env, is_windows=True) == r"C:\Users\sample\bin"


def test_posix_helpers_are_strict_noops(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(_platform, "IS_WINDOWS", False)
    assert wl.merged_windows_path({"PATH": "/usr/local/bin:/usr/bin"}) == "/usr/local/bin:/usr/bin"
    cmd_shim = _write_pnpm_js_cmd(tmp_path / "tool.cmd", "cli.js")
    assert wl.windows_cli_argv(str(cmd_shim)) == [str(cmd_shim)]
    assert wl.windows_launch_spec(str(cmd_shim)) == ([str(cmd_shim)], {})
    assert wl.resolve_claude_windows_executable(str(cmd_shim)) == str(cmd_shim)
    assert wl.split_command_argv("agy acp --model 'gemini-2.5'") == [
        "agy",
        "acp",
        "--model",
        "gemini-2.5",
    ]
    assert wl.resolve_windows_command_argv(["agy", "acp"]) == (["agy", "acp"], {})
    assert wl.windows_short_path(tmp_path) == str(tmp_path)


def test_resolve_cli_binary_windows_discovers_registry_pnpm_and_volta(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    reg_dir = tmp_path / "reg-tools"
    reg_dir.mkdir()
    reg_tool = _write_fake_exe(reg_dir / "regtool.exe")

    local_appdata = tmp_path / "AppData" / "Local"
    pnpm_dir = local_appdata / "pnpm"
    pnpm_tool = _write_pnpm_js_cmd(pnpm_dir / "pnpmdemo.cmd", r"global\5\cli.js")
    volta_dir = local_appdata / "Volta" / "bin"
    volta_tool = _write_fake_exe(volta_dir / "voltademo.exe")

    monkeypatch.delenv("OMNIGENT_CLI_FALLBACK_DIRS", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    monkeypatch.setattr(_platform, "IS_WINDOWS", True)
    monkeypatch.setattr(_platform.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(
        wl,
        "_read_registry_path",
        lambda scope: str(reg_dir) if scope == "user" else None,
    )

    assert _platform.resolve_cli_binary("regtool", which=lambda _: None) == str(reg_tool)
    assert _platform.resolve_cli_binary("pnpmdemo", which=lambda _: None) == str(pnpm_tool)
    assert _platform.resolve_cli_binary("voltademo", which=lambda _: None) == str(volta_tool)


def test_resolve_cli_binary_windows_pathext_prefers_exe_over_cmd_and_bare(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bare = tmp_path / "claude"
    bare.write_text("#!/bin/sh\n")
    bare.chmod(0o755)
    (tmp_path / "claude.cmd").write_text("@echo off\n")
    exe = _write_fake_exe(tmp_path / "claude.exe")

    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    monkeypatch.setenv("OMNIGENT_CLI_FALLBACK_DIRS", str(tmp_path))
    monkeypatch.setattr(_platform, "IS_WINDOWS", True)
    assert _platform.resolve_cli_binary("claude", which=lambda _: None) == str(exe)


def test_unwrap_windows_cmd_shim_handles_npm_pnpm_native_and_rejects_custom(
    tmp_path: Path,
) -> None:
    npm_js = tmp_path / "npm" / "node_modules" / "demo" / "cli.js"
    npm_js.parent.mkdir(parents=True)
    npm_js.write_text("console.log('ok');\n")
    npm_cmd = _write_npm_js_cmd(tmp_path / "npm" / "demo.cmd", r"node_modules\demo\cli.js")
    npm_node = _write_fake_exe(tmp_path / "npm" / "node.exe")

    argv, env = wl.windows_launch_spec(str(npm_cmd), is_windows=True)
    assert argv == [str(npm_node), str(npm_js.resolve())]
    assert env == {}

    pnpm_js = tmp_path / "pnpm" / "global" / "5" / "node_modules" / "demo" / "cli.js"
    pnpm_js.parent.mkdir(parents=True)
    pnpm_js.write_text("console.log('pnpm');\n")
    pnpm_cmd = _write_pnpm_js_cmd(
        tmp_path / "pnpm" / "demo.cmd",
        r"global\5\node_modules\demo\cli.js",
        node_path=r"%~dp0\global\5\node_modules",
    )
    pnpm_argv, pnpm_env = wl.windows_launch_spec(
        str(pnpm_cmd),
        node_resolver=lambda _: r"C:\Program Files\nodejs\node.exe",
        is_windows=True,
    )
    assert pnpm_argv == [r"C:\Program Files\nodejs\node.exe", str(pnpm_js.resolve())]
    assert pnpm_env["NODE_PATH"].endswith("global\\5\\node_modules")

    native_exe = _write_fake_exe(tmp_path / "bin" / "node_modules" / "pkg" / "tool.exe")
    native_cmd = _write_native_cmd(tmp_path / "bin" / "tool.cmd", r"node_modules\pkg\tool.exe")
    assert wl.windows_cli_argv(str(native_cmd), is_windows=True) == [str(native_exe.resolve())]

    custom_cmd = tmp_path / "custom.cmd"
    custom_cmd.write_text("@echo off\nset CUSTOM_VAR=1\nnode cli.js %*\n")
    assert wl.unwrap_windows_cmd_shim(custom_cmd) is None
    assert wl.windows_cli_argv(str(custom_cmd), is_windows=True) == [str(custom_cmd)]
    with pytest.raises(OSError, match="not a recognized npm/pnpm shim"):
        wl.windows_cli_argv(str(custom_cmd), strict_batch=True, is_windows=True)


# ---------------------------------------------------------------------------
# Per-harness: Pi
# ---------------------------------------------------------------------------


def test_pi_cli_argv_unwraps_pnpm_and_volta_and_preserves_posix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import omnigent.harnesses.pi_cli as pi_cli

    entry = tmp_path / "pnpm" / "global" / "5" / "node_modules" / "pi" / "cli.js"
    entry.parent.mkdir(parents=True)
    entry.write_text("console.log('pi');\n")
    pnpm_shim = _write_pnpm_js_cmd(
        tmp_path / "pnpm" / "pi.cmd",
        r"global\5\node_modules\pi\cli.js",
    )
    volta_exe = _write_fake_exe(tmp_path / "Volta" / "bin" / "pi.exe")

    monkeypatch.setattr(pi_cli, "IS_WINDOWS", False)
    assert pi_cli.pi_cli_argv(str(pnpm_shim)) == [str(pnpm_shim)]

    monkeypatch.setattr(pi_cli, "IS_WINDOWS", True)
    monkeypatch.setattr(pi_cli, "resolve_cli_binary", lambda name: r"C:\nodejs\node.exe")
    assert pi_cli.pi_cli_argv(str(pnpm_shim)) == [r"C:\nodejs\node.exe", str(entry.resolve())]
    assert pi_cli.pi_cli_argv(str(volta_exe)) == [str(volta_exe)]


# ---------------------------------------------------------------------------
# Per-harness: Codex
# ---------------------------------------------------------------------------


def test_codex_binary_unwraps_pnpm_cmd_and_keeps_custom_wrapper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnigent.inner import codex_binary

    monkeypatch.setattr(codex_binary, "data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(codex_binary.platform, "system", lambda: "Windows")
    monkeypatch.setattr(codex_binary.platform, "machine", lambda: "AMD64")

    pkg = tmp_path / "pnpm" / "global" / "5" / "node_modules" / "@openai" / "codex"
    js_entry = pkg / "bin" / "codex.js"
    js_entry.parent.mkdir(parents=True)
    js_entry.write_text(
        "// Unified entry point for the Codex CLI.\n"
        "const binaryPath = findCodexExecutable();\n"
        "spawn(binaryPath, process.argv.slice(2));\n"
    )
    (pkg / "package.json").write_text('{"name":"@openai/codex"}')

    target = "x86_64-pc-windows-msvc"
    opt = pkg / "node_modules" / "@openai" / "codex-win32-x64"
    vendor_root = opt / "vendor" / target
    vendor_exe = _write_fake_exe(vendor_root / "bin" / "codex.exe", b"MZ00codex-pnpm")
    _write_fake_exe(vendor_root / "bin" / "codex-code-mode-host.exe", b"MZ00host")
    (opt / "package.json").write_text("{}")
    (vendor_root / "codex-package.json").write_text(
        json.dumps(
            {
                "layoutVersion": 1,
                "version": "0.164.0",
                "target": target,
                "entrypoint": "bin/codex.exe",
            }
        )
    )

    pnpm_cmd = _write_pnpm_js_cmd(
        tmp_path / "pnpm" / "codex.cmd",
        r"global\5\node_modules\@openai\codex\bin\codex.js",
    )
    snapped = Path(codex_binary.snapshot_codex_binary(str(pnpm_cmd)))
    assert snapped != vendor_exe
    assert snapped.read_bytes() == b"MZ00codex-pnpm"

    custom_cmd = tmp_path / "pnpm" / "custom-codex.cmd"
    custom_cmd.write_text(
        "@set MY_WRAPPER=1\r\n"
        'node "%~dp0\\global\\5\\node_modules\\@openai\\codex\\bin\\codex.js" %*\r\n'
    )
    assert codex_binary.snapshot_codex_binary(str(custom_cmd)) == str(custom_cmd)


@pytest.mark.asyncio
async def test_codex_version_probe_unwraps_cmd_shim_on_windows_and_keeps_posix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnigent.inner import codex_executor as ce

    entry = tmp_path / "pnpm" / "global" / "5" / "node_modules" / "@openai" / "codex" / "codex.js"
    entry.parent.mkdir(parents=True)
    entry.write_text("console.log('codex-cli 0.164.0');\n")
    shim = _write_pnpm_js_cmd(
        tmp_path / "pnpm" / "codex.cmd",
        r"global\5\node_modules\@openai\codex\codex.js",
        node_path=r"%~dp0\global\5\node_modules",
    )
    node = _write_fake_exe(tmp_path / "pnpm" / "node.exe")

    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def fake_spawn(*args: object, **kwargs: object) -> SimpleNamespace:
        calls.append((args, kwargs))
        return SimpleNamespace(communicate=AsyncMock(return_value=(b"codex-cli 0.164.0\n", b"")))

    monkeypatch.setattr(ce, "_create_subprocess_exec", fake_spawn)

    monkeypatch.setattr(_platform, "IS_WINDOWS", False)
    assert await ce._codex_cli_version_text(str(shim)) == "0.164.0"
    assert calls[-1][0] == (str(shim), "--version")

    monkeypatch.setattr(_platform, "IS_WINDOWS", True)
    assert await ce._codex_cli_version_text(str(shim)) == "0.164.0"
    assert calls[-1][0] == (str(node), str(entry.resolve()), "--version")
    spawn_env = calls[-1][1]["env"]
    assert isinstance(spawn_env, dict)
    assert "NODE_PATH" in spawn_env


# ---------------------------------------------------------------------------
# Per-harness: Claude Code
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("layout", ["npm", "pnpm", "winget", "volta"])
def test_claude_find_system_claude_unwraps_windows_install_layouts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, layout: str
) -> None:
    from omnigent.inner import claude_sdk_executor as cse

    monkeypatch.setattr(_platform, "IS_WINDOWS", True)
    monkeypatch.delenv("OMNIGENT_CLAUDE_PATH", raising=False)
    monkeypatch.delenv("OMNIGENT_CLI_FALLBACK_DIRS", raising=False)

    if layout == "npm":
        shim_dir = tmp_path / "AppData" / "Roaming" / "npm"
        expected = _write_fake_exe(
            shim_dir / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        )
        resolved_shim = _write_npm_js_cmd(
            shim_dir / "claude.cmd", r"node_modules\@anthropic-ai\claude-code\cli.js"
        )
    elif layout == "pnpm":
        shim_dir = tmp_path / "AppData" / "Local" / "pnpm"
        expected = _write_fake_exe(
            shim_dir
            / "global"
            / "5"
            / "node_modules"
            / "@anthropic-ai"
            / "claude-code"
            / "bin"
            / "claude.exe"
        )
        resolved_shim = _write_pnpm_js_cmd(
            shim_dir / "claude.cmd",
            r"global\5\node_modules\@anthropic-ai\claude-code\cli.js",
        )
    elif layout == "winget":
        winget_root = tmp_path / "AppData" / "Local" / "Microsoft" / "WinGet"
        expected = _write_fake_exe(
            winget_root
            / "Packages"
            / "Anthropic.ClaudeCode_Microsoft.Winget.Source_8wekyb3d8bbwe"
            / "claude.exe"
        )
        resolved_shim = winget_root / "Links" / "claude.cmd"
        resolved_shim.parent.mkdir(parents=True)
        resolved_shim.write_bytes(
            b"@ECHO off\r\n"
            b'"%~dp0\\..\\Packages\\'
            b"Anthropic.ClaudeCode_Microsoft.Winget.Source_8wekyb3d8bbwe"
            b'\\claude.exe" %*\r\n'
        )
    else:
        expected = _write_fake_exe(tmp_path / "AppData" / "Local" / "Volta" / "bin" / "claude.exe")
        resolved_shim = expected

    monkeypatch.setattr(
        cse, "resolve_cli_binary", lambda _name, *, env_var=None: str(resolved_shim)
    )
    assert cse._find_system_claude() == str(expected.resolve(strict=False))


def test_claude_unwraps_only_on_windows_and_warns_without_native_exe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    shim = _write_npm_js_cmd(
        tmp_path / "claude.cmd", r"node_modules\@anthropic-ai\claude-code\cli.js"
    )

    monkeypatch.setattr(_platform, "IS_WINDOWS", False)
    assert wl.resolve_claude_windows_executable(str(shim)) == str(shim)

    monkeypatch.setattr(_platform, "IS_WINDOWS", True)
    with caplog.at_level("WARNING"):
        assert wl.resolve_claude_windows_executable(str(shim)) == str(shim)
    assert "without a native claude.exe" in caplog.text


# ---------------------------------------------------------------------------
# Per-harness: Antigravity (AG) + ACP + junction + MAX_PATH short temp
# ---------------------------------------------------------------------------


def test_agy_binary_path_and_acp_command_recognition_on_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnigent.acp_cli_harnesses import is_antigravity_acp_command
    from omnigent.harnesses.antigravity_native import launch as agy_launch

    local_appdata = tmp_path / "AppData" / "Local"
    pnpm_dir = local_appdata / "pnpm"
    real_exe = _write_fake_exe(pnpm_dir / "node_modules" / "agy" / "bin" / "agy.exe")
    _write_native_cmd(pnpm_dir / "agy.cmd", r"node_modules\agy\bin\agy.exe")

    monkeypatch.delenv("OMNIGENT_CLI_FALLBACK_DIRS", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    monkeypatch.setattr(_platform, "IS_WINDOWS", True)
    monkeypatch.setattr(agy_launch, "IS_WINDOWS", True)
    monkeypatch.setattr(agy_launch.shutil, "which", lambda _name: None)
    monkeypatch.setattr(wl, "_read_registry_path", lambda _scope: None)

    assert agy_launch.agy_binary_path() == str(real_exe.resolve())
    assert is_antigravity_acp_command(r"C:\Users\sample\AppData\Local\pnpm\agy.cmd acp")
    assert is_antigravity_acp_command(r'"C:\Program Files\Antigravity\agy.exe" acp')


@pytest.mark.asyncio
async def test_acp_executor_windows_unwraps_agy_and_sets_short_temp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnigent.inner import acp_executor as acp_mod

    local_appdata = tmp_path / "l"
    local_appdata.mkdir()
    long_temp = tmp_path / "very" / "deeply" / "nested" / "temporary" / "directory"
    long_temp.mkdir(parents=True)

    pnpm_dir = local_appdata / "pnpm"
    agy_exe = _write_fake_exe(pnpm_dir / "node_modules" / "agy" / "bin" / "agy.exe")
    _write_native_cmd(pnpm_dir / "agy.cmd", r"node_modules\agy\bin\agy.exe")

    monkeypatch.delenv("OMNIGENT_CLI_FALLBACK_DIRS", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))
    monkeypatch.setenv("APPDATA", str(tmp_path / "r"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("TEMP", str(long_temp))
    monkeypatch.setenv("TMP", str(long_temp))
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    monkeypatch.setattr(_platform, "IS_WINDOWS", True)
    monkeypatch.setattr(acp_mod, "IS_WINDOWS", True)
    monkeypatch.setattr(wl, "_read_registry_path", lambda _scope: None)
    monkeypatch.setattr(_platform.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        wl,
        "_get_short_path_name_win32",
        lambda p: p.replace(str(local_appdata), "C:\\L~1"),
    )

    executor = acp_mod.AcpExecutor(
        config=acp_mod.AcpAgentConfig(command="agy acp", name="Antigravity"),
        cwd=str(tmp_path),
    )

    spawned: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class _FakeStream:
        async def readline(self) -> bytes:
            return b""

    class _FakeProc:
        pid = 12345
        returncode: int | None = 0
        stdin = SimpleNamespace(close=lambda: None)
        stdout = _FakeStream()
        stderr = _FakeStream()

        async def wait(self) -> int:
            return 0

    async def fake_spawn(*args: object, **kwargs: object) -> _FakeProc:
        spawned.append((args, kwargs))
        return _FakeProc()

    monkeypatch.setattr(acp_mod.asyncio, "create_subprocess_exec", fake_spawn)
    await executor._start_process_unobserved()

    assert len(spawned) == 1
    argv, kwargs = spawned[0]
    assert argv == (str(agy_exe.resolve()), "acp")
    spawn_env = kwargs["env"]
    assert isinstance(spawn_env, dict)
    assert spawn_env["TEMP"] == spawn_env["TMP"]
    assert spawn_env["TEMP"].startswith("C:\\L~1")
    assert len(spawn_env["TEMP"]) < len(str(long_temp))
    assert str(pnpm_dir) in spawn_env["PATH"].split(";")
    created_dir = executor._agy_runtime_temp_dir
    assert created_dir is not None and created_dir.is_dir()

    await executor.close()
    assert executor._agy_runtime_temp_dir is None
    assert not created_dir.exists()


def test_antigravity_bridge_uses_windows_junction_and_file_copy_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnigent.harnesses.antigravity_native import bridge as agy_bridge

    real_dir = tmp_path / "real" / "skills"
    real_dir.mkdir(parents=True)
    real_file = tmp_path / "real" / "AGENTS.md"
    real_file.write_text("# rules\n", encoding="utf-8")

    iso_dir = tmp_path / "iso" / "skills"
    iso_file = tmp_path / "iso" / "AGENTS.md"

    junction_calls: list[tuple[Path, Path]] = []

    def fake_junction(target: Path, link: Path) -> bool:
        junction_calls.append((target, link))
        link.mkdir()
        return True

    monkeypatch.setattr(_platform, "IS_WINDOWS", True)
    monkeypatch.setattr(wl, "_create_windows_junction", fake_junction)

    def _refuse_file_symlink(self: Path, *_args: object, **_kwargs: object) -> None:
        raise OSError("WinError 1314 privilege not held")

    monkeypatch.setattr(Path, "symlink_to", _refuse_file_symlink)

    agy_bridge._link_into_isolated_gemini_dir(real_dir, iso_dir)
    agy_bridge._link_into_isolated_gemini_dir(real_file, iso_file)

    assert junction_calls == [(real_dir.resolve(), iso_dir)]
    assert iso_file.read_text(encoding="utf-8") == "# rules\n"


def test_harness_install_unwraps_windows_shims_for_probes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnigent.onboarding import harness_install as hi

    entry = tmp_path / "pnpm" / "global" / "5" / "cli.js"
    entry.parent.mkdir(parents=True)
    entry.write_text("console.log('1.2.3');\n")
    shim = _write_pnpm_js_cmd(tmp_path / "pnpm" / "codex.cmd", r"global\5\cli.js")
    node = _write_fake_exe(tmp_path / "pnpm" / "node.exe")

    seen: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
        seen.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout=b"codex-cli 0.164.0\n", stderr=b"")

    monkeypatch.setattr(_platform, "IS_WINDOWS", True)
    monkeypatch.setattr(hi, "IS_WINDOWS", True)
    monkeypatch.setattr(hi, "run_isolated", fake_run)
    monkeypatch.setattr(hi, "resolve_cli_binary", lambda _name: str(shim))
    hi.invalidate_harness_probe_caches()

    spec = hi.harness_install_spec("openai")
    assert spec is not None
    assert spec.status_args is not None
    assert hi._harness_cli_version_string(spec, str(shim)) == "0.164.0"
    assert seen[-1] == [str(node), str(entry.resolve()), "--version"]

    assert hi.harness_cli_logged_in("openai") is True
    assert seen[-1] == [str(node), str(entry.resolve()), *spec.status_args]


# ---------------------------------------------------------------------------
# Review R1 hardening: real pnpm templates, Win32 quoting, strict boundaries
# ---------------------------------------------------------------------------


def test_real_pnpm_conditional_cmd_shim_preserves_node_args_and_appends_node_path(
    tmp_path: Path,
) -> None:
    entry = tmp_path / "pnpm" / "global" / "5" / "node_modules" / "demo" / "bin" / "cli.js"
    entry.parent.mkdir(parents=True)
    entry.write_text("console.log('pnpm-real');\n")
    shim = tmp_path / "pnpm" / "demo.cmd"
    shim.write_bytes(
        b"@SETLOCAL\r\n"
        b"@SET PATHEXT=%PATHEXT:;.JS;=;%\r\n"
        b"IF NOT DEFINED NODE_PATH (\r\n"
        b'  SET "NODE_PATH=%~dp0\\global\\5\\node_modules"\r\n'
        b") ELSE (\r\n"
        b'  SET "NODE_PATH=%NODE_PATH%;%~dp0\\global\\5\\node_modules"\r\n'
        b")\r\n"
        b'IF EXIST "%~dp0\\node.exe" (\r\n'
        b'  "%~dp0\\node.exe" --no-deprecation '
        b'"%~dp0\\global\\5\\node_modules\\demo\\bin\\cli.js" %*\r\n'
        b") ELSE (\r\n"
        b"  node --no-deprecation "
        b'"%~dp0\\global\\5\\node_modules\\demo\\bin\\cli.js" %*\r\n'
        b")\r\n"
    )

    # Without sibling node.exe, falls back to node_resolver and keeps --no-deprecation.
    argv, shim_env = wl.windows_launch_spec(
        str(shim),
        node_resolver=lambda _: r"C:\Program Files\nodejs\node.exe",
        is_windows=True,
    )
    assert argv == [
        r"C:\Program Files\nodejs\node.exe",
        "--no-deprecation",
        str(entry.resolve()),
    ]
    assert shim_env["NODE_PATH"].endswith(r"global\5\node_modules")

    # Merging into an existing NODE_PATH appends rather than clobbering.
    merged = wl.merge_shim_env({"NODE_PATH": r"C:\existing\modules"}, shim_env)
    assert merged is not None
    assert merged["NODE_PATH"] == f"C:\\existing\\modules;{shim_env['NODE_PATH']}"

    # With sibling node.exe, prefers sibling node.exe.
    sibling_node = _write_fake_exe(tmp_path / "pnpm" / "node.exe")
    argv_local, _ = wl.windows_launch_spec(str(shim), is_windows=True)
    assert argv_local == [str(sibling_node), "--no-deprecation", str(entry.resolve())]


def test_unwrap_rejects_early_exit_and_side_effect_wrappers(tmp_path: Path) -> None:
    entry = tmp_path / "cli.js"
    entry.write_text("console.log('hi');\n")
    early_exit = tmp_path / "early.cmd"
    early_exit.write_bytes(b'@ECHO off\r\nEXIT /b 1\r\nnode "%~dp0\\cli.js" %*\r\n')
    assert wl.unwrap_windows_cmd_shim(early_exit) is None

    side_effect = tmp_path / "hook.cmd"
    side_effect.write_bytes(b'@ECHO off\r\ncall "%~dp0\\setup.cmd"\r\nnode "%~dp0\\cli.js" %*\r\n')
    assert wl.unwrap_windows_cmd_shim(side_effect) is None


def test_claude_does_not_bypass_custom_wrapper_even_with_sibling_package(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(_platform, "IS_WINDOWS", True)
    _write_fake_exe(
        tmp_path / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    )
    custom = tmp_path / "claude.cmd"
    custom.write_bytes(b"@ECHO off\r\nSET MY_PROXY=1\r\nnode custom.js %*\r\n")
    # Custom wrapper must be preserved, not bypassed to sibling node_modules.
    assert wl.resolve_claude_windows_executable(str(custom)) == str(custom)


def test_split_command_argv_windows_win32_quoting() -> None:
    assert wl.split_command_argv(
        r'"C:\Program Files\Antigravity\agy.exe" acp --label="hello world" --dir=C:\work\proj',
        is_windows=True,
    ) == [
        r"C:\Program Files\Antigravity\agy.exe",
        "acp",
        "--label=hello world",
        r"--dir=C:\work\proj",
    ]
    assert wl.split_command_argv(
        r'tool --json="{\"a\": \"b c\"}"',
        is_windows=True,
    ) == ["tool", '--json={"a": "b c"}']


def test_windows_short_temp_dir_is_exclusive_skips_links_and_falls_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    short_link = tmp_path / "s"
    real_target = tmp_path / "target"
    real_target.mkdir()
    short_link.symlink_to(real_target)

    unwritable = tmp_path / "ro"
    unwritable.mkdir()
    fallback = tmp_path / "longer-writable-root"
    fallback.mkdir()

    real_mkdtemp = wl.tempfile.mkdtemp

    def _fake_mkdtemp(*, prefix: str = "", dir: str | None = None) -> str:
        if dir == str(unwritable):
            raise PermissionError("read-only root")
        return real_mkdtemp(prefix=prefix, dir=dir)

    monkeypatch.setattr(wl.tempfile, "mkdtemp", _fake_mkdtemp)
    env = {
        "LOCALAPPDATA": str(short_link),
        "TEMP": str(unwritable),
        "TMP": str(fallback),
    }

    dir1, _ = wl.windows_short_temp_dir("og-ag", env=env, is_windows=True)
    dir2, _ = wl.windows_short_temp_dir("og-ag", env=env, is_windows=True)
    assert dir1.parent == fallback
    assert dir2.parent == fallback
    assert dir1 != dir2


@pytest.mark.asyncio
async def test_acp_executor_honors_env_unset_and_cleans_up_temp_on_spawn_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnigent.inner import acp_executor as acp_mod

    local_appdata = tmp_path / "l"
    local_appdata.mkdir()
    pnpm_dir = local_appdata / "pnpm"
    entry = pnpm_dir / "global" / "5" / "cli.js"
    entry.parent.mkdir(parents=True)
    entry.write_text("console.log('agy');\n")
    _write_pnpm_js_cmd(
        pnpm_dir / "agy.cmd",
        r"global\5\cli.js",
        node_path=r"%~dp0\global\5\node_modules",
    )
    node = _write_fake_exe(pnpm_dir / "node.exe")

    monkeypatch.delenv("OMNIGENT_CLI_FALLBACK_DIRS", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))
    monkeypatch.setenv("APPDATA", str(tmp_path / "r"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("TEMP", str(tmp_path / "long-temp"))
    monkeypatch.setenv("TMP", str(tmp_path / "long-temp"))
    (tmp_path / "long-temp").mkdir()
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    monkeypatch.setattr(_platform, "IS_WINDOWS", True)
    monkeypatch.setattr(acp_mod, "IS_WINDOWS", True)
    monkeypatch.setattr(wl, "_read_registry_path", lambda _scope: None)

    # Denied PATH / TEMP / TMP / NODE_PATH must stay absent in child env.
    executor = acp_mod.AcpExecutor(
        config=acp_mod.AcpAgentConfig(
            command="agy acp",
            name="Antigravity",
            env_unset=("PATH", "TEMP", "TMP", "NODE_PATH"),
        ),
        cwd=str(tmp_path),
    )

    seen_env: list[dict[str, str]] = []

    class _FakeStream:
        async def readline(self) -> bytes:
            return b""

    class _FakeProc:
        pid = 23456
        returncode: int | None = 0
        stdin = SimpleNamespace(close=lambda: None)
        stdout = _FakeStream()
        stderr = _FakeStream()

        async def wait(self) -> int:
            return 0

    async def fake_spawn(*args: object, **kwargs: object) -> _FakeProc:
        assert args == (str(node), str(entry.resolve()), "acp")
        seen_env.append(dict(kwargs["env"]))  # type: ignore[arg-type]
        return _FakeProc()

    monkeypatch.setattr(acp_mod.asyncio, "create_subprocess_exec", fake_spawn)
    await executor._start_process_unobserved()
    assert "PATH" not in seen_env[0]
    assert "TEMP" not in seen_env[0]
    assert "TMP" not in seen_env[0]
    assert "NODE_PATH" not in seen_env[0]
    assert executor._agy_runtime_temp_dir is None
    await executor.close()

    # Failed spawn must immediately clean up the allocated AG short temp dir.
    failing_executor = acp_mod.AcpExecutor(
        config=acp_mod.AcpAgentConfig(command="agy acp", name="Antigravity"),
        cwd=str(tmp_path),
    )
    created_during_spawn: list[Path] = []

    async def failing_spawn(*_args: object, **_kwargs: object) -> _FakeProc:
        assert failing_executor._agy_runtime_temp_dir is not None
        created_during_spawn.append(failing_executor._agy_runtime_temp_dir)
        raise OSError("spawn failed")

    monkeypatch.setattr(acp_mod.asyncio, "create_subprocess_exec", failing_spawn)
    with pytest.raises(OSError, match="spawn failed"):
        await failing_executor._start_process_unobserved()
    assert len(created_during_spawn) == 1
    assert not created_during_spawn[0].exists()
    assert failing_executor._agy_runtime_temp_dir is None


def test_agy_binary_path_respects_empty_fallback_override_and_posix_rejects_cmd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnigent.acp_cli_harnesses import is_antigravity_acp_command
    from omnigent.harnesses.antigravity_native import launch as agy_launch

    fake_home = tmp_path / "home"
    _write_fake_exe(fake_home / ".local" / "bin" / "agy.exe")

    monkeypatch.setenv("OMNIGENT_CLI_FALLBACK_DIRS", "")
    monkeypatch.setattr(_platform, "IS_WINDOWS", True)
    monkeypatch.setattr(agy_launch, "IS_WINDOWS", True)
    monkeypatch.setattr(agy_launch.shutil, "which", lambda _name: None)
    monkeypatch.setattr(agy_launch, "_AGY_FALLBACK_PATH", fake_home / ".local" / "bin" / "agy")
    monkeypatch.setattr(_platform.Path, "home", staticmethod(lambda: fake_home))

    # Empty OMNIGENT_CLI_FALLBACK_DIRS must not scan ~/.local/bin.
    with pytest.raises(RuntimeError, match="agy CLI not found"):
        agy_launch.agy_binary_path()

    # POSIX is_antigravity_acp_command keeps {"agy", "agy.exe"} and rejects .cmd.
    monkeypatch.setattr(_platform, "IS_WINDOWS", False)
    assert is_antigravity_acp_command("/usr/local/bin/agy acp") is True
    assert is_antigravity_acp_command("/usr/local/bin/agy.cmd acp") is False


def test_harness_install_forwards_pnpm_node_path_on_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnigent.onboarding import harness_install as hi

    entry = tmp_path / "pnpm" / "global" / "5" / "cli.js"
    entry.parent.mkdir(parents=True)
    entry.write_text("console.log('1.2.3');\n")
    shim = _write_pnpm_js_cmd(
        tmp_path / "pnpm" / "codex.cmd",
        r"global\5\cli.js",
        node_path=r"%~dp0\global\5\node_modules",
    )
    _write_fake_exe(tmp_path / "pnpm" / "node.exe")

    seen_envs: list[object] = []

    def fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        seen_envs.append(kwargs.get("env"))
        return SimpleNamespace(returncode=0, stdout=b"codex-cli 0.164.0\n", stderr=b"")

    monkeypatch.setenv("NODE_PATH", r"C:\user\modules")
    monkeypatch.setattr(_platform, "IS_WINDOWS", True)
    monkeypatch.setattr(hi, "IS_WINDOWS", True)
    monkeypatch.setattr(hi, "run_isolated", fake_run)
    monkeypatch.setattr(hi, "resolve_cli_binary", lambda _name: str(shim))
    hi.invalidate_harness_probe_caches()

    spec = hi.harness_install_spec("openai")
    assert spec is not None
    assert spec.status_args is not None
    assert hi._harness_cli_version_string(spec, str(shim)) == "0.164.0"
    assert isinstance(seen_envs[-1], dict)
    assert seen_envs[-1]["NODE_PATH"].startswith(r"C:\user\modules;")
    assert seen_envs[-1]["NODE_PATH"].endswith(r"global\5\node_modules")

    assert hi.harness_cli_logged_in("openai") is True
    assert isinstance(seen_envs[-1], dict)
    assert seen_envs[-1]["NODE_PATH"].endswith(r"global\5\node_modules")


def test_create_windows_junction_cleans_up_empty_dir_on_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import sys

    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"

    def _failing_create_junction(_src: str, dst: str) -> None:
        Path(dst).mkdir()
        raise OSError("reparse point failed")

    fake_winapi = SimpleNamespace(CreateJunction=_failing_create_junction)
    monkeypatch.setitem(sys.modules, "_winapi", fake_winapi)

    with pytest.raises(OSError, match="reparse point failed"):
        wl._create_windows_junction(target, link)
    assert not link.exists()


# ---------------------------------------------------------------------------
# Review R2 hardening: PATHEXT injection, PS1 template, hermetic env, quoting
# ---------------------------------------------------------------------------


def test_pathext_and_ps1_shim_parsers_reject_command_injection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    entry = tmp_path / "cli.js"
    entry.write_text("console.log('hi');\n")
    injected_cmd = tmp_path / "evil.cmd"
    injected_cmd.write_bytes(
        b'@SETLOCAL\r\n@SET PATHEXT=%PATHEXT:;.JS;=;% & exit /b 1\r\nnode "%~dp0\\cli.js" %*\r\n'
    )
    assert wl.unwrap_windows_cmd_shim(injected_cmd) is None

    monkeypatch.setattr(_platform, "IS_WINDOWS", True)
    native_claude = _write_fake_exe(
        tmp_path / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    )
    custom_ps1 = tmp_path / "claude.ps1"
    custom_ps1.write_text(
        "$basedir=Split-Path $MyInvocation.MyCommand.Definition -Parent\n"
        "exit 1\n"
        "$ret=0\n"
        '& "$basedir/node.exe" "$basedir/cli.js" $args\n'
        "exit $ret\n",
        encoding="utf-8",
    )
    assert wl.resolve_claude_windows_executable(str(custom_ps1)) == str(custom_ps1)

    valid_ps1 = tmp_path / "claude-valid.ps1"
    valid_ps1.write_text(
        "#!/usr/bin/env pwsh\n"
        "$basedir=Split-Path $MyInvocation.MyCommand.Definition -Parent\n"
        '$exe=""\n'
        'if ($PSVersionTable.PSVersion -lt "6.0" -or $IsWindows) { $exe=".exe" }\n'
        "$ret=0\n"
        '& "$basedir/node$exe" "$basedir/node_modules/@anthropic-ai/claude-code/cli.js" $args\n'
        "$ret=$LASTEXITCODE\n"
        "exit $ret\n",
        encoding="utf-8",
    )
    assert wl.resolve_claude_windows_executable(str(valid_ps1)) == str(native_claude.resolve())


def test_join_and_split_command_argv_windows_and_legacy_single_quotes() -> None:
    argv = [r"C:\Program Files\Antigravity\agy.exe", "acp", "--label=hello world"]
    serialized = wl.join_command_argv(argv, is_windows=True)
    assert wl.split_command_argv(serialized, is_windows=True) == argv
    # Legacy shlex.join-style single-quoted Windows path stays readable.
    assert wl.split_command_argv(
        r"'C:\Program Files\Antigravity\agy.exe' acp", is_windows=True
    ) == [r"C:\Program Files\Antigravity\agy.exe", "acp"]
    # Apostrophe inside a path stays literal.
    assert wl.split_command_argv(r"C:\Tools\O'Brien\agy.exe acp", is_windows=True) == [
        r"C:\Tools\O'Brien\agy.exe",
        "acp",
    ]


def test_resolve_windows_command_argv_is_hermetic_to_spawn_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    parent_dir = tmp_path / "parent"
    child_dir = tmp_path / "child"
    child_node_dir = tmp_path / "child-node"
    parent_node = _write_fake_exe(parent_dir / "node.exe")
    child_node = _write_fake_exe(child_node_dir / "node.exe")
    entry = child_dir / "global" / "5" / "cli.js"
    entry.parent.mkdir(parents=True)
    entry.write_text("console.log('child');\n")
    _write_pnpm_js_cmd(child_dir / "tool.cmd", r"global\5\cli.js")
    _write_fake_exe(parent_dir / "other.exe")

    monkeypatch.setenv("PATH", str(parent_dir))
    monkeypatch.setattr(wl, "_read_registry_path", lambda _scope: str(parent_dir))

    # Child env resolves Node from child env PATH (not parent PATH) and blocks registry/fallback.
    child_env = {
        "PATH": f"{child_dir};{child_node_dir}",
        "PATHEXT": ".EXE;.CMD",
        "OMNIGENT_CLI_FALLBACK_DIRS": "",
    }
    argv, _ = wl.resolve_windows_command_argv(["tool", "--flag"], env=child_env, is_windows=True)
    assert argv == [str(child_node), str(entry.resolve()), "--flag"]
    assert argv[0] != str(parent_node)

    # Unlisted parent binary is not leaked into hermetic child env.
    unresolved, _ = wl.resolve_windows_command_argv(["other"], env=child_env, is_windows=True)
    assert unresolved == ["other"]


def test_windows_short_temp_dir_survives_broken_gettempdir_and_pi_version_forwards_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnigent.harnesses.pi_native import main as pi_main

    local_appdata = tmp_path / "l"
    local_appdata.mkdir()

    def _broken_gettempdir() -> str:
        raise FileNotFoundError("no usable temp dir")

    monkeypatch.setattr(wl.tempfile, "gettempdir", _broken_gettempdir)
    created, _ = wl.windows_short_temp_dir("og-ag", env={"LOCALAPPDATA": str(local_appdata)})
    assert created.parent == local_appdata

    # pi_version forwards pnpm shim NODE_PATH when probing --version.
    entry = tmp_path / "pnpm" / "global" / "5" / "cli.js"
    entry.parent.mkdir(parents=True)
    entry.write_text("console.log('0.81.0');\n")
    shim = _write_pnpm_js_cmd(
        tmp_path / "pnpm" / "pi.cmd",
        r"global\5\cli.js",
        node_path=r"%~dp0\global\5\node_modules",
    )
    _write_fake_exe(tmp_path / "pnpm" / "node.exe")

    seen_env: list[object] = []

    def _fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        seen_env.append(kwargs.get("env"))
        return SimpleNamespace(returncode=0, stdout="0.81.0\n", stderr="")

    monkeypatch.setattr(_platform, "IS_WINDOWS", True)
    monkeypatch.setattr(pi_main, "IS_WINDOWS", True)
    import omnigent.harnesses.pi_cli as pi_cli

    monkeypatch.setattr(pi_cli, "IS_WINDOWS", True)
    monkeypatch.setattr("subprocess.run", _fake_run)
    assert pi_main.pi_version(str(shim)) == (0, 81, 0)
    assert isinstance(seen_env[-1], dict)
    assert seen_env[-1]["NODE_PATH"].endswith(r"global\5\node_modules")
