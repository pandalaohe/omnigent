"""Runner-side executor for the seven ``sys_assignment_*`` tools.

Every tool runs as the calling session (``conversation_id``) over the
runner's ``server_client``. Dispatch publishes pinned input refs from
the sending runner and complete publishes output refs from the
receiving runner; the server stores the row, never file content. Git
runs as argv (never a shell) on a worker thread so the event loop
never blocks on a push.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
from pathlib import Path
from typing import NamedTuple, cast

import httpx

from omnigent.cli_diagnostics import redact_secrets
from omnigent.host.git_worktree import WorktreeError, _run_git
from omnigent.project_context import ManifestError, manifest_digest, parse_manifest

_TIMEOUT_S = 30.0


class AssignmentToolError(Exception):
    """A tool failure whose message is already the ``error`` payload."""


class _DispatchRepo(NamedTuple):
    repository_name: str
    commit: str
    artifact_paths: object


class _CompleteOutput(NamedTuple):
    repository_name: str
    commit: str
    artifact_paths: object


class _PreparedInput(NamedTuple):
    repository_name: str
    commit: str
    manifest_digest: str
    context_manifest_path: str
    artifact_paths: object
    directory: str


class _PreparedOutput(NamedTuple):
    repository_name: str
    commit: str
    artifact_paths: object
    directory: str
    remote_url: str


async def execute_assignment_tool(
    tool_name: str,
    arguments: str,
    *,
    conversation_id: str | None,
    runner_workspace: Path | None,
    server_client: httpx.AsyncClient | None,
) -> str:
    """Execute one ``sys_assignment_*`` tool and return the output JSON string.

    :param tool_name: One of the seven ``sys_assignment_*`` names.
    :param arguments: JSON-encoded arguments string from the LLM.
    :param conversation_id: The calling session; every tool reads it as
        the session to act as.
    :param runner_workspace: The calling session's workspace; the
        execution-root directory for ``sys_assignment_complete`` when the
        session has no recorded worktree (legacy rows; R-ASSIGN sets one
        for every current assignment session).
    :param server_client: HTTP client pointed at the Omnigent server.
    :returns: Tool output JSON string.
    """
    if conversation_id is None:
        return json.dumps({"error": f"{tool_name} requires a session id"})
    if server_client is None:
        return json.dumps({"error": f"{tool_name} requires server access"})
    try:
        args = json.loads(arguments) if arguments.strip() else {}
    except json.JSONDecodeError:
        return json.dumps({"error": f"{tool_name}: malformed JSON arguments"})
    if not isinstance(args, dict):
        return json.dumps({"error": f"{tool_name}: malformed JSON arguments"})
    try:
        if tool_name == "sys_assignment_get":
            return await _get(tool_name, args, conversation_id, server_client)
        if tool_name == "sys_assignment_list":
            return await _list(args, conversation_id, server_client)
        if tool_name == "sys_assignment_send":
            return await _send(tool_name, args, conversation_id, server_client)
        if tool_name == "sys_assignment_read_messages":
            return await _read_messages(tool_name, args, conversation_id, server_client)
        if tool_name == "sys_assignment_cancel":
            return await _cancel(tool_name, args, conversation_id, server_client)
        if tool_name == "sys_assignment_dispatch":
            return await _dispatch(tool_name, args, conversation_id, server_client)
        if tool_name == "sys_assignment_complete":
            return await _complete(
                tool_name, args, conversation_id, runner_workspace, server_client
            )
        return json.dumps({"error": f"unknown assignment tool {tool_name!r}"})
    except AssignmentToolError as exc:
        return json.dumps({"error": str(exc)})
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"{tool_name} failed: {exc}"})


def _server_error(resp: httpx.Response) -> str:
    """Map a 4xx/5xx into the scheduled-task-shaped error envelope."""
    return json.dumps(
        {
            "error": f"server returned {resp.status_code}",
            "details": redact_secrets(resp.text)[:500],
        }
    )


def _as_dict_list(value: object) -> list[dict[str, object]]:
    """Return ``value`` as a list of dicts, or ``[]`` when it is not one."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


