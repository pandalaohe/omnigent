"""One-command, serial phone-terminal proof with an isolated runtime.

Use ``--plan`` to inspect the command without starting a build or any process.
After the host's test slot is available, omit it to build and run once. The
script never retries, installs harness CLIs, enables admin access or runs sudo.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

CHECKOUT = Path(__file__).resolve().parents[3]
TESTS = [
    "tests/e2e_ui/mobile/test_terminal_keys.py",
    "tests/e2e_ui/mobile/test_terminal_keys_categories.py",
]
CATEGORIES = {
    "shells": "Session shell · zsh / bash",
    "fullscreen": "Full-screen · vim / less",
    "claude": "Claude Code",
    "codex": "Codex",
    "opencode": "OpenCode",
    "pi": "Pi",
    "antigravity": "Antigravity",
    "sdk_tui": "SDK tui",
    "declared": "Agent-declared terminal",
    "host_shell": "Host Shell",
    "host_admin": "Host Admin",
    "read_only": "Read-only watching",
    "agent_user_shell": "Agent on a user shell",
    "other_clis": "Cursor / Kiro / Goose / Qwen / Kimi / Devin / Hermes",
}
NATIVE_PROOF_LIMIT = (
    "CLI menu opened and screen changes were observed; selected-command and mode changes "
    "were not independently identified"
)


def cache_signature(root: Path) -> str:
    """Compare browser-cache metadata without opening its files."""
    entries = []
    for directory, names, files in os.walk(root, followlinks=False):
        names[:] = sorted(name for name in names if not name.endswith((".secret", ".se")))
        for name in sorted(files):
            if name.endswith((".secret.md", ".se.md")):
                continue
            path = Path(directory) / name
            stat = path.lstat()
            entries.append((str(path.relative_to(root)), stat.st_size, stat.st_mtime_ns))
    return hashlib.sha256(json.dumps(entries).encode()).hexdigest()


def retain_observations(scratch: Path, output: Path) -> None:
    """Keep only the named test observations, not runtime config or credential files."""
    names = {
        "agent-user-shell.json",
        "normal.log",
        "app.log",
        "zsh-normal.log",
        "bash-app.log",
        "vim_out.txt",
    }
    for case in scratch.glob("test_*"):
        if not case.is_dir() or case.is_symlink():
            continue
        for path in case.iterdir():
            if (
                path.is_file()
                and not path.is_symlink()
                and (
                    path.name in names
                    or path.name.endswith("-menu.txt")
                    or path.name.endswith("-startup.json")
                )
            ):
                target = output / "observations" / case.name / path.name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)


def summarize(junit: Path) -> list[dict[str, object]]:
    """Do not promote missing, skipped or limited evidence to a terminal pass."""
    groups: dict[str, list[dict[str, str]]] = {key: [] for key in CATEGORIES}
    if junit.exists():
        for case in ET.parse(junit).iter("testcase"):
            properties = {
                row.attrib["name"]: row.attrib.get("value", "")
                for row in case.findall("properties/property")
            }
            category = properties.get("proof_category")
            targets = [category] if category in groups else []
            name = case.attrib.get("name", "")
            if not targets:
                for family in ("claude", "codex", "opencode", "pi", "antigravity"):
                    if name.startswith("test_native_phone_menu_keys[") and f"-{family}-" in name:
                        targets = [family]
                for prefix, fallback in (
                    ("test_declared_terminal_phone_bytes", "declared"),
                    ("test_sdk_tui_phone_transport_bytes", "sdk_tui"),
                    ("test_reader_has_chat_wheel", "read_only"),
                    ("test_agent_can_use_plain_user_shell", "agent_user_shell"),
                ):
                    if name.startswith(prefix):
                        targets = [fallback]
                if name.startswith("test_real_host_phone_bytes"):
                    targets = ["host_admin" if "-admin]" in name else "host_shell"]
            if name.startswith("test_mobile_terminal_keys_byte_delivery"):
                targets = ["shells", "fullscreen"]
            state, reason = "pass", "Recorded test completed"
            for tag, status in (("error", "fail"), ("failure", "fail"), ("skipped", "not-run")):
                node = case.find(tag)
                if node is not None:
                    state = status
                    reason = node.attrib.get("message") or (node.text or "").strip()
                    if tag == "skipped" and "BLOCKED:" in reason:
                        state = "blocked"
                    elif tag == "skipped" and "UNVERIFIED:" in reason:
                        state = "unverified"
                    break
            if state == "pass" and properties.get("proof_limit"):
                state, reason = "partial", properties["proof_limit"]
            if state == "pass" and name.startswith("test_native_phone_menu_keys["):
                state, reason = "partial", NATIVE_PROOF_LIMIT
            for target in targets:
                groups[target].append(
                    {"state": state, "reason": reason, "test": case.attrib.get("name", "")}
                )
    priority = {"fail": 0, "blocked": 1, "unverified": 2, "partial": 3, "not-run": 4, "pass": 5}
    rows = []
    for category, title in CATEGORIES.items():
        cases = groups[category]
        worst = min(cases, key=lambda case: priority[case["state"]]) if cases else None
        rows.append(
            {
                "id": category,
                "title": title,
                "state": worst["state"] if worst else "not-run",
                "reason": worst["reason"]
                if worst
                else (
                    "Outside this run's real-CLI subset; no claim of installation or verification"
                    if category == "other_clis"
                    else "No completed test result"
                ),
                "tests": cases,
            }
        )
    return rows


def run_stage(argv: list[str], *, env: dict[str, str], log: Path, timeout: int) -> int:
    """Each stage owns a process group; timeout signals only that group."""
    with log.open("wb") as output:
        try:
            proc = subprocess.Popen(
                argv,
                cwd=CHECKOUT,
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            output.write(f"stage could not start: {exc}\n".encode())
            return 127
        try:
            return proc.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            if proc.poll() is None:
                if os.name == "posix":
                    os.killpg(proc.pid, signal.SIGTERM)
                else:
                    proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    if os.name == "posix":
                        os.killpg(proc.pid, signal.SIGKILL)
                    else:
                        proc.kill()
                    proc.wait(timeout=5)
            return 124


def cleanup_runtime(
    root: Path, python: Path, env: dict[str, str], *, interrupted: bool = False
) -> dict[str, object]:
    """Reap this run's attributed daemons after pytest exits, including hard exits."""
    directories = [
        root,
        root / "u" / ".omnigent",
        *(root / "t").glob("omnigent-pytest-*"),
        *(root / "p").glob("test_real_host_phone_bytes*/host/home/.omnigent"),
    ]
    code = """
import json, sys
from omnigent.testing.process_reaper import (
    ProcessScanUnavailable, reap_leaked_omnigent_processes,
)
reaped = survivors = 0
try:
    for directory in sys.argv[1:]:
        gone, alive = reap_leaked_omnigent_processes(directory, timeout=3)
        reaped += len(gone)
        survivors += len(alive)
except ProcessScanUnavailable:
    print(json.dumps({"state": "unverified", "reason": "process scan unavailable"}))
else:
    print(json.dumps({"state": "fail" if survivors else "pass", "reaped": reaped,
                      "survivors": survivors}))
"""
    try:
        result = subprocess.run(
            [str(python), "-c", code, *map(str, directories)],
            cwd=CHECKOUT,
            env=env,
            capture_output=True,
            text=True,
            timeout=45,
        )
        if result.returncode:
            return {"state": "unverified", "reason": "independent reaper failed"}
        cleanup = json.loads(result.stdout)
        if interrupted and cleanup["state"] == "pass":
            cleanup.update(
                state="unverified",
                reason=(
                    "Attributed daemons reaped; interrupted pytest terminal/tmux teardown "
                    "unverified"
                ),
            )
        return cleanup
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {"state": "unverified", "reason": "independent reaper unavailable"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--plan", action="store_true", help="print the run plan without changing files"
    )
    parser.add_argument("--evidence", type=Path, default=CHECKOUT / "test-results/terminal-proof")
    parser.add_argument(
        "--skip-build", action="store_true", help="reuse this checkout's already built SPA"
    )
    parser.add_argument(
        "--acceptance-entry", type=Path, help="update proof-data.ts in this local acceptance entry"
    )
    parser.add_argument(
        "--timeout", type=int, default=1200, help="total wall-clock budget in seconds"
    )
    args = parser.parse_args()
    if args.timeout < 1:
        parser.error("--timeout must be positive")
    node = Path("/opt/homebrew/opt/node@24/bin/node")
    if not node.is_file():
        node = Path(shutil.which("node") or "node")
    python = CHECKOUT / ".venv/bin/python"
    commands = []
    if not python.is_file():
        commands.append(["uv", "sync", "--locked", "--extra", "all", "--group", "test"])
    if not args.skip_build:
        commands.extend(
            [
                ["pnpm", "install", "--offline", "--frozen-lockfile", "--filter", "web"],
                ["pnpm", "--filter", "web", "run", "build"],
            ]
        )
    pytest_args = [
        str(python),
        "-m",
        "pytest",
        *TESTS,
        "--ui-skip-build",
        "--video=on",
        "--screenshot=only-on-failure",
        "--reruns=0",
        "-n=0",
        "-q",
        "-o",
        "junit_family=xunit1",
    ]
    if args.plan:
        print(
            json.dumps(
                {
                    "checkout": str(CHECKOUT),
                    "commands": [*commands, pytest_args],
                    "total_timeout_s": args.timeout,
                    "retries": 0,
                    "isolation": (
                        "throwaway HOME/temp; no runner descriptors; existing browser cache"
                    ),
                    "unverified": (
                        "SDK PTY-reader bytes; Admin root; physical keyboard/IME; other CLIs"
                    ),
                },
                indent=2,
            )
        )
        return 0
    args.evidence = args.evidence.resolve()
    if not args.evidence.is_relative_to(CHECKOUT):
        parser.error("--evidence must stay inside this checkout")
    if args.acceptance_entry and not args.acceptance_entry.resolve().is_relative_to(CHECKOUT):
        parser.error("--acceptance-entry must stay inside this checkout")
    version = subprocess.check_output(
        [str(node), "-p", "process.versions.node"], text=True
    ).strip()
    if int(version.split(".")[0]) != 24:
        parser.error(f"Node 24 is required; found {version}")
    browser_cache = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if not browser_cache:
        browser_cache = str(
            Path.home()
            / (
                "Library/Caches/ms-playwright"
                if sys.platform == "darwin"
                else ".cache/ms-playwright"
            )
        )
    if not Path(browser_cache).is_dir():
        parser.error(
            "the existing Playwright browser cache is missing; this script does not install it"
        )
    pnpm_store = subprocess.check_output(
        ["pnpm", "store", "path"],
        env={**os.environ, "PATH": str(node.parent) + os.pathsep + os.environ.get("PATH", "")},
        cwd=CHECKOUT,
        text=True,
    ).strip()
    # Use the already cached, project-pinned engine inside the throwaway HOME.
    wanted_pnpm = json.loads((CHECKOUT / "package.json").read_text())["packageManager"]
    pnpm_version = wanted_pnpm.removeprefix("pnpm@").split("+")[0]
    pinned = sorted(Path(pnpm_store).glob(f"links/@/pnpm/{pnpm_version}/*/bin/pnpm"))
    if not pinned:
        parser.error(f"project-pinned {wanted_pnpm} is not cached in the existing pnpm store")
    pnpm = pinned[0]
    for command in commands:
        if command[0] == "pnpm":
            command[0] = str(pnpm)
    uv_cache = str(Path.home() / ".cache/uv")
    args.evidence.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix="run-", dir=args.evidence.resolve()))
    junit = output / "junit.xml"
    start = time.monotonic()
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=CHECKOUT, text=True
    ).strip()
    sources = [*TESTS, str(Path(__file__).relative_to(CHECKOUT))]
    source_hashes = {
        name: hashlib.sha256((CHECKOUT / name).read_bytes()).hexdigest() for name in sources
    }
    stages = []
    exit_code = 0
    cache_before = cache_signature(Path(browser_cache))
    temp_parent = "/private/tmp" if Path("/private/tmp").is_dir() else tempfile.gettempdir()
    root = Path(tempfile.mkdtemp(prefix="pkp.", dir=temp_parent))
    try:
        proof_home = root / "u"
        proof_home.mkdir()
        scratch = root / "t"
        scratch.mkdir()
        env = {
            key: value
            for key, value in os.environ.items()
            if key
            in {
                "PATH",
                "LANG",
                "LC_ALL",
                "SYSTEMROOT",
                "WINDIR",
                "COMSPEC",
                "PATHEXT",
            }
        }
        env.update(
            {
                "PATH": str(node.parent)
                + os.pathsep
                + str(pnpm.parent)
                + os.pathsep
                + str(python.parent)
                + os.pathsep
                + env.get("PATH", ""),
                "HOME": str(proof_home),
                "TMPDIR": str(scratch),
                "PLAYWRIGHT_BROWSERS_PATH": browser_cache,
                "PLAYWRIGHT_SKIP_BROWSER_GC": "1",
                "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1",
                "npm_config_store_dir": pnpm_store,
                "UV_CACHE_DIR": uv_cache,
                "OMNIGENT_SKIP_WEB_UI": "true",
                "OMNIGENT_TERMINAL_PROOF": "1",
                "OMNIGENT_LOCAL_SINGLE_USER": "1",
                "OMNIGENT_RUNNER_HOST_OWNS_GLOBAL_CLEANUP": "1",
                "OMNIGENT_DISABLE_CATALOG_LOOKUP": "1",
                "OMNIGENT_DISABLE_TELEMETRY": "1",
                "OMNIGENT_TEST_ORPHAN_SWEEP": "0",
                "PYTHONPATH": str(CHECKOUT),
                "OMNIGENT_TERMINAL_KEYS_VIDEO_DIR": str(output / "videos"),
            }
        )
        # No ambient provider credentials, config home or runner identity enters the run.
        commands.append(
            [
                *pytest_args,
                "--basetemp",
                str(root / "p"),
                "--output",
                str(output / "playwright"),
                "--junitxml",
                str(junit),
            ]
        )
        for index, command in enumerate(commands):
            remaining = args.timeout - int(time.monotonic() - start)
            log = output / f"stage-{index}.log"
            exit_code = (
                run_stage(command, env=env, log=log, timeout=max(1, remaining))
                if remaining > 0
                else 124
            )
            stages.append({"command": command, "exit": exit_code, "log": log.name})
            print(f"stage {index}: exit={exit_code}; log={log}", flush=True)
            if exit_code:
                break
    finally:
        retain_observations(root / "p", output)
    cleanup = cleanup_runtime(
        root,
        python,
        env,
        interrupted=any(stage["exit"] != 0 for stage in stages),
    )
    if cleanup["state"] != "pass":
        exit_code = exit_code or 1
    cache_after = cache_signature(Path(browser_cache))
    cache_unchanged = cache_before == cache_after
    if not cache_unchanged:
        exit_code = exit_code or 1
    retained_runtime = str(root) if exit_code else None
    if not exit_code:
        shutil.rmtree(root)
    report = {
        "revision": revision,
        "source_sha256": source_hashes,
        "node": version,
        "pnpm": wanted_pnpm,
        "platform": platform.platform(),
        "exit": exit_code,
        "elapsed_s": round(time.monotonic() - start, 2),
        "stages": stages,
        "browser_cache_unchanged": cache_unchanged,
        "cleanup": cleanup,
        "retained_runtime": retained_runtime,
        "rows": summarize(junit),
        "limits": [
            "Headless touch emulation; no physical keyboard or IME proof",
            "Admin capability is not enabled by this run",
            "SDK TUI frame comparison is not a PTY-reader proof",
        ],
    }
    receipt = output / "receipt.json"
    receipt.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.acceptance_entry:
        entry = args.acceptance_entry.resolve()
        if not entry.is_relative_to(CHECKOUT):
            parser.error("--acceptance-entry must stay inside this checkout")
        (entry / "proof-data.ts").write_text(
            'import type { ProofReceipt } from "./proof-types";\n\n'
            + "export const proof: ProofReceipt = "
            + json.dumps(report, ensure_ascii=False, indent=2)
            + ";\n",
            encoding="utf-8",
        )
    print(f"receipt: {receipt}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
