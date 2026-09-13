"""Dispatch and complete against the real assignment routes and store.

Real git repositories with local bare remotes stand in for the shared
remotes; sessions, hosts and bindings are seeded through the stores.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import subprocess
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.db.utils import now_epoch
from omnigent.project_context import manifest_digest
from omnigent.runner.assignment_tools import execute_assignment_tool
from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.feature_flags import resolve_feature_flags
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.assignment_store.sqlalchemy_store import SqlAlchemyAssignmentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.project_host_binding_store.sqlalchemy_store import (
    SqlAlchemyProjectHostBindingStore,
)
from omnigent.stores.project_repository_store.sqlalchemy_store import (
    SqlAlchemyProjectRepositoryStore,
)
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH")

AGENT_ID = "087b7cb7ac30abf4debfaa578d052ec6"
_HOST_ID = "a1b2c3d4e5f60718293a4b5c6d7e8f01"
_MANIFEST_PATH = ".agents/project/manifest.json"

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env={**os.environ, **_GIT_ENV},
        capture_output=True,
        text=True,
        check=False,
    )


def _git_ok(cwd: Path, *args: str) -> str:
    result = _git(cwd, *args)
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout.strip()


def _make_remote_and_source(tmp_path: Path, name: str) -> tuple[Path, Path]:
    remote = (tmp_path / f"{name}-remote").resolve()
    remote.mkdir()
    _git_ok(remote, "init", "-q", "-b", "main", "--bare")
    source = (tmp_path / name).resolve()
    source.mkdir()
    _git_ok(source, "init", "-q", "-b", "main")
    (source / "README.md").write_text("hi\n")
    _git_ok(source, "add", ".")
    _git_ok(source, "commit", "-q", "-m", "init")
    _git_ok(source, "remote", "add", "origin", str(remote))
    return remote, source


def _commit_all(source: Path, message: str) -> str:
    _git_ok(source, "add", "-A")
    _git_ok(source, "commit", "-q", "-m", message)
    return _git_ok(source, "rev-parse", "HEAD")


def _write_manifest(source: Path) -> None:
    manifest = source / _MANIFEST_PATH
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"version": 1}))


def _ls_remote(remote: Path, ref: str) -> str | None:
    out = _git(remote, "ls-remote", str(remote), ref).stdout.strip()
    # Git matches the pattern against ref tails, so only an exact refname counts.
    for line in out.splitlines():
        sha, _, name = line.partition("\t")
        if name == ref:
            return sha
    return None


def _build_app(db_uri: str, tmp_path: Path) -> FastAPI:
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        host_store=HostStore(db_uri),
        project_store=SqlAlchemyProjectStore(db_uri),
        project_repository_store=SqlAlchemyProjectRepositoryStore(db_uri),
        project_host_binding_store=SqlAlchemyProjectHostBindingStore(db_uri),
        assignment_store=SqlAlchemyAssignmentStore(db_uri),
        feature_flags=resolve_feature_flags({"OMNIGENT_FEATURES": "project_assignments"}),
    )


@pytest.fixture()
def app(db_uri: str, tmp_path: Path) -> FastAPI:
    return _build_app(db_uri, tmp_path)


@pytest_asyncio.fixture()
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _uid(seed: str) -> str:
    return uuid.uuid5(uuid.NAMESPACE_DNS, seed).hex


def _seed(
    db_uri: str, tmp_path: Path, name: str, *, project_name: str | None = None
) -> dict[str, Any]:
    """Project + repo + binding + session wired to a real local remote."""
    agent_store = SqlAlchemyAgentStore(db_uri)
    if agent_store.get(AGENT_ID) is None:
        agent_store.create(agent_id=AGENT_ID, name="t", bundle_location=f"{AGENT_ID}/b")
    remote, source = _make_remote_and_source(tmp_path, name)
    _write_manifest(source)
    commit = _commit_all(source, "add manifest")
    project_store = SqlAlchemyProjectStore(db_uri)
    project_id = _uid(f"{name}-proj-{tmp_path.name}")
    project_store.create(project_id, project_name or f"P-{name}", None)
    project_store.set_collaboration(project_id, user_id=None, enabled=True, expected_revision=0)
    repo = SqlAlchemyProjectRepositoryStore(db_uri).upsert(
        project_id=project_id,
        name=name,
        remote_url=str(remote),
        default_branch="main",
    )
    SqlAlchemyProjectHostBindingStore(db_uri).upsert(
        project_id=project_id,
        host_id=_HOST_ID,
        name="primary",
        repository_id=repo.id,
        workspace=str(source),
    )
    HostStore(db_uri).upsert_on_connect(_HOST_ID, "box", "local")
    conv = SqlAlchemyConversationStore(db_uri).create_conversation(
        title=f"s-{name}", agent_id=AGENT_ID, project_id=project_id
    )
    SqlAlchemyConversationStore(db_uri).set_host_id(conv.id, _HOST_ID, workspace=str(source))
    return {
        "project_id": project_id,
        "session_id": conv.id,
        "remote": remote,
        "source": source,
        "commit": commit,
        "repo": repo,
    }


def _dispatch_args(commit: str, key: str = "k1", **extra: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "target_agent_id": AGENT_ID,
        "task": "Do the thing",
        "repositories": [{"repository_name": "root", "commit": commit}],
        "idempotency_key": key,
    }
    args.update(extra)
    return args


async def _dispatch(
    session_id: str,
    client: httpx.AsyncClient,
    args: dict[str, Any],
    *,
    workspace: Path | None = None,
) -> dict[str, Any]:
    out = await execute_assignment_tool(
        "sys_assignment_dispatch",
        json.dumps(args),
        conversation_id=session_id,
        runner_workspace=workspace,
        server_client=client,  # type: ignore[arg-type]
    )
    return json.loads(out)


async def test_dispatch_happy_path(
    app: FastAPI, client: httpx.AsyncClient, db_uri: str, tmp_path: Path
) -> None:
    """The row reaches waiting, the ref lands, and the digest matches."""
    seed = _seed(db_uri, tmp_path, "root")
    row = await _dispatch(seed["session_id"], client, _dispatch_args(seed["commit"]))
    assert row["state"] == "waiting", row
    expected_id = hashlib.sha256(
        f"assignment-dispatch:{seed['session_id']}:k1".encode()
    ).hexdigest()[:32]
    assert row["id"] == expected_id
    ref = f"refs/omnigent/assignments/{expected_id}/input/root"
    assert _ls_remote(seed["remote"], ref) == seed["commit"]
    blob = _git_ok(seed["source"], "cat-file", "blob", f"{seed['commit']}:{_MANIFEST_PATH}")
    assert row["inputs"][0]["manifest_digest"] == manifest_digest(blob.encode())
    assert row["inputs"][0]["context_manifest_path"] == _MANIFEST_PATH


async def test_dispatch_idempotent_same_call_twice(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario 7: a retry reuses the assignment and pushes once only."""
    import omnigent.runner.assignment_tools as assignment_tools

    pushes = 0
    real_run_git = assignment_tools._run_git

    def _counting_run_git(args: list[str], **kwargs: Any) -> Any:
        nonlocal pushes
        if args and args[0] == "push":
            pushes += 1
        return real_run_git(args, **kwargs)

    monkeypatch.setattr(assignment_tools, "_run_git", _counting_run_git)
    seed = _seed(db_uri, tmp_path, "root")
    args = _dispatch_args(seed["commit"])
    first = await _dispatch(seed["session_id"], client, args)
    assert first["state"] == "waiting", first
    second = await _dispatch(seed["session_id"], client, args)
    assert second["id"] == first["id"]
    assert second["state"] == "waiting"
    assert pushes == 1

    changed = _dispatch_args(seed["commit"], task="A different task")
    out = await _dispatch(seed["session_id"], client, changed)
    assert "409" in out["error"], out
    assert pushes == 1
    listed = (await client.get("/v1/assignments")).json()
    assert [row["id"] for row in listed["data"]] == [first["id"]]