async def _get(
    tool_name: str,
    args: dict[str, object],
    conversation_id: str,
    server_client: httpx.AsyncClient,
) -> str:
    del conversation_id
    assignment_id = args.get("assignment_id")
    if not assignment_id:
        return json.dumps({"error": f"{tool_name} requires 'assignment_id'"})
    resp = await server_client.get(f"/v1/assignments/{assignment_id}", timeout=_TIMEOUT_S)
    if resp.status_code >= 400:
        return _server_error(resp)
    return json.dumps(resp.json())


async def _list(
    args: dict[str, object],
    conversation_id: str,
    server_client: httpx.AsyncClient,
) -> str:
    params: dict[str, str | int] = {}
    for key in ("state", "after", "limit"):
        value = args.get(key)
        if (isinstance(value, str) and value) or isinstance(value, int):
            params[key] = value
    role = args.get("role")
    if isinstance(role, str) and role:
        params["role"] = role
        if role == "sent":
            params["source_session_id"] = conversation_id
        elif role == "received":
            params["session_id"] = conversation_id
    resp = await server_client.get("/v1/assignments", params=params, timeout=_TIMEOUT_S)
    if resp.status_code >= 400:
        return _server_error(resp)
    return json.dumps(resp.json())


async def _send(
    tool_name: str,
    args: dict[str, object],
    conversation_id: str,
    server_client: httpx.AsyncClient,
) -> str:
    assignment_id = args.get("assignment_id")
    if not assignment_id:
        return json.dumps({"error": f"{tool_name} requires 'assignment_id'"})
    if not args.get("body"):
        return json.dumps({"error": f"{tool_name} requires 'body'"})
    if not args.get("idempotency_key"):
        return json.dumps({"error": f"{tool_name} requires 'idempotency_key'"})
    resp = await server_client.post(
        f"/v1/assignments/{assignment_id}/messages",
        json={
            "sender_session_id": conversation_id,
            "body": args["body"],
            "idempotency_key": args["idempotency_key"],
        },
        timeout=_TIMEOUT_S,
    )
    if resp.status_code >= 400:
        return _server_error(resp)
    return json.dumps(resp.json())


async def _read_messages(
    tool_name: str,
    args: dict[str, object],
    conversation_id: str,
    server_client: httpx.AsyncClient,
) -> str:
    del conversation_id
    assignment_id = args.get("assignment_id")
    if not assignment_id:
        return json.dumps({"error": f"{tool_name} requires 'assignment_id'"})
    params: dict[str, str | int] = {}
    for key in ("after", "limit"):
        value = args.get(key)
        if (isinstance(value, str) and value) or isinstance(value, int):
            params[key] = value
    resp = await server_client.get(
        f"/v1/assignments/{assignment_id}/messages", params=params, timeout=_TIMEOUT_S
    )
    if resp.status_code >= 400:
        return _server_error(resp)
    return json.dumps(resp.json())


async def _cancel(
    tool_name: str,
    args: dict[str, object],
    conversation_id: str,
    server_client: httpx.AsyncClient,
) -> str:
    del conversation_id
    assignment_id = args.get("assignment_id")
    if not assignment_id:
        return json.dumps({"error": f"{tool_name} requires 'assignment_id'"})
    # The route takes an optional body, so always send an object.
    body: dict[str, object] = {}
    if args.get("reason") is not None:
        body["reason"] = args["reason"]
    resp = await server_client.post(
        f"/v1/assignments/{assignment_id}/cancel", json=body, timeout=_TIMEOUT_S
    )
    if resp.status_code >= 400:
        return _server_error(resp)
    return json.dumps(resp.json())


