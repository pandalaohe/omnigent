"""Unit tests for the calling-defaults resolver, checks, and validation."""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.calling_defaults import (
    check_offered,
    load_master,
    resolve_calling,
    validate_project_calling_defaults,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.user_preferences_store import SqlAlchemyUserPreferencesStore


def _agent_harness(agent_id: str) -> str | None:
    """Test agent catalog: two SDK agents and one native legacy agent."""
    return {
        "codex-sdk": "codex",
        "claude-agent": "claude-sdk",
        "legacy-agent": "claude-native",
    }.get(agent_id)


def _resolve(
    *,
    explicit: dict[str, Any] | None = None,
    explicit_fields: set[str] | None = None,
    project_config: dict | None = None,
    master: dict | None = None,
    host_id: str = "HDS",
):
    return resolve_calling(
        explicit=explicit if explicit is not None else {},
        explicit_fields=explicit_fields if explicit_fields is not None else set(),
        project_config=project_config,
        master=master if master is not None else {},
        host_id=host_id,
        agent_harness=_agent_harness,
    )


def _catalog(
    models: list[dict[str, Any]],
    *,
    error: str | None = None,
    fetched_at: int | None = 1700000000,
) -> dict[str, Any]:
    return {"models": models, "error": error, "fetched_at": fetched_at}


def test_explicit_values_win_per_field() -> None:
    """Scenario 3: an explicit model keeps the project's effort."""
    project = {
        "calling_defaults": {
            "HDS": {"harnesses": {"codex": {"model": "gpt-6-sol", "effort": "high"}}}
        }
    }
    result = _resolve(
        explicit={"agent_id": "codex-sdk", "model_override": "gpt-6-luna"},
        explicit_fields={"agent_id", "model_override"},
        project_config=project,
    )
    assert result.agent_id == "codex-sdk"
    assert result.harness == "codex"
    assert result.model == "gpt-6-luna"
    assert result.effort == "high"
    assert result.sources == {"agent": "explicit", "model": "explicit", "effort": "project_host"}


def test_explicit_none_is_kept() -> None:
    """Scenario 4: an explicit null effort stays unset despite a project default."""
    project = {
        "calling_defaults": {
            "HDS": {"harnesses": {"codex": {"model": "gpt-6-sol", "effort": "high"}}}
        }
    }
    result = _resolve(
        explicit={"agent_id": "codex-sdk", "reasoning_effort": None},
        explicit_fields={"agent_id", "reasoning_effort"},
        project_config=project,
    )
    assert result.effort is None
    assert result.sources["effort"] == "explicit"
    assert result.model == "gpt-6-sol"


def test_sdk_harness_inherits_master_native() -> None:
    """Scenario 5: an unset SDK harness falls back to its native sibling."""
    master = {"HDS": {"codex-native": {"model": "gpt-6-astra", "effort": "medium"}}}
    result = _resolve(
        explicit={"agent_id": "codex-sdk"},
        explicit_fields={"agent_id"},
        master=master,
    )
    assert result.harness == "codex"
    assert result.model == "gpt-6-astra"
    assert result.effort == "medium"
    assert result.sources["model"] == "master_native"
    assert result.sources["effort"] == "master_native"


def test_master_sdk_entry_beats_project_native_fallback() -> None:
    """Scenario 6: the pinned order checks the master SDK row before the native."""
    project = {
        "calling_defaults": {"HDS": {"harnesses": {"codex-native": {"model": "gpt-6-sol"}}}}
    }
    master = {"HDS": {"codex": {"model": "gpt-5.5"}}}
    result = _resolve(
        explicit={"agent_id": "codex-sdk"},
        explicit_fields={"agent_id"},
        project_config=project,
        master=master,
    )
    assert result.model == "gpt-5.5"
    assert result.sources["model"] == "master"


def test_project_native_entry_beats_master_native() -> None:
    """The project native fallback sits above the master native fallback."""
    project = {
        "calling_defaults": {"HDS": {"harnesses": {"codex-native": {"model": "project-native"}}}}
    }
    master = {"HDS": {"codex-native": {"model": "master-native"}}}
    result = _resolve(
        explicit={"agent_id": "codex-sdk"},
        explicit_fields={"agent_id"},
        project_config=project,
        master=master,
    )
    assert result.model == "project-native"
    assert result.sources["model"] == "project_host_native"


def test_no_defaults_anywhere() -> None:
    """Scenario 7: with no entries the explicit agent survives and fields stay unset."""
    result = _resolve(
        explicit={"agent_id": "codex-sdk"},
        explicit_fields={"agent_id"},
        project_config={},
    )
    assert result.agent_id == "codex-sdk"
    assert result.model is None
    assert result.effort is None
    assert result.sources == {"agent": "explicit", "model": "none", "effort": "none"}


def test_projectless_uses_master_layers_only() -> None:
    """D29: no project keeps the master entry and its native fallback."""
    master = {"HDS": {"codex-native": {"model": "gpt-6-astra"}}}
    result = _resolve(
        explicit={"agent_id": "codex-sdk"},
        explicit_fields={"agent_id"},
        project_config=None,
        master=master,
    )
    assert result.model == "gpt-6-astra"
    assert result.sources["model"] == "master_native"


def test_fields_resolve_independently() -> None:
    """A project effort and a master model resolve in one pass."""
    project = {"calling_defaults": {"HDS": {"harnesses": {"codex": {"effort": "high"}}}}}
    master = {"HDS": {"codex": {"model": "gpt-5.5"}}}
    result = _resolve(
        explicit={"agent_id": "codex-sdk"},
        explicit_fields={"agent_id"},
        project_config=project,
        master=master,
    )
    assert result.model == "gpt-5.5"
    assert result.sources["model"] == "master"
    assert result.effort == "high"
    assert result.sources["effort"] == "project_host"


def test_other_host_entries_do_not_apply() -> None:
    """Project and master entries are keyed by host id."""
    project = {"calling_defaults": {"TMB": {"harnesses": {"codex": {"model": "gpt-6-sol"}}}}}
    master = {"TMB": {"codex": {"model": "gpt-5.5"}}}
    result = _resolve(
        explicit={"agent_id": "codex-sdk"},
        explicit_fields={"agent_id"},
        project_config=project,
        master=master,
        host_id="HDS",
    )
    assert result.model is None
    assert result.sources["model"] == "none"


def test_agent_resolution_prefers_project_host_then_legacy() -> None:
    """The agent chain is per-host default, legacy config, then none."""
    project = {
        "agent_id": "legacy-agent",
        "calling_defaults": {"HDS": {"agent_id": "codex-sdk"}},
    }
    per_host = _resolve(project_config=project)
    assert per_host.agent_id == "codex-sdk"
    assert per_host.sources["agent"] == "project_host"
    assert per_host.harness == "codex"

    legacy = _resolve(project_config={"agent_id": "legacy-agent"})
    assert legacy.agent_id == "legacy-agent"
    assert legacy.sources["agent"] == "project_legacy"
    assert legacy.harness == "claude-native"

    absent = _resolve(project_config={})
    assert absent.agent_id is None
    assert absent.harness is None
    assert absent.sources["agent"] == "none"


def test_legacy_model_applies_only_to_the_legacy_agent() -> None:
    """A legacy project model is not inherited by a different effective agent."""
    project = {"agent_id": "legacy-agent", "model": "legacy-model"}
    same = _resolve(
        explicit={"agent_id": "legacy-agent"},
        explicit_fields={"agent_id"},
        project_config=project,
    )
    assert same.model == "legacy-model"
    assert same.sources["model"] == "project_legacy"

    other = _resolve(
        explicit={"agent_id": "codex-sdk"},
        explicit_fields={"agent_id"},
        project_config=project,
    )
    assert other.model is None
    assert other.sources["model"] == "none"


def test_blank_stored_values_fall_through() -> None:
    """Blank project / master values count as absent."""
    project = {"calling_defaults": {"HDS": {"harnesses": {"codex": {"model": "   "}}}}}
    master = {"HDS": {"codex": {"model": "gpt-5.5"}}}
    result = _resolve(
        explicit={"agent_id": "codex-sdk"},
        explicit_fields={"agent_id"},
        project_config=project,
        master=master,
    )
    assert result.model == "gpt-5.5"
    assert result.sources["model"] == "master"

    project = {"agent_id": "legacy-agent", "calling_defaults": {"HDS": {"agent_id": ""}}}
    result = _resolve(project_config=project)
    assert result.agent_id == "legacy-agent"
    assert result.sources["agent"] == "project_legacy"


def test_check_offered_flags_a_default_model_missing_from_the_catalog() -> None:
    """Scenario 11: a default model absent from a fresh catalog is refused."""
    problems = check_offered(
        harness="codex",
        model="gpt-6-sol",
        effort=None,
        catalog=_catalog([{"id": "gpt-5.5"}]),
        sources={"model": "project_host", "effort": "none"},
        host_id="HDS",
        project_name="P",
    )
    assert problems == [
        {
            "field": "model",
            "setting": "project_host",
            "message": (
                "Default model 'gpt-6-sol' from project 'P' host settings is not offered "
                "by host 'HDS' (codex, last sync 2023-11-14 22:13 UTC). "
                "Change the setting, pass model, or run Sync models (Settings › "
                "Calling defaults) if the host offers it now."
            ),
        }
    ]


def test_check_offered_names_the_master_table() -> None:
    """A master-sourced value names the master table, not a project."""
    (problem,) = check_offered(
        harness="codex",
        model="gpt-6-sol",
        effort=None,
        catalog=_catalog([{"id": "gpt-5.5"}]),
        sources={"model": "master", "effort": "none"},
        host_id="HDS",
    )
    assert problem["setting"] == "master"
    assert "from the master table is not offered by host 'HDS'" in problem["message"]

    (legacy,) = check_offered(
        harness="codex",
        model="legacy-model",
        effort=None,
        catalog=_catalog([{"id": "gpt-5.5"}]),
        sources={"model": "project_legacy", "effort": "none"},
        host_id="HDS",
        project_name="P",
    )
    assert legacy["setting"] == "project_legacy"
    assert "from project 'P' All hosts row is not offered by host 'HDS'" in legacy["message"]


def test_check_offered_flags_an_unoffered_default_effort() -> None:
    """Scenario 12: an effort the matching model row omits is refused."""
    problems = check_offered(
        harness="codex",
        model="gpt-6-sol",
        effort="high",
        catalog=_catalog([{"id": "gpt-6-sol", "supportedReasoningEfforts": ["low", "medium"]}]),
        sources={"model": "project_host", "effort": "project_host"},
        host_id="HDS",
        project_name="P",
    )
    assert len(problems) == 1
    assert problems[0]["field"] == "effort"
    assert problems[0]["setting"] == "project_host"
    assert "not offered" in problems[0]["message"]


def test_check_offered_skips_a_stale_or_empty_catalog() -> None:
    """Scenario 13: an error or an empty catalog blocks nothing."""
    stale = _catalog([{"id": "gpt-5.5"}], error="unsupported")
    sources = {"model": "project_host", "effort": "project_host"}
    assert (
        check_offered(
            harness="codex",
            model="gpt-6-sol",
            effort="high",
            catalog=stale,
            sources=sources,
            host_id="HDS",
        )
        == []
    )
    assert (
        check_offered(
            harness="codex",
            model="gpt-6-sol",
            effort="high",
            catalog=_catalog([]),
            sources=sources,
            host_id="HDS",
        )
        == []
    )
    assert (
        check_offered(
            harness="codex",
            model="gpt-6-sol",
            effort="high",
            catalog=None,
            sources=sources,
            host_id="HDS",
        )
        == []
    )


def test_check_offered_accepts_effort_option_objects_and_uses_the_ladder() -> None:
    """Advertised effort objects count; absent lists fall back to the harness ladder."""
    objects = [{"id": "gpt-6-sol", "supportedReasoningEfforts": [{"reasoningEffort": "high"}]}]
    assert (
        check_offered(
            harness="codex",
            model="gpt-6-sol",
            effort="high",
            catalog=_catalog(objects),
            sources={"model": "master", "effort": "master"},
            host_id="HDS",
        )
        == []
    )

    bare = [{"id": "gpt-6-sol"}]
    assert (
        check_offered(
            harness="codex-native",
            model="gpt-6-sol",
            effort="ultra",
            catalog=_catalog(bare),
            sources={"model": "master_native", "effort": "master_native"},
            host_id="HDS",
        )
        == []
    )
    problems = check_offered(
        harness="claude-sdk",
        model="claude-opus-4-8",
        effort="ultra",
        catalog=_catalog([{"id": "claude-opus-4-8"}]),
        sources={"model": "master", "effort": "master"},
        host_id="HDS",
    )
    assert [problem["field"] for problem in problems] == ["effort"]


def test_check_offered_ignores_explicit_sources() -> None:
    """Explicit values keep today's validation; the catalog does not judge them."""
    assert (
        check_offered(
            harness="codex",
            model="gpt-6-sol",
            effort="high",
            catalog=_catalog([{"id": "gpt-5.5"}]),
            sources={"model": "explicit", "effort": "explicit"},
            host_id="HDS",
        )
        == []
    )


def test_validate_project_calling_defaults_accepts_and_copies_the_shape() -> None:
    """A full valid shape round-trips unchanged."""
    value = {
        "HDS": {
            "agent_id": "codex-sdk",
            "harnesses": {"codex": {"model": "gpt-6-sol", "effort": "high"}},
        },
        "TMB": {},
    }
    assert validate_project_calling_defaults(value) == value


@pytest.mark.parametrize(
    "value",
    [
        [],
        {"HDS": []},
        {"HDS": {"agent_id": 5}},
        {"HDS": {"unknown": True}},
        {"HDS": {"harnesses": []}},
        {"HDS": {"harnesses": {"codex": {"model": 5}}}},
        {"HDS": {"harnesses": {"codex": {"model": "gpt 6"}}}},
        {"HDS": {"harnesses": {"codex": {"model": ""}}}},
        {"HDS": {"harnesses": {"codex": {"effort": "bogus"}}}},
        {"HDS": {"harnesses": {"codex": {"unknown": "x"}}}},
    ],
)
def test_validate_project_calling_defaults_rejects_bad_shapes(value: object) -> None:
    """Scenario 28: every malformed shape raises INVALID_INPUT."""
    with pytest.raises(OmnigentError) as excinfo:
        validate_project_calling_defaults(value)
    assert excinfo.value.code == ErrorCode.INVALID_INPUT


class _RaisingStore:
    """A preferences store whose read fails."""

    def get(self, user_id: str) -> None:
        raise RuntimeError("preferences unavailable")


@pytest.mark.asyncio
async def test_load_master_reads_the_namespace_and_defaults_on_gaps(db_uri: str) -> None:
    """load_master returns the namespace dict and {} on every gap."""
    store = SqlAlchemyUserPreferencesStore(db_uri)
    master = {"HDS": {"codex": {"model": "gpt-6-sol", "effort": "high"}}}
    store.patch_namespace("alice@example.com", "calling_defaults", master)
    assert await load_master("alice@example.com", store) == master

    store.patch_namespace("shape@example.com", "calling_defaults", "not-an-object")
    assert await load_master("shape@example.com", store) == {}

    # The single-user server stores its preferences under the reserved
    # "local" identity, so a None owner reads that master table.
    local = {"host_1": {"codex-native": {"model": "gpt-6-local"}}}
    store.patch_namespace(RESERVED_USER_LOCAL, "calling_defaults", local)
    assert await load_master(None, store) == local

    assert await load_master("nobody@example.com", store) == {}
    assert await load_master("alice@example.com", None) == {}
    assert await load_master("broken@example.com", _RaisingStore()) == {}
