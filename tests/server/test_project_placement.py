"""Project roots and default-host choices."""

from __future__ import annotations

from dataclasses import replace

import pytest

from omnigent.entities import Project, ProjectHostBinding, ProjectHostEntry
from omnigent.server.feature_flags import Feature, FeatureFlags
from omnigent.server.project_placement import (
    HostRoot,
    bindings_apply,
    checkout_on_host,
    default_host,
    host_roots,
    load_bindings,
    load_eligible_host_ids,
    load_entries,
    place_session,
    root_on_host,
)


def _project(*, host_id: str | None = "h1", workspace: str = "/config") -> Project:
    config = {"workspace": workspace}
    if host_id is not None:
        config["host_id"] = host_id
    return Project("p1", "Project", "alice", 1, config=config, collaboration_enabled=True)


def _binding(host_id: str, workspace: str = "/binding") -> ProjectHostBinding:
    return ProjectHostBinding(
        "b1", "p1", host_id, "primary", "repo", workspace, 1, 1, is_primary=True
    )


def _entry(host_id: str, workspace: str = "/entry") -> ProjectHostEntry:
    return ProjectHostEntry("p1", host_id, workspace, 1)


def test_binding_precedes_config_only_with_both_gates() -> None:
    project = _project()
    binding = _binding("h1")
    on = FeatureFlags(frozenset({Feature.PROJECT_ASSIGNMENTS}))
    assert bindings_apply(project, on)
    assert root_on_host(project, [binding], "h1", gates_on=True).workspace == "/binding"
    assert root_on_host(project, [binding], "h1", gates_on=False).workspace == "/config"
    assert not bindings_apply(replace(project, collaboration_enabled=False), on)
    assert not bindings_apply(project, FeatureFlags())
    assert (
        root_on_host(project, [replace(binding, enabled=False)], "h1", gates_on=True).workspace
        == "/config"
    )
    assert (
        root_on_host(project, [replace(binding, is_primary=False)], "h1", gates_on=True).workspace
        == "/config"
    )


def test_roots_and_default_host_obey_eligibility_and_sandbox_sentinel() -> None:
    project = _project(host_id=None)
    roots = host_roots(project, [_binding("h1"), replace(_binding("h2"), id="b2")], gates_on=True)
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
    assert host_roots(sandbox, [], gates_on=False) == []
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
    """An entry is the root regardless of gates; the binding stays the checkout."""
    project = _project(workspace="/config")
    binding = _binding("h1", "/binding")
    entries = [_entry("h1", "/entry")]
    root = root_on_host(project, [binding], "h1", gates_on=True, entries=entries)
    assert root == HostRoot("h1", "/entry", "entry", "/binding")
    # Entries are not gated: collaboration off changes nothing.
    assert root_on_host(project, [binding], "h1", gates_on=False, entries=entries) == root
    roots = host_roots(project, [binding], gates_on=True, entries=entries)
    assert roots == [root]


def test_entry_hosts_only_and_no_fallback_after_removal() -> None:
    """With entries, a host without one has no root — no binding/config fallback."""
    project = _project(workspace="/config")
    binding = _binding("h1", "/binding")
    entries = [_entry("h2", "/other")]
    assert root_on_host(project, [binding], "h1", gates_on=True, entries=entries) is None
    assert root_on_host(project, [binding], "h1", gates_on=False, entries=entries) is None
    assert host_roots(project, [binding], gates_on=True, entries=entries) == [
        HostRoot("h2", "/other", "entry", "/other")
    ]


def test_entries_keep_sandbox_and_legacy_paths_intact() -> None:
    """``__sandbox__`` has no root; a project without entries keeps binding → config."""
    project = _project(workspace="/config")
    binding = _binding("h1", "/binding")
    entries = [_entry("h1", "/entry")]
    assert root_on_host(project, [binding], "__sandbox__", gates_on=True, entries=entries) is None
    assert host_roots(project, [binding], gates_on=True, entries=entries) == [
        HostRoot("h1", "/entry", "entry", "/binding")
    ]
    legacy = root_on_host(project, [binding], "h1", gates_on=True)
    assert (legacy.workspace, legacy.source, legacy.checkout) == (
        "/binding",
        "binding",
        "/binding",
    )
    config = root_on_host(project, [binding], "h1", gates_on=False)
    assert (config.workspace, config.source, config.checkout) == ("/config", "config", "/binding")
    assert root_on_host(project, [], "__sandbox__", gates_on=False) is None


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


# ── R-PLACE step 4–5 ────────────────────────────────────────────────────


def test_place_session_inside_entry_launches_at_entry() -> None:
    assert place_session(
        "/entry", "/entry/worktrees/x", git_used=True, entry_within_agent_boundary=True
    ) == ("/entry", "/entry/worktrees/x")


def test_place_session_equal_and_prefix_trap_stay_at_target() -> None:
    assert place_session("/x/a", "/x/a", git_used=True, entry_within_agent_boundary=True) == (
        "/x/a",
        "/x/a",
    )
    assert place_session("/x/a", "/x/a/", git_used=False, entry_within_agent_boundary=True) == (
        "/x/a/",
        None,
    )
    assert place_session("/x/a", "/x/ab/w", git_used=True, entry_within_agent_boundary=True) == (
        "/x/ab/w",
        "/x/ab/w",
    )


def test_place_session_windows_case_and_separators() -> None:
    target = "d:\\p\\omnigent\\fork\\omnigent-worktrees\\x"
    assert place_session(
        "D:\\P\\omnigent", target, git_used=True, entry_within_agent_boundary=True
    ) == ("D:\\P\\omnigent", target)
    # Same directory, different case and trailing separator: not "inside".
    assert place_session("D:\\P", "d:\\p\\", git_used=True, entry_within_agent_boundary=True) == (
        "d:\\p\\",
        "d:\\p\\",
    )


def test_place_session_boundary_and_non_git_outcomes() -> None:
    assert place_session(
        "/entry", "/entry/worktrees/x", git_used=True, entry_within_agent_boundary=False
    ) == ("/entry/worktrees/x", "/entry/worktrees/x")
    assert place_session(
        "/entry", "/outside/x", git_used=False, entry_within_agent_boundary=True
    ) == ("/outside/x", None)
    assert place_session(None, "/outside/x", git_used=True, entry_within_agent_boundary=True) == (
        "/outside/x",
        "/outside/x",
    )
