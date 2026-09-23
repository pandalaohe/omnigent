"""Project roots and default-host choices."""

from __future__ import annotations

from dataclasses import replace

import pytest

from omnigent.entities import Project, ProjectHostBinding
from omnigent.server.feature_flags import Feature, FeatureFlags
from omnigent.server.project_placement import (
    bindings_apply,
    default_host,
    host_roots,
    load_bindings,
    load_eligible_host_ids,
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
    assert await load_eligible_host_ids(None, "alice", ["h1"]) is None
    assert await load_eligible_host_ids(Hosts(), "alice", ["h1", "h2", "h9"]) == frozenset({"h1"})  # type: ignore[arg-type]
    assert await load_eligible_host_ids(Hosts(), None, ["h1", "h2", "h9"]) == frozenset(
        {"h1", "h2"}
    )  # type: ignore[arg-type]