def _seed_second_repo(
    db_uri: str, tmp_path: Path, seed: dict[str, Any], name: str = "docs"
) -> dict[str, Any]:
    """Register a second repository + binding on the seed's project/host."""
    remote, source = _make_remote_and_source(tmp_path, name)
    _write_manifest(source)
    commit = _commit_all(source, "add manifest")
    repo = SqlAlchemyProjectRepositoryStore(db_uri).upsert(
        project_id=seed["project_id"],
        name=name,
        remote_url=str(remote),
        default_branch="main",
    )
    SqlAlchemyProjectHostBindingStore(db_uri).upsert(
        project_id=seed["project_id"],
        host_id=_HOST_ID,
        name=f"{name}-binding",
        repository_id=repo.id,
        workspace=str(source),
    )
    return {"remote": remote, "source": source, "commit": commit}


async def test_dispatch_partial_publication_fails_row(
    app: FastAPI, client: httpx.AsyncClient, db_uri: str, tmp_path: Path
) -> None:
    """Scenario 15: a pre-existing foreign ref fails the row, naming it."""
    seed = _seed(db_uri, tmp_path, "root")
    docs = _seed_second_repo(db_uri, tmp_path, seed)
    key = "two-repo"
    assignment_id = hashlib.sha256(
        f"assignment-dispatch:{seed['session_id']}:{key}".encode()
    ).hexdigest()[:32]
    (docs["source"] / "foreign.txt").write_text("someone else landed first\n")
    foreign = _commit_all(docs["source"], "foreign work")
    _git_ok(
        docs["source"],
        "push",
        "-q",
        "origin",
        f"{foreign}:refs/omnigent/assignments/{assignment_id}/input/docs",
    )
    row = await _dispatch(
        seed["session_id"],
        client,
        {
            "target_agent_id": AGENT_ID,
            "task": "Two repos",
            "repositories": [
                {"repository_name": "root", "commit": seed["commit"]},
                {"repository_name": "docs", "commit": docs["commit"]},
            ],
            "idempotency_key": key,
            "execution_root": "root",
        },
    )
    assert row["state"] == "failed", row
    assert row["error_code"] == "publication_failed"
    assert "docs" in row["error"], row
    by_name = {item["repository_name"]: item for item in row["inputs"]}
    assert by_name["root"]["observed_commit"] == seed["commit"]
    assert by_name["docs"]["observed_commit"] is None
    assert _ls_remote(seed["remote"], f"refs/omnigent/assignments/{assignment_id}/input/root")
    assert (
        _ls_remote(docs["remote"], f"refs/omnigent/assignments/{assignment_id}/input/docs")
        == foreign
    )


