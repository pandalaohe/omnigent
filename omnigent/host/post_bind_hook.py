"""Run the host's own post-bind command for a project host binding.

The server tells a host that a binding was stored or re-verified
(``host.post_bind_hook``); the command itself comes from the host's startup
config (``host.post_bind_command``), so no server-side value can choose what
runs. The hook is opt-in, host-local and shell-free; a failure never refuses
the binding — the outcome rides back in the result frame.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import IO

import yaml

from omnigent._env_compat import _LEGACY_PREFIXES, _NEW_PREFIX
from omnigent.host.frames import (
    HostPostBindHookFrame,
    HostPostBindHookResultFrame,
)
from omnigent.host.identity import HOST_TOKEN_ENV_VAR
from omnigent.project_context import ManifestError, parse_manifest

# Wall-clock cap on one hook run. A module constant so tests can lower it;
# the server's own wait is longer (it must also cover the round trip).
POST_BIND_TIMEOUT_S: float = 30.0

# The child's output goes to a temp file, never a pipe: no reader can block
# on a descendant that inherited the write end. Only the tail travels back,
# and the read window bounds memory even when the command is chatty.
_OUTPUT_TAIL_CHARS = 2000
_OUTPUT_READ_BYTES = 4 * _OUTPUT_TAIL_CHARS

_MANIFEST_MAX_BYTES = 1024 * 1024

# Every variable the hook sets or clears, so nothing inherited from the host
# process (a stale identity id, a previous binding's facts) can leak in.
_HOOK_ENV_VARS = (
    "OMNIGENT_PROJECT_ID",
    "OMNIGENT_BINDING_NAME",
    "OMNIGENT_BINDING_REVISION",
    "OMNIGENT_BINDING_PRIMARY",
    "OMNIGENT_BINDING_WORKSPACE",
    "OMNIGENT_REPOSITORY_NAME",
    "OMNIGENT_PROJECT_IDENTITY_ID",
    "OMNIGENT_HOOK_TRIGGER",
)

_IDENTITY_ID_RE = re.compile(r"^[A-Za-z0-9._:@+-]{1,128}$")


class PostBindHookRunner:
    """Run the ``host.post_bind_command`` for incoming hook requests.

    Blocking by design: callers run :meth:`run` off the event loop and inside
    :meth:`omnigent.host.connect.HostProcess._host_subprocess_op`. Requests
    for one binding name are serialized; a stale request is dropped only when
    it names the same stored binding row as a newer one, so a deleted and
    recreated binding (new row id, revision restarts at 1) still runs.

    :param config_path: The config file the host process was started with;
        re-read on every request so an edit applies without a restart.
    """

    def __init__(self, config_path: Path) -> None:
        """Initialize with the host's startup config path.

        :param config_path: Path to ``config.yaml``.
        """
        self._config_path = config_path
        self._locks_guard = threading.Lock()
        self._locks: dict[tuple[str, str], threading.Lock] = {}
        # Rows of one binding name have independent revision sequences, so the
        # watermark is per row id.
        self._highest_revision: dict[str, int] = {}

    def run(self, frame: HostPostBindHookFrame) -> HostPostBindHookResultFrame:
        """Order, configure and run one hook request.

        :param frame: The hook request from the server.
        :returns: The result frame; every failure is a status, never a raise.
            A request is ``superseded`` only when it names the same stored
            binding row as one already started and carries a lower revision.
        """
        with self._binding_lock(frame):
            highest = self._highest_revision.get(frame.binding_id)
            if highest is not None and frame.revision < highest:
                return self._result(frame, "superseded")
            self._highest_revision[frame.binding_id] = frame.revision

            try:
                argv = _load_command(self._config_path)
            except _CommandConfigError as exc:
                return self._result(frame, "failed", error=str(exc))
            if argv is None:
                return self._result(frame, "not_configured")

            workspace = frame.workspace
            if os.path.realpath(workspace) != workspace or not os.path.isdir(workspace):
                return self._result(
                    frame,
                    "failed",
                    error=f"workspace is not a canonical directory: {workspace!r}",
                )

            env = _build_env(frame)
            return self._run(argv, frame, workspace, env)

    def _binding_lock(self, frame: HostPostBindHookFrame) -> threading.Lock:
        """Return the lock serializing runs for one binding key.

        :param frame: The hook request naming the binding.
        :returns: The lock for ``(project_id, binding_name)``.
        """
        key = (frame.project_id, frame.binding_name)
        with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    def _run(
        self,
        argv: list[str],
        frame: HostPostBindHookFrame,
        workspace: str,
        env: dict[str, str],
    ) -> HostPostBindHookResultFrame:
        """Start the command and summarise its outcome.

        :param argv: Full argv from the host config, element 0 absolute.
        :param frame: The hook request (carries the request id).
        :param workspace: Canonical bound directory, used as the cwd.
        :param env: The child environment.
        :returns: The result frame.
        """
        with tempfile.TemporaryFile() as output:
            try:
                proc = subprocess.Popen(
                    argv,
                    cwd=workspace,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    shell=False,
                )
            except OSError as exc:
                return self._result(frame, "failed", error=str(exc))
            try:
                exit_code = proc.wait(timeout=POST_BIND_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                return self._result(frame, "timed_out", output=_read_tail(output))
            text = _read_tail(output)
            if exit_code == 0:
                return self._result(frame, "ok", exit_code=0, output=text)
            return self._result(
                frame,
                "failed",
                exit_code=exit_code,
                output=text,
                error=f"command exited {exit_code}",
            )

    def _result(
        self,
        frame: HostPostBindHookFrame,
        status: str,
        *,
        exit_code: int | None = None,
        output: str | None = None,
        error: str | None = None,
    ) -> HostPostBindHookResultFrame:
        """Build a result frame for the request.

        :param frame: The request being answered.
        :param status: ``ok`` / ``failed`` / ``timed_out`` / ``superseded`` /
            ``not_configured``.
        :param exit_code: Child exit status, when one was observed.
        :param output: Last chars of the child's combined output.
        :param error: Failure detail.
        :returns: The result frame.
        """
        return HostPostBindHookResultFrame(
            request_id=frame.request_id,
            status=status,
            exit_code=exit_code,
            output=output or None,
            error=error,
        )


class _CommandConfigError(Exception):
    """A configured host command is present but not a usable argv list."""


def _load_command(config_path: Path, key: str = "post_bind_command") -> list[str] | None:
    """Read ``host.<key>`` from the host's config file.

    :param config_path: The host's startup config path.
    :param key: The ``host`` section key to read, e.g.
        ``"post_bind_command"`` or ``"worktree_add_command"``.
    :returns: The argv list, or ``None`` when the file or key is absent.
    :raises _CommandConfigError: When the key is present but not a non-empty
        list of strings, or the first element is not an absolute path, or the
        first element is a Windows batch file (no ``cmd.exe`` re-parse).
    """
    if not config_path.exists():
        return None
    try:
        with open(config_path) as fh:
            cfg = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError) as exc:
        raise _CommandConfigError(f"could not read host.{key}: {exc}") from exc
    if not isinstance(cfg, dict):
        return None
    host_section = cfg.get("host")
    if not isinstance(host_section, dict) or key not in host_section:
        return None
    raw = host_section[key]
    if not isinstance(raw, list) or not raw or not all(isinstance(item, str) for item in raw):
        raise _CommandConfigError(f"host.{key} must be a non-empty list of strings")
    executable = raw[0]
    if not os.path.isabs(executable):
        raise _CommandConfigError(f"host.{key} must start with an absolute path")
    if executable.lower().endswith((".bat", ".cmd")):
        raise _CommandConfigError(f"host.{key} must not be a .bat/.cmd file")
    return list(raw)


def _build_env(frame: HostPostBindHookFrame) -> dict[str, str]:
    """Build the child environment: host env minus secrets, plus binding facts.

    :param frame: The hook request carrying the binding facts.
    :returns: The child environment.
    """
    env = dict(os.environ)
    # A child omnigent process mirrors any legacy-prefix name onto its
    # OMNIGENT_ spelling at startup, so every spelling must be cleared.
    for name in (HOST_TOKEN_ENV_VAR, *_HOOK_ENV_VARS):
        suffix = name[len(_NEW_PREFIX) :]
        for spelling in (name, *(prefix + suffix for prefix in _LEGACY_PREFIXES)):
            env.pop(spelling, None)
    env["OMNIGENT_PROJECT_ID"] = frame.project_id
    env["OMNIGENT_BINDING_NAME"] = frame.binding_name
    env["OMNIGENT_BINDING_REVISION"] = str(frame.revision)
    env["OMNIGENT_BINDING_PRIMARY"] = "true" if frame.is_primary else "false"
    env["OMNIGENT_BINDING_WORKSPACE"] = frame.workspace
    env["OMNIGENT_REPOSITORY_NAME"] = frame.repository_name
    env["OMNIGENT_HOOK_TRIGGER"] = frame.trigger
    identity_id = _read_identity_id(frame)
    if identity_id is not None:
        env["OMNIGENT_PROJECT_IDENTITY_ID"] = identity_id
    return env


def _read_identity_id(frame: HostPostBindHookFrame) -> str | None:
    """Read the manifest's ``identity.id`` from the binding's working copy.

    Best-effort: a missing, oversized, escaping or invalid manifest leaves the
    variable absent — the command decides whether it needs the id.

    :param frame: The hook request naming the workspace and manifest path.
    :returns: The id when it is present and safe, else ``None``.
    """
    candidate = os.path.realpath(os.path.join(frame.workspace, frame.context_manifest_path))
    if not candidate.startswith(frame.workspace + os.sep):
        return None
    try:
        if not os.path.isfile(candidate) or os.path.getsize(candidate) > _MANIFEST_MAX_BYTES:
            return None
        with open(candidate, "rb") as fh:
            # The file can grow after getsize, so the read stays capped.
            blob = fh.read(_MANIFEST_MAX_BYTES + 1)
    except OSError:
        return None
    if len(blob) > _MANIFEST_MAX_BYTES:
        return None
    try:
        identity = parse_manifest(blob).identity
    except ManifestError:
        return None
    if identity is None:
        return None
    identity_id = identity.get("id")
    if not isinstance(identity_id, str) or not _IDENTITY_ID_RE.fullmatch(identity_id):
        return None
    return identity_id


def _read_tail(output: IO[bytes]) -> str:
    """Read the tail of the temp file, bounded in bytes and characters.

    :param output: The child's output file, positioned anywhere.
    :returns: The last 2000 characters, UTF-8 with replacement on errors.
    """
    output.seek(0, os.SEEK_END)
    output.seek(max(0, output.tell() - _OUTPUT_READ_BYTES))
    return output.read(_OUTPUT_READ_BYTES).decode("utf-8", errors="replace")[-_OUTPUT_TAIL_CHARS:]
