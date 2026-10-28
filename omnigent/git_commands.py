"""Snapshot protection for Git commands owned by Omnigent, not user/agent Git."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from subprocess import Popen
from typing import Any

_logger = logging.getLogger(__name__)

_FIXED_CONFIG = {
    "core.fsmonitor": "false",
    "core.hooksPath": os.devnull,
    "core.editor": "",
    "sequence.editor": "",
    "core.pager": "",
    "core.askPass": "",
    "core.sshCommand": "ssh -oBatchMode=yes",
    "core.gitProxy": "",
    "core.alternateRefsCommand": "",
    "credential.helper": "",
    "diff.external": "",
    "interactive.diffFilter": "",
    "gpg.program": "",
    "gpg.openpgp.program": "",
    "gpg.x509.program": "",
    "gpg.ssh.program": "",
    "gpg.ssh.defaultKeyCommand": "",
    "commit.gpgSign": "false",
    "tag.gpgSign": "false",
    "log.showSignature": "false",
    "gc.auto": "0",
    "maintenance.auto": "false",
    "submodule.recurse": "false",
    "fetch.recurseSubmodules": "false",
    "diff.ignoreSubmodules": "all",
    "status.submoduleSummary": "false",
}
_CALLBACK_KEY = re.compile(
    r"^(filter\..*\.(clean|smudge|process|required)|credential(\..*)?\.helper|"
    r"diff\..*\.(command|textconv)|include(if\..*)?\.path)$",
    re.IGNORECASE,
)
_VALUE_OPTIONS = {"-C", "-c", "--config-env", "--git-dir", "--work-tree", "--namespace"}
_DIFF_COMMANDS = {"diff", "show", "log", "diff-files", "diff-index", "diff-tree"}


def _global_args(args: Sequence[str]) -> tuple[list[str], list[str]]:
    index = 0
    while index < len(args) and args[index].startswith("-"):
        if args[index] in {"--version", "-v", "--help", "-h"}:
            break
        option = args[index]
        index += 2 if option in _VALUE_OPTIONS else 1
    return list(args[:index]), list(args[index:])


def _without_callbacks(args: Sequence[str]) -> list[str]:
    kept: list[str] = []
    index = 0
    fixed = {key.lower() for key in _FIXED_CONFIG}
    while index < len(args):
        option = args[index]
        value = None
        size = 1
        if option in {"-c", "--config-env"} and index + 1 < len(args):
            value, size = args[index + 1], 2
        elif option.startswith("-c") and len(option) > 2:
            value = option[2:]
        elif option.startswith("--config-env="):
            value = option.partition("=")[2]
        key = value.partition("=")[0] if value is not None else ""
        if key.lower() not in fixed and not _CALLBACK_KEY.fullmatch(key):
            kept.extend(args[index : index + size])
        index += size
    return kept


def _append_config(env: dict[str, str], entries: Sequence[tuple[str, str]]) -> None:
    try:
        count = int(env.get("GIT_CONFIG_COUNT", "0"))
    except ValueError as error:
        raise OSError("Invalid Git configuration count") from error
    if not 0 <= count <= 10000:
        raise OSError("Invalid Git configuration count")
    for key, value in entries:
        env[f"GIT_CONFIG_KEY_{count}"] = key
        env[f"GIT_CONFIG_VALUE_{count}"] = value
        count += 1
    env["GIT_CONFIG_COUNT"] = str(count)


def _config_snapshot(
    globals_: Sequence[str],
    cwd: str | os.PathLike[str] | None,
    env: Mapping[str, str],
    *,
    trusted_helpers: bool = False,
    deadline: float | None = None,
) -> bytes:
    query = ["git", "--no-pager", *globals_, "config", "--null"]
    query.extend(["--show-scope", "--no-includes"] if trusted_helpers else ["--name-only"])
    query.extend(
        [
            "--get-regexp",
            r"^credential(\..*)?\.helper$"
            if trusted_helpers
            else r"^filter\..*\.(clean|smudge|process|required)$",
        ]
    )
    timeout = min(10, deadline - time.monotonic()) if deadline is not None else 10
    if timeout <= 0:
        raise subprocess.TimeoutExpired(query, 0)
    with Popen(query, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        try:
            stdout, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
        if process.returncode not in (0, 1):
            raise OSError(
                f"Git callback configuration snapshot failed (exit {process.returncode})"
            )
    return stdout


def _prepare_git(
    args: Sequence[str],
    cwd: str | os.PathLike[str] | None,
    env: Mapping[str, str] | None,
    *,
    deadline: float | None = None,
) -> tuple[list[str], dict[str, str]]:
    if not args or args[0] != "git":
        raise ValueError("run_git expects argv beginning with git")
    globals_, command = _global_args(args[1:])
    child_env = {
        key.upper() if key.upper().startswith("GIT_") else key: value
        for key, value in (os.environ if env is None else env).items()
    }
    child_env.pop("GIT_CONFIG_PARAMETERS", None)
    child_env.update(
        GIT_PAGER="",
        GIT_EDITOR="",
        GIT_SEQUENCE_EDITOR="",
        GIT_ASKPASS="",
        SSH_ASKPASS="",
        SSH_ASKPASS_REQUIRE="never",
        GIT_TERMINAL_PROMPT="0",
        GIT_SSH_COMMAND="ssh -oBatchMode=yes",
        GIT_PROXY_COMMAND="",
        GIT_ALLOW_PROTOCOL="file:http:https:ssh:git",
        GIT_NO_LAZY_FETCH="1",
    )
    child_env.pop("GIT_EXTERNAL_DIFF", None)
    child_env.pop("GIT_EXEC_PATH", None)
    protocols = {"file", "http", "https", "ssh", "git"}
    configured = {}
    for index, option in enumerate(globals_):
        raw = (
            globals_[index + 1]
            if option == "-c" and index + 1 < len(globals_)
            else option[2:]
            if option.startswith("-c")
            else ""
        )
        key, _, value = raw.partition("=")
        if key.startswith("protocol."):
            configured[key] = value
    if configured.get("protocol.allow") == "never":
        protocols = {
            name for name in protocols if configured.get(f"protocol.{name}.allow") == "always"
        }
    protocols.difference_update(
        name for name in protocols.copy() if configured.get(f"protocol.{name}.allow") == "never"
    )
    if "GIT_ALLOW_PROTOCOL" in (os.environ if env is None else env):
        protocols.intersection_update(
            (os.environ if env is None else env)["GIT_ALLOW_PROTOCOL"].split(":")
        )
    child_env["GIT_ALLOW_PROTOCOL"] = ":".join(sorted(protocols))
    _append_config(child_env, list(_FIXED_CONFIG.items()))
    # Direct system/global helpers retain image and host authentication in Git order.
    # Local/worktree helpers and includes are not imported as trusted code.
    helpers = _config_snapshot(globals_, cwd, child_env, trusted_helpers=True, deadline=deadline)
    credentials = []
    records = helpers.decode("utf-8", errors="surrogateescape").split("\0")
    if records.pop() != "" or len(records) % 2:
        raise OSError("Invalid Git credential configuration snapshot")
    for scope, record in zip(records[::2], records[1::2], strict=True):
        if scope not in {"system", "global"}:
            continue
        key, separator, value = record.partition("\n")
        if not separator:
            raise OSError("Invalid Git credential configuration snapshot")
        credentials.append((key, value))
    _append_config(child_env, credentials)
    keys = _config_snapshot(globals_, cwd, child_env, deadline=deadline)
    drivers = {
        key.rsplit(".", 1)[0]
        for key in keys.decode("utf-8", errors="surrogateescape").split("\0")
        if key
    }
    _append_config(
        child_env,
        [
            (f"{driver}.{name}", "false" if name == "required" else "")
            for driver in sorted(drivers)
            for name in ("clean", "smudge", "process", "required")
        ],
    )
    # Snapshot only: a driver added after enumeration but before Git reads config
    # can still run. This does not protect the user's editor or their own Git.
    if command and command[0] in _DIFF_COMMANDS:
        command[1:1] = ["--no-ext-diff", "--no-textconv"]
    if command and command[0] in {"fetch", "clone", "ls-remote"}:
        command.insert(1, "--upload-pack=git-upload-pack")
    if command and command[0] == "push":
        command.insert(1, "--receive-pack=git-receive-pack")
    return ["git", *_without_callbacks(globals_), *command], child_env


def run_git(
    args: Sequence[str],
    *,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    **kwargs: Any,
) -> subprocess.CompletedProcess[Any]:
    """Run one internal Git command after a fresh callback configuration snapshot."""
    raw_timeout = kwargs.get("timeout")
    timeout = float(raw_timeout) if raw_timeout is not None else None
    deadline = time.monotonic() + timeout if timeout is not None else None
    text_mode = bool(
        kwargs.get("text")
        or kwargs.get("universal_newlines")
        or kwargs.get("encoding")
        or kwargs.get("errors")
    )
    globals_, command = _global_args(args[1:])
    destination = None
    if command[:2] == ["worktree", "remove"] and not any(
        arg in {"--force", "-f"} for arg in command[2:]
    ):
        target = _destination(command[2:], set())
        # Git removes a worktree after running status in that tree's config context.
        target = _absolute_target(target, globals_, cwd)
        _, env = _prepare_git(
            ["git", "-C", target, "status"], None, _destination_env(env), deadline=deadline
        )
    elif command[:2] == ["worktree", "add"] and not any(
        arg in {"--no-checkout", "-n"} for arg in command[2:]
    ):
        target = _destination(command[2:], {"-b", "-B", "--reason"})
        destination = _absolute_target(target, globals_, cwd)
        args = ["git", *globals_, "worktree", "add", "--no-checkout", *command[2:]]
    elif command[:1] == ["clone"] and not any(
        arg in {"--no-checkout", "-n", "--bare", "--mirror"} for arg in command[1:]
    ):
        # Omnigent clone boundaries supply an explicit destination as the last operand.
        operands = _operands(
            command[1:],
            {
                "-b",
                "--branch",
                "--depth",
                "--filter",
                "-o",
                "--origin",
                "--template",
                "--config",
                "-c",
                "--upload-pack",
                "--reference",
                "--reference-if-able",
                "--separate-git-dir",
                "--shallow-since",
                "--shallow-exclude",
                "-j",
                "--jobs",
            },
        )
        if len(operands) != 2:
            raise OSError("Guarded clone requires an explicit repository and destination")
        destination = _absolute_target(operands[-1], globals_, cwd)
        args = ["git", *globals_, "clone", "--no-checkout", *command[1:]]
    argv, child_env = _prepare_git(args, cwd, env, deadline=deadline)
    if deadline is not None and timeout is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(args, timeout)
        kwargs["timeout"] = remaining
    result = subprocess.run(argv, cwd=cwd, env=child_env, **kwargs)
    if result.returncode == 0 and destination is not None:
        # Conditional includes and config.worktree can expose additional drivers here.
        if deadline is not None:
            kwargs["timeout"] = max(0, deadline - time.monotonic())
        if command[:1] == ["clone"]:
            probe_kwargs = {**kwargs, "check": False, "capture_output": True}
            probe_kwargs.pop("stdout", None)
            probe_kwargs.pop("stderr", None)
            head = run_git(
                ["git", "rev-parse", "--verify", "--quiet", "HEAD"],
                cwd=destination,
                env=_destination_env(child_env),
                **probe_kwargs,
            )
            if head.returncode == 1:
                return result  # Cloning an empty repository has no checkout.
            head.check_returncode()
            if deadline is not None:
                kwargs["timeout"] = max(0, deadline - time.monotonic())
        try:
            _materialize_checkout(destination, _destination_env(child_env), deadline)
            if deadline is not None:
                kwargs["timeout"] = max(0, deadline - time.monotonic())
            checkout = run_git(
                ["git", "reset", "--hard", "HEAD"],
                cwd=destination,
                env=_destination_env(child_env),
                **kwargs,
            )
        except subprocess.CalledProcessError as error:
            _rollback_worktree(command, globals_, destination, cwd, env, error, text=text_mode)
            if kwargs.get("check"):
                raise
            output, stderr = error.output, error.stderr
            if kwargs.get("text") or kwargs.get("universal_newlines") or kwargs.get("encoding"):
                encoding = kwargs.get("encoding") or "utf-8"
                errors = kwargs.get("errors") or "replace"
                output = output.decode(encoding, errors) if isinstance(output, bytes) else output
                stderr = stderr.decode(encoding, errors) if isinstance(stderr, bytes) else stderr
            return subprocess.CompletedProcess(args, error.returncode, output, stderr)
        except Exception as error:
            _rollback_worktree(command, globals_, destination, cwd, env, error, text=text_mode)
            raise
        if checkout.returncode != 0:
            _rollback_worktree(command, globals_, destination, cwd, env, checkout, text=text_mode)
        for stream in ("stdout", "stderr"):
            before, after = getattr(result, stream), getattr(checkout, stream)
            if before is not None and after is not None:
                setattr(checkout, stream, before + after)
        return checkout
    return result


def _materialize_checkout(
    destination: str, env: Mapping[str, str], deadline: float | None
) -> None:
    """Fetch missing checkout blobs explicitly; ordinary reads never lazy-fetch."""

    def read(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return run_git(
            ["git", *args],
            cwd=destination,
            env=env,
            capture_output=True,
            check=True,
            timeout=max(0, deadline - time.monotonic()) if deadline is not None else None,
            **kwargs,
        )

    tree = read(["ls-tree", "-r", "-z", "HEAD"]).stdout
    blobs = sorted(
        {
            fields[2]
            for record in tree.split(b"\0")
            if record
            for fields in [record.partition(b"\t")[0].split()]
            if len(fields) == 3 and fields[1] == b"blob"
        }
    )
    if not blobs:
        return
    objects = read(["cat-file", "--batch-check"], input=b"\n".join(blobs) + b"\n")
    missing = [
        line.split()[0].decode("ascii")
        for line in objects.stdout.splitlines()
        if line.endswith(b" missing")
    ]
    if not missing:
        return
    remotes = read(["config", "--get-regexp", r"^remote\..*\.promisor$"]).stdout
    names = [
        match.group(1).decode("utf-8", errors="surrogateescape")
        for line in remotes.splitlines()
        if (
            match := re.fullmatch(
                rb"remote\.(.+)\.promisor\s+(?:true|yes|on|1)", line, re.IGNORECASE
            )
        )
    ]
    if not names:
        raise OSError("Checkout objects missing without a promisor remote")
    for index in range(0, len(missing), 128):
        read(
            [
                "fetch",
                "--no-tags",
                "--no-write-fetch-head",
                "--no-recurse-submodules",
                "--no-filter",
                "--",
                names[0],
                *missing[index : index + 128],
            ]
        )


def _rollback_worktree(
    command: Sequence[str],
    globals_: Sequence[str],
    destination: str,
    cwd: str | os.PathLike[str] | None,
    env: Mapping[str, str] | None,
    failure: subprocess.CompletedProcess[Any] | Exception,
    *,
    text: bool,
) -> None:
    if command[:2] != ["worktree", "add"]:
        return
    # The successful first phase created this registration; preserve existing branches.
    try:
        run_git(
            ["git", *globals_, "worktree", "remove", "--force", destination],
            cwd=cwd,
            env=env,
            capture_output=True,
            check=True,
            timeout=10,
        )
        if "-b" in command:
            branch = command[command.index("-b") + 1]
            run_git(
                ["git", *globals_, "branch", "-D", "--", branch],
                cwd=cwd,
                env=env,
                capture_output=True,
                check=True,
                timeout=10,
            )
    except (OSError, subprocess.SubprocessError) as cleanup_error:
        if isinstance(cleanup_error, subprocess.CalledProcessError):
            reason = f"Git exit {cleanup_error.returncode}"
        elif isinstance(cleanup_error, subprocess.TimeoutExpired):
            reason = "timed out"
        else:
            reason = "could not start Git"
        detail = f"Worktree rollback incomplete ({reason})"
        _logger.warning("%s", detail)
        if isinstance(failure, Exception):
            failure.add_note(detail)
        if isinstance(failure, subprocess.TimeoutExpired):
            failure.stderr = (failure.stderr or b"") + f"\n{detail}\n".encode()
        elif isinstance(failure, (subprocess.CompletedProcess, subprocess.CalledProcessError)):
            stderr = failure.stderr
            if isinstance(stderr, bytes) or (stderr is None and not text):
                failure.stderr = (stderr or b"") + f"\n{detail}\n".encode()
            else:
                failure.stderr = (stderr or "") + f"\n{detail}\n"


def _operands(args: Sequence[str], value_options: set[str]) -> list[str]:
    operands = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            operands.extend(args[index + 1 :])
            break
        if arg in value_options:
            index += 2
            continue
        if not arg.startswith("-"):
            operands.append(arg)
        index += 1
    return operands


def _destination(args: Sequence[str], value_options: set[str]) -> str:
    operands = _operands(args, value_options)
    if not operands:
        raise OSError("Guarded worktree operation requires an explicit destination")
    return operands[0]


def _absolute_target(
    target: str, globals_: Sequence[str], cwd: str | os.PathLike[str] | None
) -> str:
    base = Path(cwd or os.getcwd()).absolute()
    for index, arg in enumerate(globals_):
        if arg == "-C" and index + 1 < len(globals_):
            base = base / globals_[index + 1]
    return str(base / target)


def _destination_env(env: Mapping[str, str] | None) -> dict[str, str]:
    redirects = {"GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE"}
    return {
        key: value
        for key, value in (os.environ if env is None else env).items()
        if key.upper() not in redirects
    }


def git_shell_argv(args: Sequence[str]) -> list[str]:
    """Defer the same guard to the sandbox where a generated shell command runs."""
    return ["python3", "-I", "-m", "omnigent.git_commands", "--", *args]


if __name__ == "__main__":
    arguments = sys.argv[1:]
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    sys.exit(run_git(["git", *arguments]).returncode)