async def test_dispatch_refuses_disabled_collaboration(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A project with the switch off is refused naming the project."""
    import omnigent.runner.assignment_tools as assignment_tools

    pushes = 0
    real_run_git = assignment_tools._run_git

    def _counting_run_git(args: list[str], **kwargs: Any) -> Any:
        nonlocal pushes
        if args and args[0] == "push":
            pushes += 1
        return real_run_git(args, **kwargs)

    monkeypatch.setattr(assignment_tools, "_run_git", _counting_run_git)
    agent_store = SqlAlchemyAgentStore(db_uri)
    if agent_store.get(AGENT_ID) is None:
        agent_store.create(agent_id=AGENT_ID, name="t", bundle_location=f"{AGENT_ID}/b")
    remote, source = _make_remote_and_source(tmp_path, "off")
    _write_manifest(source)
    commit = _commit_all(source, "add manifest")
    project_store = SqlAlchemyProjectStore(db_uri)
    project_id = _uid(f"off-proj-{tmp_path.name}")
    project_store.create(project_id, "Quiet Project", None)
    SqlAlchemyProjectRepositoryStore(db_uri).upsert(
        project_id=project_id, name="off", remote_url=str(remote), default_branch="main"
    )
    conv = SqlAlchemyConversationStore(db_uri).create_conversation(
        title="s-off", agent_id=AGENT_ID, project_id=project_id
    )
    SqlAlchemyConversationStore(db_uri).set_host_id(conv.id, _HOST_ID, workspace=str(source))
    out = await _dispatch(
        conv.id,
        client,
        {
            "target_agent_id": AGENT_ID,
            "task": "x",
            "repositories": [{"repository_name": "off", "commit": commit}],
            "idempotency_key": "k-off",
        },
    )
    assert "Quiet Project" in out["error"], out
    assert "collaboration disabled" in out["error"]
    assert (await client.get("/v1/assignments")).json()["data"] == []
    assert pushes == 0


async def test_dispatch_refusals_create_no_row(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No binding, an absent manifest and a bad commit are refused naming."""
    import omnigent.runner.assignment_tools as assignment_tools

    pushes = 0
    real_run_git = assignment_tools._run_git

    def _counting_run_git(args: list[str], **kwargs: Any) -> Any:
        nonlocal pushes
        if args and args[0] == "push":
            pushes += 1
        return real_run_git(args, **kwargs)

    monkeypatch.setattr(assignment_tools, "_run_git", _counting_run_git)
    seed = _seed(db_uri, tmp_path, "root")
    store = SqlAlchemyConversationStore(db_uri)

    orphan = store.create_conversation(
        title="s-orphan", agent_id=AGENT_ID, project_id=seed["project_id"]
    )
    store.set_host_id(orphan.id, "b1b2c3d4e5f60718293a4b5c6d7e8f02", workspace=str(seed["source"]))
    out = await _dispatch(orphan.id, client, _dispatch_args(seed["commit"], key="k-nobind"))
    assert "'root'" in out["error"] and "no enabled binding" in out["error"], out

    bare = store.create_conversation(
        title="s-bare", agent_id=AGENT_ID, project_id=seed["project_id"]
    )
    store.set_host_id(bare.id, _HOST_ID, workspace=str(seed["source"]))
    (seed["source"] / "uncommitted.txt").write_text("not committed\n")
    out = await _dispatch(bare.id, client, _dispatch_args("0" * 40, key="k-badcommit"))
    assert "'root'" in out["error"] and "0" * 40 in out["error"], out

    remote2, source2 = _make_remote_and_source(tmp_path, "nomanifest")
    commit2 = _git_ok(source2, "rev-parse", "HEAD")
    SqlAlchemyProjectRepositoryStore(db_uri).upsert(
        project_id=seed["project_id"], name="plain", remote_url=str(remote2), default_branch="main"
    )
    SqlAlchemyProjectHostBindingStore(db_uri).upsert(
        project_id=seed["project_id"],
        host_id=_HOST_ID,
        name="plain-binding",
        repository_id=SqlAlchemyProjectRepositoryStore(db_uri)
        .get_by_name(project_id=seed["project_id"], name="plain")
        .id,
        workspace=str(source2),
    )
    out = await _dispatch(
        bare.id,
        client,
        {
            "target_agent_id": AGENT_ID,
            "task": "x",
            "repositories": [{"repository_name": "plain", "commit": commit2}],
            "idempotency_key": "k-nomanifest",
        },
    )
    assert "'plain'" in out["error"] and _MANIFEST_PATH in out["error"], out
    assert (await client.get("/v1/assignments")).json()["data"] == []
    assert pushes == 0


def _runner_client_for(app: FastAPI, token: str) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={RUNNER_TUNNEL_TOKEN_HEADER: token},
    )