async def _git(directory: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Run one git argv in ``directory`` on a worker thread."""
    try:
        result = await asyncio.to_thread(_run_git, list(args), cwd=directory)
    except WorktreeError as exc:
        raise AssignmentToolError(redact_secrets(f"git {' '.join(args)} failed: {exc}")) from exc
    return cast(subprocess.CompletedProcess[str], result)


async def _git_bytes(directory: str, *args: str) -> subprocess.CompletedProcess[bytes]:
    """Run one git argv in ``directory``, capturing raw bytes."""
    try:
        result = await asyncio.to_thread(_run_git, list(args), cwd=directory, text=False)
    except WorktreeError as exc:
        raise AssignmentToolError(redact_secrets(f"git {' '.join(args)} failed: {exc}")) from exc
    return cast(subprocess.CompletedProcess[bytes], result)


async def _resolve_commit(
    tool_name: str, directory: str, repository_name: str, commit: str
) -> str:
    """Expand ``commit`` to its full hash, or raise naming repo and value."""
    result = await _git(
        directory,
        "rev-parse",
        "--verify",
        "--quiet",
        "--end-of-options",
        f"{commit}^{{commit}}",
    )
    full = result.stdout.strip() if isinstance(result.stdout, str) else ""
    if result.returncode != 0 or not full:
        raise AssignmentToolError(
            f"{tool_name}: cannot resolve commit {commit!r} for repository {repository_name!r}"
        )
    return full


async def _ls_remote(directory: str, url: str, ref: str) -> str | None:
    """Return the commit ``ref`` points at on ``url``, or ``None`` when absent."""
    result = await _git(directory, "ls-remote", "--", url, ref)
    if result.returncode != 0:
        detail = result.stderr.strip() if isinstance(result.stderr, str) else ""
        # Scrub before cutting: a cut can split a secret so it no longer matches.
        redacted = redact_secrets(detail)[:200]
        suffix = f": {redacted}" if redacted else ""
        raise AssignmentToolError(redact_secrets(f"git ls-remote {url} {ref} failed{suffix}"))
    out = result.stdout.strip() if isinstance(result.stdout, str) else ""
    if not out:
        return None
    # The pattern also matches ref tails, so only an exact refname counts.
    for line in out.splitlines():
        sha, sep, refname = line.partition("\t")
        if sep and refname == ref:
            return sha
    return None


async def _push(directory: str, url: str, commit: str, ref: str) -> None:
    """Create ``ref`` at ``commit``; the empty lease refuses an existing ref."""
    result = await _git(
        directory, "push", f"--force-with-lease={ref}:", "--", url, f"{commit}:{ref}"
    )
    if result.returncode != 0:
        detail = result.stderr.strip() if isinstance(result.stderr, str) else ""
        # Scrub before cutting: a cut can split a secret so it no longer matches.
        redacted = redact_secrets(detail)[:200]
        suffix = f": {redacted}" if redacted else ""
        raise AssignmentToolError(redact_secrets(f"git push {url} {commit}:{ref} failed{suffix}"))


async def _publish_ref(
    directory: str, url: str, commit: str, ref: str, *, attempts: int
) -> tuple[str | None, str | None]:
    """Land ``commit`` at ``ref``; a lost push ack still counts when observed."""
    reason: str | None = None
    for _ in range(max(1, attempts)):
        try:
            observed = await _ls_remote(directory, url, ref)
        except AssignmentToolError as exc:
            reason = str(exc)
            continue
        if observed == commit:
            return commit, None
        if observed is not None:
            return None, f"ref {ref} already points at {observed} on the remote"
        try:
            await _push(directory, url, commit, ref)
        except AssignmentToolError as exc:
            reason = str(exc)
        try:
            observed = await _ls_remote(directory, url, ref)
        except AssignmentToolError as exc:
            reason = str(exc)
            continue
        if observed == commit:
            return commit, None
        if observed is not None:
            return None, f"ref {ref} already points at {observed} on the remote"
        reason = f"push did not land {ref} at {commit}"
    return None, reason or f"push did not land {ref} at {commit}"


async def _get_assignment_row(
    tool_name: str, assignment_id: str, server_client: httpx.AsyncClient
) -> dict[str, object]:
    """Return one assignment row, or raise on transport, status or shape."""
    resp = await server_client.get(f"/v1/assignments/{assignment_id}", timeout=_TIMEOUT_S)
    if resp.status_code >= 400:
        raise AssignmentToolError(
            f"{tool_name}: server returned {resp.status_code}: {redact_secrets(resp.text)[:500]}"
        )
    row = resp.json()
    if not isinstance(row, dict):
        raise AssignmentToolError(f"{tool_name}: invalid assignment response")
    return row


async def _session_placement(
    tool_name: str, conversation_id: str, server_client: httpx.AsyncClient
) -> tuple[str | None, str | None, str | None]:
    """Return the calling session's ``(project_id, host_id, worktree)``."""
    resp = await server_client.get(
        f"/v1/sessions/{conversation_id}",
        params={"include_items": "false", "include_liveness": "false"},
        timeout=_TIMEOUT_S,
    )
    if resp.status_code >= 400:
        raise AssignmentToolError(
            f"{tool_name}: server returned {resp.status_code}: {redact_secrets(resp.text)[:500]}"
        )
    body = resp.json()
    project_id = body.get("project_id")
    host_id = body.get("host_id")
    worktree = body.get("worktree")
    return (
        project_id if isinstance(project_id, str) else None,
        host_id if isinstance(host_id, str) else None,
        worktree if isinstance(worktree, str) else None,
    )


async def _collaboration(
    tool_name: str,
    project_id: str,
    server_client: httpx.AsyncClient,
) -> dict[str, object]:
    """Return the project's collaboration config, or raise when it is off."""
    resp = await server_client.get(f"/v1/projects/{project_id}/collaboration", timeout=_TIMEOUT_S)
    if resp.status_code == 404:
        raise AssignmentToolError(
            f"{tool_name}: project assignments are not enabled on this server"
        )
    if resp.status_code >= 400:
        raise AssignmentToolError(
            f"{tool_name}: server returned {resp.status_code}: {redact_secrets(resp.text)[:500]}"
        )
    body = resp.json()
    if not isinstance(body, dict):
        raise AssignmentToolError(f"{tool_name}: invalid collaboration response")
    if not body.get("enabled"):
        name = project_id
        proj = await server_client.get(f"/v1/projects/{project_id}", timeout=_TIMEOUT_S)
        if proj.status_code < 400:
            proj_body = proj.json()
            if isinstance(proj_body, dict) and isinstance(proj_body.get("name"), str):
                name = proj_body["name"]
        raise AssignmentToolError(
            f"{tool_name}: project {name!r} has collaboration disabled; "
            "enable it before dispatching assignments"
        )
    return body


def _registrations(
    collaboration: dict[str, object],
) -> dict[str, tuple[str, str]]:
    """Map repository name to ``(registration id, manifest path)``."""
    registered: dict[str, tuple[str, str]] = {}
    for repo in _as_dict_list(collaboration.get("repositories")):
        name = repo.get("name")
        rid = repo.get("id")
        if not isinstance(name, str) or not name or not isinstance(rid, str) or not rid:
            continue
        manifest_path = repo.get("context_manifest_path")
        registered[name] = (
            rid,
            manifest_path
            if isinstance(manifest_path, str) and manifest_path
            else ".agents/project/manifest.json",
        )
    return registered


def _input_entries(row: dict[str, object]) -> dict[str, dict[str, object]]:
    """Map repository name to its input entry."""
    inputs: dict[str, dict[str, object]] = {}
    for item in _as_dict_list(row.get("inputs")):
        name = item.get("repository_name")
        if isinstance(name, str) and name:
            inputs[name] = item
    return inputs


def _bindings_for_repo(
    tool_name: str,
    collaboration: dict[str, object],
    host_id: str,
    repository_name: str,
    repository_id: str,
) -> str:
    """Return the unique enabled binding directory, or raise naming the repo."""
    matches = [
        binding
        for binding in _as_dict_list(collaboration.get("bindings"))
        if binding.get("enabled")
        and binding.get("host_id") == host_id
        and binding.get("repository_id") == repository_id
    ]
    if not matches:
        raise AssignmentToolError(
            f"{tool_name}: no enabled binding for {repository_name!r} on this host"
        )
    if len(matches) > 1:
        raise AssignmentToolError(
            f"{tool_name}: several enabled bindings for {repository_name!r} on this host"
        )
    workspace = matches[0].get("workspace")
    if not isinstance(workspace, str) or not workspace:
        raise AssignmentToolError(
            f"{tool_name}: no enabled binding for {repository_name!r} on this host"
        )
    return workspace


async def _manifest_digest_for(
    tool_name: str, directory: str, repository_name: str, manifest_path: str, commit: str
) -> tuple[str, str]:
    """Hash the manifest blob at ``commit``; absent/invalid raises."""
    result = await _git_bytes(directory, "cat-file", "blob", f"{commit}:{manifest_path}")
    if result.returncode != 0:
        raise AssignmentToolError(
            f"{tool_name}: manifest {manifest_path!r} absent at the commit for "
            f"repository {repository_name!r}"
        )
    blob = bytes(result.stdout)
    try:
        parse_manifest(blob)
    except ManifestError as exc:
        raise AssignmentToolError(
            f"{tool_name}: invalid manifest for repository {repository_name!r}: {exc}"
        ) from exc
    return manifest_digest(blob), manifest_path


def _parse_dispatch_repos(tool_name: str, value: object) -> list[_DispatchRepo]:
    """Validate the dispatch ``repositories`` argument once, carrying typed values."""
    if not isinstance(value, list) or not value:
        raise AssignmentToolError(f"{tool_name} requires a non-empty 'repositories'")
    repos: list[_DispatchRepo] = []
    for entry in value:
        name = entry.get("repository_name") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not name:
            raise AssignmentToolError(
                f"{tool_name} requires 'repository_name' for every repository"
            )
        commit = entry.get("commit") if isinstance(entry, dict) else None
        if not isinstance(commit, str) or not commit:
            raise AssignmentToolError(f"{tool_name} requires 'commit' for repository {name!r}")
        # Passed through verbatim (the server validates); ``[]`` when absent.
        raw_paths = (
            entry["artifact_paths"]
            if isinstance(entry, dict) and "artifact_paths" in entry
            else []
        )
        repos.append(
            _DispatchRepo(
                repository_name=name,
                commit=commit,
                artifact_paths=raw_paths,
            )
        )
    return repos


async def _dispatch(
    tool_name: str,
    args: dict[str, object],
    conversation_id: str,
    server_client: httpx.AsyncClient,
) -> str:
    for key in ("target_agent_id", "task", "idempotency_key"):
        if not args.get(key):
            return json.dumps({"error": f"{tool_name} requires '{key}'"})
    try:
        requested = _parse_dispatch_repos(tool_name, args.get("repositories"))
    except AssignmentToolError as exc:
        return json.dumps({"error": str(exc)})

    project_id, host_id, _ = await _session_placement(tool_name, conversation_id, server_client)
    if project_id is None:
        raise AssignmentToolError(f"{tool_name}: this session is not filed in a project")
    if host_id is None:
        raise AssignmentToolError(
            f"{tool_name}: dispatch needs a session running on a registered host"
        )
    collaboration = await _collaboration(tool_name, project_id, server_client)
    registered = _registrations(collaboration)
    prepared: list[_PreparedInput] = []
    for req in requested:
        registration = registered.get(req.repository_name)
        if registration is None:
            raise AssignmentToolError(
                f"{tool_name}: unknown repository {req.repository_name!r} on this project"
            )
        registration_id, manifest_path = registration
        directory = _bindings_for_repo(
            tool_name, collaboration, host_id, req.repository_name, registration_id
        )
        full = await _resolve_commit(tool_name, directory, req.repository_name, req.commit)
        digest, hashed_path = await _manifest_digest_for(
            tool_name, directory, req.repository_name, manifest_path, full
        )
        prepared.append(
            _PreparedInput(
                repository_name=req.repository_name,
                commit=full,
                manifest_digest=digest,
                context_manifest_path=hashed_path,
                artifact_paths=req.artifact_paths,
                directory=directory,
            )
        )

    # The id derives from the idempotency key so a retried call reuses it.
    assignment_id = hashlib.sha256(
        f"assignment-dispatch:{conversation_id}:{args['idempotency_key']}".encode()
    ).hexdigest()[:32]
    payload: dict[str, object] = {
        "id": assignment_id,
        "source_session_id": conversation_id,
        "target_agent_id": args["target_agent_id"],
        "task": args["task"],
        "repositories": [
            {
                "repository_name": item.repository_name,
                "commit": item.commit,
                "manifest_digest": item.manifest_digest,
                "context_manifest_path": item.context_manifest_path,
                "artifact_paths": item.artifact_paths,
            }
            for item in prepared
        ],
        "idempotency_key": args["idempotency_key"],
    }
    if args.get("host_id") is not None:
        payload["requested_host_id"] = args["host_id"]
    for key in (
        "binding_name",
        "execution_root",
        "model_override",
        "harness_override",
        "start_deadline",
    ):
        if args.get(key) is not None:
            payload[key] = args[key]
    resp = await server_client.post("/v1/assignments", json=payload, timeout=_TIMEOUT_S)
    if resp.status_code >= 400:
        return _server_error(resp)
    row = resp.json()
    if not isinstance(row, dict):
        raise AssignmentToolError(f"{tool_name}: invalid assignment response")
    if row.get("state") != "preparing":
        return json.dumps(row)

    inputs = _input_entries(row)
    landed: dict[str, str] = {}
    failures: dict[str, str] = {}
    for item in prepared:
        entry = inputs.get(item.repository_name, {})
        ref = entry.get("input_ref") if isinstance(entry, dict) else None
        url = entry.get("remote_url") if isinstance(entry, dict) else None
        if not isinstance(ref, str) or not isinstance(url, str):
            failures[item.repository_name] = "the server returned no input ref to publish"
            continue
        landed_commit, reason = await _publish_ref(
            item.directory, url, item.commit, ref, attempts=1
        )
        if landed_commit is not None:
            landed[item.repository_name] = landed_commit
        else:
            failures[item.repository_name] = reason or "unknown publication failure"

    published = await server_client.post(
        f"/v1/assignments/{row.get('id', assignment_id)}/published",
        json={
            "refs": [
                {"repository_name": name, "commit": commit} for name, commit in landed.items()
            ]
        },
        timeout=_TIMEOUT_S,
    )
    if published.status_code >= 400:
        return _server_error(published)
    result = published.json()
    if isinstance(result, dict):
        if result.get("state") == "failed" and failures:
            result = {
                **result,
                "error": "\n".join(f"{name}: {why}" for name, why in failures.items()),
            }
        return json.dumps(result)
    return json.dumps(result)


def _parse_complete_outputs(tool_name: str, value: object) -> list[_CompleteOutput]:
    """Validate the complete ``outputs`` argument once, carrying typed values."""
    if not isinstance(value, list) or not value:
        raise AssignmentToolError(f"{tool_name} requires a non-empty 'outputs'")
    outputs: list[_CompleteOutput] = []
    for entry in value:
        name = entry.get("repository_name") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not name:
            raise AssignmentToolError(f"{tool_name} requires 'repository_name' for every output")
        commit = entry.get("commit") if isinstance(entry, dict) else None
        if not isinstance(commit, str) or not commit:
            raise AssignmentToolError(f"{tool_name} requires 'commit' for repository {name!r}")
        # Passed through verbatim (the server validates); ``[]`` when absent.
        raw_paths = (
            entry["artifact_paths"]
            if isinstance(entry, dict) and "artifact_paths" in entry
            else []
        )
        outputs.append(
            _CompleteOutput(
                repository_name=name,
                commit=commit,
                artifact_paths=raw_paths,
            )
        )
    return outputs


async def _complete(
    tool_name: str,
    args: dict[str, object],
    conversation_id: str,
    runner_workspace: Path | None,
    server_client: httpx.AsyncClient,
) -> str:
    assignment_id = args.get("assignment_id")
    if not assignment_id:
        return json.dumps({"error": f"{tool_name} requires 'assignment_id'"})
    if not args.get("summary"):
        return json.dumps({"error": f"{tool_name} requires 'summary'"})
    try:
        requested = _parse_complete_outputs(tool_name, args.get("outputs"))
    except AssignmentToolError as exc:
        return json.dumps({"error": str(exc)})

    row = await _get_assignment_row(tool_name, str(assignment_id), server_client)
    inputs = _input_entries(row)
    project_id, host_id, worktree = await _session_placement(
        tool_name, conversation_id, server_client
    )
    collaboration: dict[str, object] | None = None
    registered: dict[str, tuple[str, str]] = {}
    prepared: list[_PreparedOutput] = []
    for req in requested:
        entry = inputs.get(req.repository_name)
        if entry is None:
            raise AssignmentToolError(
                f"{tool_name}: unknown repository {req.repository_name!r}: outputs must "
                "name a repository from the assignment inputs"
            )
        if entry.get("is_execution_root"):
            # Commits in the session's recorded worktree (R-ASSIGN); a
            # session launched at a project entry has its execution root
            # there, not at the launch directory the entry itself is.
            execution_root = worktree or (
                str(runner_workspace) if runner_workspace is not None else None
            )
            if execution_root is None:
                raise AssignmentToolError(
                    f"{tool_name}: no session workspace for the execution-root "
                    f"repository {req.repository_name!r}"
                )
            directory = execution_root
        else:
            if host_id is None:
                raise AssignmentToolError(
                    f"{tool_name}: complete needs a session running on a registered host"
                )
            if project_id is None:
                raise AssignmentToolError(f"{tool_name}: this session is not filed in a project")
            if collaboration is None:
                collaboration = await _collaboration(tool_name, project_id, server_client)
                registered = _registrations(collaboration)
            registration = registered.get(req.repository_name)
            if registration is None:
                raise AssignmentToolError(
                    f"{tool_name}: unknown repository {req.repository_name!r} on this project"
                )
            registration_id, _ = registration
            binding_workspace = _bindings_for_repo(
                tool_name, collaboration, host_id, req.repository_name, registration_id
            )
            top = await _git(binding_workspace, "rev-parse", "--show-toplevel")
            toplevel = top.stdout.strip() if isinstance(top.stdout, str) else ""
            if top.returncode != 0 or not toplevel:
                raise AssignmentToolError(
                    f"{tool_name}: cannot resolve a worktree for repository "
                    f"{req.repository_name!r} on this host"
                )
            directory = str(
                Path(toplevel)
                / ".omnigent"
                / "worktrees"
                / str(assignment_id)
                / req.repository_name
            )
        full = await _resolve_commit(tool_name, directory, req.repository_name, req.commit)
        remote_url = entry.get("remote_url")
        if not isinstance(remote_url, str):
            raise AssignmentToolError(f"{tool_name}: invalid assignment response")
        prepared.append(
            _PreparedOutput(
                repository_name=req.repository_name,
                commit=full,
                artifact_paths=req.artifact_paths,
                directory=directory,
                remote_url=remote_url,
            )
        )

    completed = await server_client.post(
        f"/v1/assignments/{assignment_id}/complete",
        json={
            "session_id": conversation_id,
            "outputs": [
                {
                    "repository_name": item.repository_name,
                    "commit": item.commit,
                    "artifact_paths": item.artifact_paths,
                }
                for item in prepared
            ],
            "summary": args["summary"],
        },
        timeout=_TIMEOUT_S,
    )
    if completed.status_code >= 400:
        return _server_error(completed)
    current = completed.json()
    if not isinstance(current, dict):
        raise AssignmentToolError(f"{tool_name}: invalid assignment response")
    if current.get("state") != "publishing":
        return json.dumps(current)
    current_outputs = current.get("outputs")
    refs = (
        {
            item["repository_name"]: item["ref"]
            for item in current_outputs
            if isinstance(item, dict)
            and isinstance(item.get("repository_name"), str)
            and isinstance(item.get("ref"), str)
        }
        if isinstance(current_outputs, list)
        else {}
    )

    landed: dict[str, str] = {}
    failures: dict[str, str] = {}
    for item in prepared:
        ref = refs.get(item.repository_name)
        if ref is None:
            failures[item.repository_name] = "the server returned no output ref to publish"
            continue
        landed_commit, reason = await _publish_ref(
            item.directory, item.remote_url, item.commit, ref, attempts=3
        )
        if landed_commit is not None:
            landed[item.repository_name] = landed_commit
        else:
            failures[item.repository_name] = reason or "unknown publication failure"

    error_text = (
        None if not failures else "\n".join(f"{name}: {why}" for name, why in failures.items())
    )
    finished = await server_client.post(
        f"/v1/assignments/{assignment_id}/finish",
        json={
            "session_id": conversation_id,
            "refs": [
                {"repository_name": name, "commit": commit} for name, commit in landed.items()
            ],
            "error": error_text,
        },
        timeout=_TIMEOUT_S,
    )
    if finished.status_code >= 400:
        return _server_error(finished)
    result = finished.json()
    if isinstance(result, dict):
        if result.get("state") == "succeeded":
            result = {
                **result,
                "next_step": "The assignment is finished. End your turn now with no further "
                "tool calls; the session will be closed.",
            }
        if result.get("state") == "failed" and error_text:
            result = {**result, "error": error_text}
        return json.dumps(result)
    return json.dumps(result)
