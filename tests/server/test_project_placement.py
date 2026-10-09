"""Project roots and default-host choices."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from omnigent.entities import Project, ProjectHostBinding, ProjectHostEntry, ProjectRepository
from omnigent.server.project_placement import (
    HostRoot,
    checkout_on_host,
    default_host,
    host_roots,
    load_bindings,
    load_eligible_host_ids,
    load_entries,
    resolve_new_branch_base,
    root_on_host,
)
from omnigent.server.routes._sessions.helpers import _place_project_session


def _project(*, host_id: str | None = "h1", workspace: str = "/config") -> Project:
    config = {"workspace": workspace}
    if host_id is not None:
        config["host_id"] = host_id
    return Project("p1", "Project", "alice", 1, config=config)


def _binding(host_id: str, workspace: str = "/binding") -> ProjectHostBinding:
    return ProjectHostBinding(
        "b1", "p1", host_id, "primary", "repo", workspace, 1, 1, is_primary=True
    )


def _entry(host_id: str, workspace: str = "/entry") -> ProjectHostEntry:
    return ProjectHostEntry("p1", host_id, workspace, 1)


def test_binding_precedes_config_without_a_flag() -> None:
    """A primary enabled binding is the root whenever the project has no entries."""
    project = _project()
    binding = _binding("h1")
    assert root_on_host(project, [binding], "h1").workspace == "/binding"
    # No binding supplies a root: the config host stays the legacy fallback.
    assert root_on_host(project, [], "h1").workspace == "/config"
    assert root_on_host(project, [replace(binding, enabled=False)], "h1").workspace == "/config"
    assert root_on_host(project, [replace(binding, is_primary=False)], "h1").workspace == "/config"


def test_roots_and_default_host_obey_eligibility_and_sandbox_sentinel() -> None:
    project = _project(host_id=None)
    roots = host_roots(project, [_binding("h1"), replace(_binding("h2"), id="b2")])
    assert [root.host_id for root in roots] == ["h1", "h2"]
    assert (
        default_host(project, roots, eligible_host_ids=frozenset({"h1"})).reason == "single_root"
    )
    assert default_host(project, roots, eligible_host_ids=frozenset()).reason == "none"
    assert default_host(project, roots).reason == "ambiguous"
    assert (
        default_host(_project(host_id="h9"), roots, eligible_host_ids=frozenset({"h1"})).host_id
        == "h9"
    )
    sandbox = _project(host_id="__sandbox__")
    assert host_roots(sandbox, []) == []
    assert default_host(sandbox, roots).reason == "none"


@pytest.mark.asyncio
async def test_loaders_filter_deleted_and_foreign_hosts() -> None:
    class Bindings:
        def list_by_project(self, project_id: str) -> list[ProjectHostBinding]:
            assert project_id == "p1"
            return [_binding("h1")]

        def list_entries(self, project_id: str) -> list[ProjectHostEntry]:
            assert project_id == "p1"
            return [_entry("h1")]

    class Hosts:
        def get_host(self, host_id: str) -> object | None:
            owners = {"h1": "alice", "h2": "bob"}
            owner = owners.get(host_id)
            return (
                None
                if owner is None
                else type("Host", (), {"host_id": host_id, "user_id": owner})()
            )

    assert await load_bindings(None, "p1") == []
    assert len(await load_bindings(Bindings(), "p1")) == 1  # type: ignore[arg-type]
    assert await load_entries(None, "p1") == []
    assert await load_entries(Bindings(), "p1") == [_entry("h1")]  # type: ignore[arg-type]
    assert await load_eligible_host_ids(None, "alice", ["h1"]) is None
    assert await load_eligible_host_ids(Hosts(), "alice", ["h1", "h2", "h9"]) == frozenset({"h1"})  # type: ignore[arg-type]
    assert await load_eligible_host_ids(Hosts(), None, ["h1", "h2", "h9"]) == frozenset(
        {"h1", "h2"}
    )  # type: ignore[arg-type]


# ── R-ROOT with entries ─────────────────────────────────────────────────


def test_entry_outranks_binding_and_config_with_checkout() -> None:
    """An entry is the root; the binding stays the checkout."""
    project = _project(workspace="/config")
    binding = _binding("h1", "/binding")
    entries = [_entry("h1", "/entry")]
    root = root_on_host(project, [binding], "h1", entries=entries)
    assert root == HostRoot("h1", "/entry", "entry", "/binding")
    roots = host_roots(project, [binding], entries=entries)
    assert roots == [root]


def test_entry_hosts_only_and_no_fallback_after_removal() -> None:
    """With entries, a host without one has no root — no binding/config fallback."""
    project = _project(workspace="/config")
    binding = _binding("h1", "/binding")
    entries = [_entry("h2", "/other")]
    assert root_on_host(project, [binding], "h1", entries=entries) is None
    assert host_roots(project, [binding], entries=entries) == [
        HostRoot("h2", "/other", "entry", "/other")
    ]


def test_entries_keep_sandbox_and_legacy_paths_intact() -> None:
    """``__sandbox__`` has no root; a project without entries keeps binding → config."""
    project = _project(workspace="/config")
    binding = _binding("h1", "/binding")
    entries = [_entry("h1", "/entry")]
    assert root_on_host(project, [binding], "__sandbox__", entries=entries) is None
    assert host_roots(project, [binding], entries=entries) == [
        HostRoot("h1", "/entry", "entry", "/binding")
    ]
    legacy = root_on_host(project, [binding], "h1")
    assert (legacy.workspace, legacy.source, legacy.checkout) == (
        "/binding",
        "binding",
        "/binding",
    )
    config = root_on_host(project, [replace(binding, enabled=False)], "h1")
    assert (config.workspace, config.source, config.checkout) == ("/config", "config", None)
    assert root_on_host(project, [], "__sandbox__") is None


# ── R-CHECKOUT ──────────────────────────────────────────────────────────


def test_checkout_prefers_binding_then_entry() -> None:
    """The primary enabled binding sources worktrees regardless of gates."""
    binding = _binding("h1", "/binding")
    entries = [_entry("h1", "/entry"), _entry("h2", "/two")]
    assert checkout_on_host([binding], entries, "h1") == "/binding"
    assert checkout_on_host([], entries, "h1") == "/entry"
    assert checkout_on_host([], entries, "h3") is None
    # Disabled and non-primary bindings never source.
    assert checkout_on_host([replace(binding, enabled=False)], entries, "h1") == "/entry"
    assert checkout_on_host([replace(binding, is_primary=False)], entries, "h1") == "/entry"
    assert checkout_on_host([], [], "h1") is None


# ── New-branch base ─────────────────────────────────────────────────────


def _repository(role: str = "related", default_branch: str = "main") -> ProjectRepository:
    return ProjectRepository(
        "r1",
        "p1",
        "repo",
        "https://example.com/org/repo.git",
        default_branch,
        ".agents/project/manifest.json",
        1,
        1,
        role=role,
    )


def test_resolve_new_branch_base_order() -> None:
    """Explicit wins, else the code repository, else nothing."""
    project = _project()
    code = _repository(role="code", default_branch="release")
    related = _repository(role="related", default_branch="coordination")

    assert resolve_new_branch_base(project, [code, related], "given/base") == "given/base"
    assert resolve_new_branch_base(project, [related, code], None) == "release"
    assert resolve_new_branch_base(project, [related], None) is None
    assert resolve_new_branch_base(None, [code], None) is None
    assert resolve_new_branch_base(None, [code], "given/base") == "given/base"
    assert resolve_new_branch_base(project, [replace(code, default_branch="")], None) is None


# ── Launch directory and recorded worktree ──────────────────────────────


@pytest.mark.parametrize(
    ("target", "git_used", "worktree_root", "expected"),
    [
        # A created worktree inside the entry: the session launches in it.
        ("/entry/.worktrees/repo/x", True, None, ("/entry/.worktrees/repo/x",) * 2),
        # A bound worktree outside any entry.
        ("/outside/x", True, None, ("/outside/x", "/outside/x")),
        # A launch relocated into a subdirectory records the worktree root.
        (
            "/entry/.worktrees/repo/x/web",
            True,
            "/entry/.worktrees/repo/x",
            ("/entry/.worktrees/repo/x/web", "/entry/.worktrees/repo/x"),
        ),
        # No git: the entry itself, or a directory inside it, records nothing.
        ("/entry", False, None, ("/entry", None)),
        ("/entry/sub", False, None, ("/entry/sub", None)),
        (None, False, None, (None, None)),
    ],
)
def test_place_project_session_launches_at_the_target(
    target: str | None,
    git_used: bool,
    worktree_root: str | None,
    expected: tuple[str | None, str | None],
) -> None:
    assert (
        _place_project_session(target=target, git_used=git_used, worktree_root=worktree_root)
        == expected
    )


def test_place_project_session_child_takes_the_parent_worktree() -> None:
    parent = SimpleNamespace(worktree="/entry/.worktrees/repo/x")
    assert _place_project_session(
        target="/entry",
        git_used=False,
        parent=parent,  # type: ignore[arg-type]
    ) == ("/entry", "/entry/.worktrees/repo/x")