def _drive_to_running(db_uri: str, assignment_id: str, session_id: str) -> tuple[str, str]:
    """Claim an attempt bound to ``session_id`` and move the row to running."""
    token = secrets.token_hex(16)
    runner_id = token_bound_runner_id(token)
    bound = SqlAlchemyConversationStore(db_uri).create_conversation(
        title="recv", agent_id=AGENT_ID, runner_id=runner_id
    )
    store = SqlAlchemyAssignmentStore(db_uri)
    attempt = store.claim_attempt(assignment_id, host_id=_HOST_ID, now=now_epoch())
    assert attempt is not None
    updated = store.update_attempt(
        assignment_id, attempt.id, session_id=bound.id, runner_id=runner_id
    )
    assert updated is not None
    moved = store.transition(assignment_id, from_state="starting", to_state="running")
    assert moved is not None
    return token, bound.id


async def _complete(
    session_id: str,
    authed: httpx.AsyncClient,
    assignment_id: str,
    outputs: list[dict[str, Any]],
    workspace: Path,
) -> dict[str, Any]:
    out = await execute_assignment_tool(
        "sys_assignment_complete",
        json.dumps({"assignment_id": assignment_id, "outputs": outputs, "summary": "done"}),
        conversation_id=session_id,
        runner_workspace=workspace,
        server_client=authed,  # type: ignore[arg-type]
    )
    return json.loads(out)


async def test_complete_happy_path(
    app: FastAPI, client: httpx.AsyncClient, db_uri: str, tmp_path: Path
) -> None:
    """Both repos complete from their prepared worktrees and succeed."""
    seed = _seed(db_uri, tmp_path, "root")
    docs = _seed_second_repo(db_uri, tmp_path, seed)
    row = await _dispatch(
        seed["session_id"],
        client,
        {
            "target_agent_id": AGENT_ID,
            "task": "Two repos",
            "repositories": [
                {"repository_name": "root", "commit": seed["commit"]},
                {"repository_name": "docs", "commit": docs["commit"]},
            ],
            "idempotency_key": "two-complete",
            "execution_root": "root",
        },
    )
    assert row["state"] == "waiting", row
    assignment_id = row["id"]
    token, recv_session = _drive_to_running(db_uri, assignment_id, seed["session_id"])
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv_store.set_conversation_project(recv_session, seed["project_id"])
    conv_store.set_host_id(recv_session, _HOST_ID, workspace="/tmp/recv")
    root_wt = seed["source"] / ".omnigent" / "worktrees" / assignment_id / "root"
    docs_top = Path(_git_ok(docs["source"], "rev-parse", "--show-toplevel"))
    docs_wt = docs_top / ".omnigent" / "worktrees" / assignment_id / "docs"
    for worktree, source in ((root_wt, seed["source"]), (docs_wt, docs["source"])):
        worktree.parent.mkdir(parents=True, exist_ok=True)
        _git_ok(
            source,
            "worktree",
            "add",
            str(worktree),
            "-b",
            f"wt-{worktree.name}-{assignment_id[:8]}",
        )
    (root_wt / "work.txt").write_text("finished root\n")
    root_out = _commit_all(root_wt, "finish root work")
    (docs_wt / "work.txt").write_text("finished docs\n")
    docs_out = _commit_all(docs_wt, "finish docs work")
    async with _runner_client_for(app, token) as authed:
        result = await _complete(
            recv_session,
            authed,
            assignment_id,
            [
                {"repository_name": "root", "commit": root_out},
                {"repository_name": "docs", "commit": docs_out},
            ],
            root_wt,
        )
    assert result["state"] == "succeeded", result
    attempt_id = SqlAlchemyAssignmentStore(db_uri).get(assignment_id).active_attempt_id
    assert (
        _ls_remote(
            seed["remote"], f"refs/omnigent/assignments/{assignment_id}/output/{attempt_id}/root"
        )
        == root_out
    )
    assert (
        _ls_remote(
            docs["remote"], f"refs/omnigent/assignments/{assignment_id}/output/{attempt_id}/docs"
        )
        == docs_out
    )


async def test_complete_rejected_push_fails_row(
    app: FastAPI, client: httpx.AsyncClient, db_uri: str, tmp_path: Path
) -> None:
    """A pre-existing foreign output ref fails publication naming the repo."""
    seed = _seed(db_uri, tmp_path, "root")
    row = await _dispatch(seed["session_id"], client, _dispatch_args(seed["commit"]))
    assert row["state"] == "waiting", row
    token, recv_session = _drive_to_running(db_uri, row["id"], seed["session_id"])
    attempt_id = SqlAlchemyAssignmentStore(db_uri).get(row["id"]).active_attempt_id
    output_ref = f"refs/omnigent/assignments/{row['id']}/output/{attempt_id}/root"
    (seed["source"] / "foreign.txt").write_text("someone else published first\n")
    foreign = _commit_all(seed["source"], "foreign output")
    _git_ok(seed["source"], "push", "-q", "origin", f"{foreign}:{output_ref}")
    (seed["source"] / "work.txt").write_text("finished\n")
    output_commit = _commit_all(seed["source"], "finish the work")
    async with _runner_client_for(app, token) as authed:
        result = await _complete(
            recv_session,
            authed,
            row["id"],
            [{"repository_name": "root", "commit": output_commit}],
            seed["source"],
        )
    assert result["state"] == "failed", result
    assert result["error_code"] == "publication_failed"
    assert "root" in result["error"], result
    assert _ls_remote(seed["remote"], output_ref) == foreign


