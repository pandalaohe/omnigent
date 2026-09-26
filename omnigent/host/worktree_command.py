"""Run the configured external command that creates linked git worktrees.

``host.worktree_add_command`` in the host's ``config.yaml`` names an external
command that creates a linked git worktree. When it is set, both worktree
producers — session worktrees (:mod:`omnigent.host.git_worktree`) and
assignment execution roots (:mod:`omnigent.host.assignment_workspace`) — call
it instead of computing a path and running ``git worktree add`` themselves.
A configured command that fails in any way refuses the worktree; the built-in
layout is never used as a fallback.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import IO

from omnigent._env_compat import _LEGACY_PREFIXES, _NEW_PREFIX
from omnigent.host.git_worktree import WorktreeError, _contained_inside
from omnigent.host.identity import HOST_TOKEN_ENV_VAR
from omnigent.host.post_bind_hook import _CommandConfigError, _load_command

# Wall-clock cap on one command run; a module constant so tests can lower it.
WORKTREE_COMMAND_TIMEOUT_S: float = 120.0

# The command's stdout is one JSON envelope, kept well under a pipe-buffer
# scale; the failure path only needs a short tail of stderr.
_STDOUT_READ_BYTES = 64 * 1024
_ERROR_TAIL_CHARS = 500
_ERROR_TAIL_BYTES = 4 * _ERROR_TAIL_CHARS


class WorktreeCommandError(WorktreeError):
    """Raised when the configured worktree command cannot produce a worktree.

    The message is user-facing (``.message``); ``code`` is the machine-readable
    category from the loader/envelope (``config_invalid``, ``unavailable``,
    ``timeout``, ``malformed``, ``failed``, or an opaque command code such as
    ``EXISTS``).

    :param message: Human-readable failure reason.
    :param code: Stable failure category, e.g. ``"timeout"``.
    :param detail: Envelope ``error.detail`` when the command refused.
    """

    def __init__(
        self, message: str, *, code: str, detail: dict[str, object] | None = None
    ) -> None:
        """Initialize with the user-facing message and failure category.

        :param message: Error string surfaced to the API caller.
        :param code: Machine-readable failure category.
        :param detail: Optional envelope detail dict.
        """
        super().__init__(message)
        self.code = code
        self.detail = detail


def load_worktree_command(config_path: Path) -> list[str] | None:
    """Read ``host.worktree_add_command`` from the host's config file.

    :param config_path: The host's startup config path.
    :returns: The argv list, or ``None`` when the file or key is absent.
    :raises WorktreeCommandError: With code ``"config_invalid"`` when the key
        is present but not a usable argv list (see ``_load_command``).
    """
    try:
        return _load_command(config_path, key="worktree_add_command")
    except _CommandConfigError as exc:
        raise WorktreeCommandError(str(exc), code="config_invalid") from exc


def run_worktree_command(
    command: list[str],
    *,
    source: str,
    topic: str,
    entry: str | None,
    mode: list[str],
) -> str:
    """Run the configured command and return the worktree path it reports.

    Invocation is ``[*command, "--source=<S>", "--topic=<t>",
    ("--entry=<E>"), <mode>]``, always shell-free. stdout must be one JSON
    object; a non-zero exit is a refusal and is never retried through the
    built-in layout.

    :param command: argv from ``host.worktree_add_command``, element 0 absolute.
    :param source: Source directory passed as ``--source``, e.g.
        ``"/Users/alice/myrepo"``.
    :param topic: Worktree topic passed as ``--topic``, e.g.
        ``"feature/login"``.
    :param entry: Project entry passed as ``--entry`` when set, else ``None``.
    :param mode: The mode flags, e.g. ``["--new-branch=feature/login"]``.
    :returns: The absolute worktree directory reported by the command.
    :raises WorktreeCommandError: Per the failure table: the command cannot
        start, times out, refuses (non-zero exit), or returns no usable path.
    """
    argv = [*command, f"--source={source}", f"--topic={topic}"]
    if entry is not None:
        argv.append(f"--entry={entry}")
    argv += mode
    # Output goes to temp files, never pipes: no reader can block on a
    # descendant that inherited the write end (same reason as the post-bind
    # hook), and the read windows bound memory.
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            proc = subprocess.Popen(
                argv,
                cwd=source,
                env=_build_env(),
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                shell=False,
            )
        except OSError as exc:
            raise WorktreeCommandError(
                f"worktree command could not start: {exc}", code="unavailable"
            ) from exc
        try:
            exit_code = proc.wait(timeout=WORKTREE_COMMAND_TIMEOUT_S)
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            proc.wait()
            raise WorktreeCommandError(
                f"worktree command timed out after {WORKTREE_COMMAND_TIMEOUT_S:g}s",
                code="timeout",
            ) from exc
        stdout_text = _read_stdout(stdout)
        if exit_code != 0:
            raise _refusal(exit_code, stdout_text, _read_tail(stderr))
    return _path_from_envelope(stdout_text, entry)


def _build_env() -> dict[str, str]:
    """Build the child environment: the host's env minus the host token.

    :returns: The child environment. A child omnigent process mirrors any
        legacy-prefix name onto its ``OMNIGENT_`` spelling at startup, so
        every spelling of the token must be cleared.
    """
    env = dict(os.environ)
    suffix = HOST_TOKEN_ENV_VAR[len(_NEW_PREFIX) :]
    for spelling in (HOST_TOKEN_ENV_VAR, *(prefix + suffix for prefix in _LEGACY_PREFIXES)):
        env.pop(spelling, None)
    return env


def _refusal(exit_code: int, stdout_text: str, stderr_text: str) -> WorktreeCommandError:
    """Build the error for a non-zero exit, preferring the envelope's code.

    :param exit_code: The command's exit status.
    :param stdout_text: The stdout read window (the envelope candidate).
    :param stderr_text: The stderr tail.
    :returns: A refusal error carrying the command's code/detail, or ``failed``
        with the stderr (else stdout) tail when no usable envelope exists.
    """
    envelope = _parse_object(stdout_text)
    error = envelope.get("error") if envelope is not None else None
    if isinstance(error, dict) and isinstance(error.get("code"), str):
        code = error["code"]
        raw_message = error.get("message")
        message = raw_message if isinstance(raw_message, str) else ""
        detail = error.get("detail")
        return WorktreeCommandError(
            f"worktree command refused ({code}): {message}",
            code=code,
            detail=detail if isinstance(detail, dict) else None,
        )
    tail = (stderr_text or stdout_text).strip()[-_ERROR_TAIL_CHARS:]
    suffix = f": {tail}" if tail else ""
    return WorktreeCommandError(f"worktree command exited {exit_code}{suffix}", code="failed")


def _path_from_envelope(stdout_text: str, entry: str | None) -> str:
    """Return the absolute worktree path from a success envelope.

    :param stdout_text: The command's stdout read window.
    :param entry: The entry the path must stay inside when given, else ``None``.
    :returns: ``result.path`` as reported.
    :raises WorktreeCommandError: Code ``"malformed"`` when the envelope,
        ``ok`` flag, or path fails any check, or the path escapes the entry.
    """
    envelope = _parse_object(stdout_text)
    if envelope is None:
        raise WorktreeCommandError(
            "worktree command returned no usable path: stdout is not a JSON object",
            code="malformed",
        )
    if envelope.get("ok") is not True:
        raise WorktreeCommandError(
            "worktree command returned no usable path: the command did not report success",
            code="malformed",
        )
    result = envelope.get("result")
    path = result.get("path") if isinstance(result, dict) else None
    if not isinstance(path, str) or not path:
        raise WorktreeCommandError(
            "worktree command returned no usable path: result.path is not a string",
            code="malformed",
        )
    if not os.path.isabs(path):
        raise WorktreeCommandError(
            f"worktree command returned no usable path: {path!r} is not absolute",
            code="malformed",
        )
    if not os.path.isdir(path):
        raise WorktreeCommandError(
            f"worktree command returned no usable path: {path!r} is not a directory",
            code="malformed",
        )
    if entry is not None and not _contained_inside(
        os.path.realpath(path), os.path.realpath(entry)
    ):
        raise WorktreeCommandError(
            f"worktree command returned a path outside the entry: {path}", code="malformed"
        )
    return path


def _parse_object(text: str) -> dict[str, object] | None:
    """Parse ``text`` as a JSON object, or return ``None`` when it is not one."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _read_stdout(handle: IO[bytes]) -> str:
    """Read at most :data:`_STDOUT_READ_BYTES` of the command's stdout.

    :param handle: The child's stdout temp file.
    :returns: The decoded read window, UTF-8 with replacement on errors.
    """
    handle.seek(0)
    return handle.read(_STDOUT_READ_BYTES).decode("utf-8", errors="replace")


def _read_tail(handle: IO[bytes]) -> str:
    """Read a bounded tail of the command's stderr.

    :param handle: The child's stderr temp file.
    :returns: The last :data:`_ERROR_TAIL_CHARS` characters, UTF-8 with
        replacement on errors.
    """
    handle.seek(0, os.SEEK_END)
    handle.seek(max(0, handle.tell() - _ERROR_TAIL_BYTES))
    return handle.read().decode("utf-8", errors="replace")[-_ERROR_TAIL_CHARS:]
