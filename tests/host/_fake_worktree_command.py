"""Fake external worktree command for the host worktree tests.

Standalone script (not a pytest module): the producers spawn it as
``[sys.executable, <this file>, "--fake-record=<file>", "--fake-behave=<mode>"]``
plus Omnigent's own flags. It records its argv (after the fake flags) and
whether any host-token spelling is in the environment, then emulates the
collab kit's JSON envelope for the requested behave mode.

Behave modes: ``ok`` (create a worktree per mode), ``refuse:<CODE>``,
``exit1``, ``garbage``, ``nopath``, ``outside``, ``exists-source`` (an
``EXISTS`` refusal whose ``detail.path`` is the source), ``adopt-source``
(checkout the new branch INSIDE ``--source`` and report it), ``sleep``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

_HOST_TOKEN_NAMES = ("OMNIGENT_HOST_TOKEN", "OMNIGENTS_HOST_TOKEN", "OMNIAGENTS_HOST_TOKEN")


def _git(path: str, *args: str) -> str:
    """Run git in ``path``; return stdout, or ``""`` on failure."""
    result = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def _main_worktree(source: str) -> str:
    """Return the first (main) worktree path git reports for ``source``."""
    result = subprocess.run(
        ["git", "-C", source, "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"not a git repository: {source}")
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            return line[len("worktree ") :].strip()
    raise RuntimeError(f"no worktree listed for {source}")


def _succeed(flags: dict[str, str], *, path: str) -> None:
    """Print a success envelope for ``path``."""
    print(
        json.dumps(
            {
                "ok": True,
                "result": {
                    "path": path,
                    "entry": flags.get("entry"),
                    "repo": flags.get("source"),
                    "topic": flags.get("topic"),
                    "branch": flags.get("new-branch") or flags.get("branch") or None,
                    "head": _git(path, "rev-parse", "HEAD") or None,
                    "ignored": False,
                },
                "error": None,
            }
        )
    )


def _refuse(code: str, detail: dict[str, object]) -> None:
    """Print a refusal envelope for ``code``."""
    print(
        json.dumps(
            {
                "ok": False,
                "result": None,
                "error": {
                    "code": code,
                    "message": f"worktree command refused: {code}",
                    "hint": None,
                    "detail": detail,
                },
            }
        )
    )


def _ok(flags: dict[str, str]) -> int:
    """Emulate the kit: derive the path, then ``git worktree add`` per mode."""
    source = flags.get("source", "")
    topic = flags.get("topic", "")
    try:
        main = _main_worktree(source)
    except RuntimeError as exc:
        _refuse("NOT_A_REPO", {"source": source, "error": str(exc)})
        return 4
    entry = flags.get("entry") or main
    path = os.path.join(entry, ".worktrees", os.path.basename(main), topic.replace("/", "-"))
    if os.path.exists(path):
        _refuse(
            "EXISTS",
            {
                "path": path,
                "head": _git(path, "rev-parse", "HEAD") or None,
                "branch": flags.get("new-branch") or flags.get("branch"),
            },
        )
        return 4
    argv = ["-C", main, "worktree", "add"]
    if "new-branch" in flags:
        argv += ["-b", flags["new-branch"], path]
        if "base" in flags:
            argv.append(flags["base"])
    elif "branch" in flags:
        argv += [path, flags["branch"]]
    elif "detach" in flags:
        argv += ["--detach", path, flags["detach"]]
    else:
        _refuse("GIT_REFUSED", {"reason": "no mode flag given"})
        return 4
    added = subprocess.run(["git", *argv], capture_output=True, text=True)
    if added.returncode != 0:
        _refuse("GIT_REFUSED", {"path": path, "error": added.stderr.strip()})
        return 4
    _succeed(flags, path=path)
    return 0


def _act(behave: str, flags: dict[str, str]) -> int:
    """Dispatch on the requested behave mode."""
    if behave == "sleep":
        time.sleep(30)
        return 0
    if behave == "exit1":
        print("fake worktree command exploded", file=sys.stderr)
        return 1
    if behave == "garbage":
        print("not json")
        return 0
    if behave == "nopath":
        _succeed(flags, path=os.path.join(flags.get("source", ""), "no-such-worktree"))
        return 0
    if behave == "outside":
        _succeed(flags, path=flags.get("source", ""))
        return 0
    if behave == "adopt-source":
        source = flags.get("source", "")
        branch = flags.get("new-branch") or flags.get("branch") or ""
        switched = subprocess.run(
            ["git", "-C", source, "checkout", "-b", branch], capture_output=True, text=True
        )
        if switched.returncode != 0:
            _refuse("GIT_REFUSED", {"path": source, "error": switched.stderr.strip()})
            return 4
        _succeed(flags, path=source)
        return 0
    if behave == "exists-source":
        source = flags.get("source", "")
        _refuse(
            "EXISTS",
            {
                "path": source,
                "head": _git(source, "rev-parse", "HEAD") or None,
                "branch": flags.get("new-branch") or flags.get("branch"),
            },
        )
        return 4
    if behave.startswith("refuse:"):
        _refuse(behave.split(":", 1)[1], {"source": flags.get("source")})
        return 4
    return _ok(flags)


def main() -> int:
    """Parse the fake and Omnigent flags, record the invocation, then act."""
    record_path: str | None = None
    behave = "ok"
    flags: dict[str, str] = {}
    passed: list[str] = []
    for arg in sys.argv[1:]:
        if arg.startswith("--fake-record="):
            record_path = arg.split("=", 1)[1]
        elif arg.startswith("--fake-behave="):
            behave = arg.split("=", 1)[1]
        else:
            passed.append(arg)
            if arg.startswith("--") and "=" in arg:
                key, value = arg[2:].split("=", 1)
                flags[key] = value
    if record_path is not None:
        with open(record_path, "a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "argv": passed,
                        "host_token_present": any(
                            name in os.environ for name in _HOST_TOKEN_NAMES
                        ),
                    }
                )
                + "\n"
            )
    return _act(behave, flags)


if __name__ == "__main__":
    sys.exit(main())