async def test_push_is_create_only(tmp_path: Path) -> None:
    """`_push` creates an absent ref but refuses one sitting at an ancestor."""
    import omnigent.runner.assignment_tools as assignment_tools

    remote, source = _make_remote_and_source(tmp_path, "lease")
    _write_manifest(source)
    ancestor = _commit_all(source, "add manifest")
    (source / "more.txt").write_text("more\n")
    descendant = _commit_all(source, "more work")
    ref = "refs/omnigent/assignments/lease-check/input/root"

    await assignment_tools._push(str(source), str(remote), descendant, ref)
    assert _ls_remote(remote, ref) == descendant
    _git_ok(source, "push", "-q", "origin", f":{ref}")

    _git_ok(source, "push", "-q", "origin", f"{ancestor}:{ref}")
    with pytest.raises(assignment_tools.AssignmentToolError):
        await assignment_tools._push(str(source), str(remote), descendant, ref)
    assert _ls_remote(remote, ref) == ancestor


async def test_ls_remote_and_push_take_double_dash_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A URL starting with `-` is never parsed as an option."""
    import omnigent.runner.assignment_tools as assignment_tools

    seen: list[list[str]] = []

    def _recording_run_git(args: list[str], **kwargs: Any) -> Any:
        seen.append(list(args))
        raise RuntimeError("stop before exec")

    monkeypatch.setattr(assignment_tools, "_run_git", _recording_run_git)
    with pytest.raises(RuntimeError, match="stop before exec"):
        await assignment_tools._ls_remote(str(tmp_path), "-evil", "refs/x")
    with pytest.raises(RuntimeError, match="stop before exec"):
        await assignment_tools._push(str(tmp_path), "-evil", "a" * 40, "refs/x")
    assert seen[0][:3] == ["ls-remote", "--", "-evil"]
    assert seen[1][:3] == ["push", "--force-with-lease=refs/x:", "--"]
    assert seen[1][3] == "-evil"


async def test_dispatch_ancestor_ref_is_not_fast_forwarded(
    app: FastAPI, client: httpx.AsyncClient, db_uri: str, tmp_path: Path
) -> None:
    """A ref created at an ancestor in between is refused, never fast-forwarded."""
    seed = _seed(db_uri, tmp_path, "root")
    key = "ancestor-race"
    assignment_id = hashlib.sha256(
        f"assignment-dispatch:{seed['session_id']}:{key}".encode()
    ).hexdigest()[:32]
    ref = f"refs/omnigent/assignments/{assignment_id}/input/root"
    ancestor = _git_ok(seed["source"], "rev-parse", "HEAD~1")
    _git_ok(seed["source"], "push", "-q", "origin", f"{ancestor}:{ref}")
    row = await _dispatch(seed["session_id"], client, _dispatch_args(seed["commit"], key=key))
    assert row["state"] == "failed", row
    assert row["error_code"] == "publication_failed"
    assert "root" in row["error"], row
    assert _ls_remote(seed["remote"], ref) == ancestor


async def test_dispatch_lost_ack_still_lands(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A push that landed but lost its ack is reported as landed."""
    import omnigent.runner.assignment_tools as assignment_tools

    real_push = assignment_tools._push

    async def _push_then_lose_ack(directory: str, url: str, commit: str, ref: str) -> None:
        await real_push(directory, url, commit, ref)
        raise assignment_tools.AssignmentToolError("ack lost")

    monkeypatch.setattr(assignment_tools, "_push", _push_then_lose_ack)
    seed = _seed(db_uri, tmp_path, "root")
    row = await _dispatch(seed["session_id"], client, _dispatch_args(seed["commit"], key="k-ack"))
    assert row["state"] == "waiting", row
    ref = f"refs/omnigent/assignments/{row['id']}/input/root"
    assert _ls_remote(seed["remote"], ref) == seed["commit"]


async def test_complete_lost_ack_still_lands(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Complete observes after a raising push and reports the landed ref."""
    import omnigent.runner.assignment_tools as assignment_tools

    real_push = assignment_tools._push

    async def _push_then_lose_ack(directory: str, url: str, commit: str, ref: str) -> None:
        await real_push(directory, url, commit, ref)
        raise assignment_tools.AssignmentToolError("ack lost")

    monkeypatch.setattr(assignment_tools, "_push", _push_then_lose_ack)
    seed = _seed(db_uri, tmp_path, "root")
    row = await _dispatch(seed["session_id"], client, _dispatch_args(seed["commit"], key="k-cack"))
    assert row["state"] == "waiting", row
    token, recv_session = _drive_to_running(db_uri, row["id"], seed["session_id"])
    (seed["source"] / "work.txt").write_text("finished\n")
    output_commit = _commit_all(seed["source"], "finish the work")
    async with _runner_client_for(app, token) as authed:
        result = await _complete(
            recv_session,
            authed,
            row["id"],
            [{"repository_name": "root", "commit": output_commit}],
            seed["source"],
        )
    assert result["state"] == "succeeded", result
    attempt_id = SqlAlchemyAssignmentStore(db_uri).get(row["id"]).active_attempt_id
    output_ref = f"refs/omnigent/assignments/{row['id']}/output/{attempt_id}/root"
    assert _ls_remote(seed["remote"], output_ref) == output_commit


async def test_complete_retries_push_until_third_attempt(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two failed pushes without landing are retried; the third lands."""
    import omnigent.runner.assignment_tools as assignment_tools

    seed = _seed(db_uri, tmp_path, "root")
    row = await _dispatch(
        seed["session_id"], client, _dispatch_args(seed["commit"], key="k-retry")
    )
    assert row["state"] == "waiting", row
    token, recv_session = _drive_to_running(db_uri, row["id"], seed["session_id"])
    (seed["source"] / "work.txt").write_text("finished\n")
    output_commit = _commit_all(seed["source"], "finish the work")
    real_push = assignment_tools._push
    calls = 0

    async def _fail_twice_then_push(directory: str, url: str, commit: str, ref: str) -> None:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise assignment_tools.AssignmentToolError("transient push failure")
        await real_push(directory, url, commit, ref)

    monkeypatch.setattr(assignment_tools, "_push", _fail_twice_then_push)
    async with _runner_client_for(app, token) as authed:
        result = await _complete(
            recv_session,
            authed,
            row["id"],
            [{"repository_name": "root", "commit": output_commit}],
            seed["source"],
        )
    assert result["state"] == "succeeded", result
    assert calls == 3
    attempt_id = SqlAlchemyAssignmentStore(db_uri).get(row["id"]).active_attempt_id
    output_ref = f"refs/omnigent/assignments/{row['id']}/output/{attempt_id}/root"
    assert _ls_remote(seed["remote"], output_ref) == output_commit


async def test_dispatch_malformed_artifact_paths_rejected(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-string artifact_paths entry is sent verbatim; the server rejects it."""
    import omnigent.runner.assignment_tools as assignment_tools

    pushes = 0
    real_run_git = assignment_tools._run_git

    def _counting_run_git(args: list[str], **kwargs: Any) -> Any:
        nonlocal pushes
        if args and args[0] == "push":
            pushes += 1
        return real_run_git(args, **kwargs)

    monkeypatch.setattr(assignment_tools, "_run_git", _counting_run_git)
    seed = _seed(db_uri, tmp_path, "root")
    out = await _dispatch(
        seed["session_id"],
        client,
        {
            "target_agent_id": AGENT_ID,
            "task": "Do the thing",
            "repositories": [
                {
                    "repository_name": "root",
                    "commit": seed["commit"],
                    "artifact_paths": ["ok.md", 7],
                }
            ],
            "idempotency_key": "k-badpaths",
        },
    )
    assert "server returned 422" in out.get("error", ""), out
    assert (await client.get("/v1/assignments")).json()["data"] == []
    assert pushes == 0


async def test_dispatch_null_artifact_paths_rejected(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit null artifact_paths is passed through; the server rejects it."""
    import omnigent.runner.assignment_tools as assignment_tools

    pushes = 0
    real_run_git = assignment_tools._run_git

    def _counting_run_git(args: list[str], **kwargs: Any) -> Any:
        nonlocal pushes
        if args and args[0] == "push":
            pushes += 1
        return real_run_git(args, **kwargs)

    monkeypatch.setattr(assignment_tools, "_run_git", _counting_run_git)
    seed = _seed(db_uri, tmp_path, "root")
    out = await _dispatch(
        seed["session_id"],
        client,
        {
            "target_agent_id": AGENT_ID,
            "task": "Do the thing",
            "repositories": [
                {
                    "repository_name": "root",
                    "commit": seed["commit"],
                    "artifact_paths": None,
                }
            ],
            "idempotency_key": "k-nullpaths",
        },
    )
    assert "server returned 422" in out.get("error", ""), out
    assert (await client.get("/v1/assignments")).json()["data"] == []
    assert pushes == 0


async def test_dispatch_server_error_redacts_artifact_secret(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 422 echoing a credential-shaped artifact path is redacted in the result."""
    import omnigent.runner.assignment_tools as assignment_tools

    pushes = 0
    real_run_git = assignment_tools._run_git

    def _counting_run_git(args: list[str], **kwargs: Any) -> Any:
        nonlocal pushes
        if args and args[0] == "push":
            pushes += 1
        return real_run_git(args, **kwargs)

    monkeypatch.setattr(assignment_tools, "_run_git", _counting_run_git)
    seed = _seed(db_uri, tmp_path, "root")
    out = await _dispatch(
        seed["session_id"],
        client,
        {
            "target_agent_id": AGENT_ID,
            "task": "Do the thing",
            "repositories": [
                {
                    "repository_name": "root",
                    "commit": seed["commit"],
                    "artifact_paths": [
                        "https://user:s3cret-marker@example.invalid/x",
                        {"url": "https://user:s3cret-marker@example.invalid/x"},
                    ],
                }
            ],
            "idempotency_key": "k-secretpaths",
        },
    )
    assert "server returned 422" in out.get("error", ""), out
    assert "s3cret-marker" not in json.dumps(out), out
    assert (await client.get("/v1/assignments")).json()["data"] == []
    assert pushes == 0


async def test_publish_ref_ignores_lookalike_ref(tmp_path: Path) -> None:
    """A tail-matching longer ref is not the requested ref: it is pushed, not read."""
    import omnigent.runner.assignment_tools as assignment_tools

    remote, source = _make_remote_and_source(tmp_path, "lookalike")
    _write_manifest(source)
    commit = _commit_all(source, "add manifest")
    requested = "refs/omnigent/assignments/lookalike-case/input/root"
    lookalike = f"refs/heads/shadow/{requested}"
    (source / "other.txt").write_text("elsewhere\n")
    other = _commit_all(source, "other work")
    _git_ok(source, "push", "-q", "origin", f"{other}:{lookalike}")
    assert _ls_remote(remote, requested) is None
    landed, reason = await assignment_tools._publish_ref(
        str(source), str(remote), commit, requested, attempts=1
    )
    assert landed == commit, reason
    assert _ls_remote(remote, requested) == commit
    assert _ls_remote(remote, lookalike) == other


async def test_dispatch_rows_keep_registered_remote_url(
    app: FastAPI, client: httpx.AsyncClient, db_uri: str, tmp_path: Path
) -> None:
    """Server rows echo the registered remote verbatim; only the error is scrubbed."""
    agent_store = SqlAlchemyAgentStore(db_uri)
    if agent_store.get(AGENT_ID) is None:
        agent_store.create(agent_id=AGENT_ID, name="t", bundle_location=f"{AGENT_ID}/b")
    _remote, source = _make_remote_and_source(tmp_path, "sshecho")
    _write_manifest(source)
    commit = _commit_all(source, "add manifest")
    project_store = SqlAlchemyProjectStore(db_uri)
    project_id = _uid(f"sshecho-proj-{tmp_path.name}")
    project_store.create(project_id, "Echo Project", None)
    project_store.set_collaboration(project_id, user_id=None, enabled=True, expected_revision=0)
    # A username-only ssh remote is valid registration (no password to store).
    ssh_url = "ssh://git@127.0.0.1:9/org/repo.git"
    repo = SqlAlchemyProjectRepositoryStore(db_uri).upsert(
        project_id=project_id,
        name="sshecho",
        remote_url=ssh_url,
        default_branch="main",
    )
    SqlAlchemyProjectHostBindingStore(db_uri).upsert(
        project_id=project_id,
        host_id=_HOST_ID,
        name="primary",
        repository_id=repo.id,
        workspace=str(source),
    )
    HostStore(db_uri).upsert_on_connect(_HOST_ID, "box", "local")
    conv = SqlAlchemyConversationStore(db_uri).create_conversation(
        title="s-sshecho", agent_id=AGENT_ID, project_id=project_id
    )
    SqlAlchemyConversationStore(db_uri).set_host_id(conv.id, _HOST_ID, workspace=str(source))
    out = await _dispatch(
        conv.id,
        client,
        {
            "target_agent_id": AGENT_ID,
            "task": "x",
            "repositories": [{"repository_name": "sshecho", "commit": commit}],
            "idempotency_key": "k-sshecho",
        },
    )
    assert out["state"] == "failed", out
    assert out["inputs"][0]["remote_url"] == ssh_url, out
    assert "ssh://git@" not in out["error"], out


def _straddling_stderr() -> tuple[str, str]:
    """Stderr whose credential userinfo crosses the 200-char diagnostic cut."""
    secret = "s3cret-straddle-marker-pw"
    stderr = "E" * 178 + f" https://bot:{secret}@githost.internal/org/repo.git: access denied"
    assert secret in stderr
    assert stderr.index("@githost") > 200, "the userinfo must cross the cut"
    assert "s3cret-st" in stderr[:200], "the cut must keep a recognizable fragment"
    return stderr, secret


async def test_dispatch_redaction_precedes_truncation(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A secret split by the diagnostic cut is scrubbed before cutting."""
    import omnigent.runner.assignment_tools as assignment_tools

    stderr, secret = _straddling_stderr()
    real_run_git = assignment_tools._run_git

    def _failing_ls_remote(args: list[str], **kwargs: Any) -> Any:
        if args and args[0] == "ls-remote":
            return subprocess.CompletedProcess(args, returncode=128, stdout="", stderr=stderr)
        return real_run_git(args, **kwargs)

    monkeypatch.setattr(assignment_tools, "_run_git", _failing_ls_remote)
    seed = _seed(db_uri, tmp_path, "root")
    out = await _dispatch(seed["session_id"], client, _dispatch_args(seed["commit"], key="k-cut"))
    assert out["state"] == "failed", out
    assert secret not in out["error"], out
    assert "s3cret-st" not in out["error"], out


async def test_complete_redaction_precedes_truncation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither the complete error nor the /finish error keeps a split secret."""
    import omnigent.runner.assignment_tools as assignment_tools
    from omnigent.runner.assignment_tools import execute_assignment_tool

    stderr, secret = _straddling_stderr()
    real_run_git = assignment_tools._run_git

    def _failing_git(args: list[str], **kwargs: Any) -> Any:
        if args and args[0] in ("ls-remote", "push"):
            return subprocess.CompletedProcess(args, returncode=128, stdout="", stderr=stderr)
        return real_run_git(args, **kwargs)

    monkeypatch.setattr(assignment_tools, "_run_git", _failing_git)
    _remote, source = _make_remote_and_source(tmp_path, "cut-complete")
    (source / "work.txt").write_text("done\n")
    commit = _commit_all(source, "work")
    assignment_id = "b" * 32
    output_ref = f"refs/omnigent/assignments/{assignment_id}/output/att1/root"
    remote_url = str(_remote)
    finished_bodies: list[dict[str, Any]] = []

    class _Resp:
        def __init__(self, status_code: int = 200, body: Any = None) -> None:
            self.status_code = status_code
            self._body = body if body is not None else {}

        @property
        def text(self) -> str:
            return json.dumps(self._body)

        def json(self) -> Any:
            return self._body

    class _FakeClient:
        async def get(self, url: str, **kwargs: Any) -> _Resp:
            if url.startswith("/v1/assignments/"):
                return _Resp(
                    body={
                        "id": assignment_id,
                        "state": "running",
                        "inputs": [
                            {
                                "repository_name": "root",
                                "remote_url": remote_url,
                                "is_execution_root": True,
                            }
                        ],
                    }
                )
            return _Resp(body={"project_id": "p1", "host_id": _HOST_ID})

        async def post(self, url: str, **kwargs: Any) -> _Resp:
            payload = kwargs.get("json")
            if url.endswith("/complete"):
                return _Resp(
                    body={
                        "id": assignment_id,
                        "state": "publishing",
                        "outputs": [{"repository_name": "root", "ref": output_ref}],
                    }
                )
            if url.endswith("/finish"):
                finished_bodies.append(payload)
                return _Resp(body={"id": assignment_id, "state": "failed"})
            raise AssertionError(f"unexpected POST {url}")

    out = json.loads(
        await execute_assignment_tool(
            "sys_assignment_complete",
            json.dumps(
                {
                    "assignment_id": assignment_id,
                    "outputs": [{"repository_name": "root", "commit": commit}],
                    "summary": "done",
                }
            ),
            conversation_id="c" * 32,
            runner_workspace=source,
            server_client=_FakeClient(),  # type: ignore[arg-type]
        )
    )
    assert secret not in out.get("error", ""), out
    assert "s3cret-st" not in out.get("error", ""), out
    assert finished_bodies
    assert secret not in json.dumps([body.get("error") for body in finished_bodies])
    assert "s3cret-st" not in json.dumps([body.get("error") for body in finished_bodies])


async def test_complete_secret_url_failure_is_redacted(tmp_path: Path) -> None:
    """A failing secret URL reaches neither the tool error nor the finish error."""
    from omnigent.runner.assignment_tools import execute_assignment_tool

    _remote, source = _make_remote_and_source(tmp_path, "redact-complete")
    (source / "work.txt").write_text("done\n")
    commit = _commit_all(source, "work")
    assignment_id = "a" * 32
    output_ref = f"refs/omnigent/assignments/{assignment_id}/output/att1/root"
    secret_url = "https://user:s3cret-value@127.0.0.1:9/org/repo.git"
    finished_bodies: list[dict[str, Any]] = []

    class _Resp:
        def __init__(self, status_code: int = 200, body: Any = None) -> None:
            self.status_code = status_code
            self._body = body if body is not None else {}

        @property
        def text(self) -> str:
            return json.dumps(self._body)

        def json(self) -> Any:
            return self._body

    class _FakeClient:
        async def get(self, url: str, **kwargs: Any) -> _Resp:
            if url.startswith("/v1/assignments/"):
                return _Resp(
                    body={
                        "id": assignment_id,
                        "state": "running",
                        "inputs": [
                            {
                                "repository_name": "root",
                                "remote_url": secret_url,
                                "is_execution_root": True,
                            }
                        ],
                    }
                )
            return _Resp(body={"project_id": "p1", "host_id": _HOST_ID})

        async def post(self, url: str, **kwargs: Any) -> _Resp:
            payload = kwargs.get("json")
            if url.endswith("/complete"):
                return _Resp(
                    body={
                        "id": assignment_id,
                        "state": "publishing",
                        "outputs": [{"repository_name": "root", "ref": output_ref}],
                    }
                )
            if url.endswith("/finish"):
                finished_bodies.append(payload)
                return _Resp(body={"id": assignment_id, "state": "failed"})
            raise AssertionError(f"unexpected POST {url}")

    out = json.loads(
        await execute_assignment_tool(
            "sys_assignment_complete",
            json.dumps(
                {
                    "assignment_id": assignment_id,
                    "outputs": [{"repository_name": "root", "commit": commit}],
                    "summary": "done",
                }
            ),
            conversation_id="c" * 32,
            runner_workspace=source,
            server_client=_FakeClient(),  # type: ignore[arg-type]
        )
    )
    assert "s3cret-value" not in out.get("error", ""), out
    assert finished_bodies
    assert "s3cret-value" not in json.dumps([body.get("error") for body in finished_bodies])
