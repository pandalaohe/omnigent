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
    assert result.sources == {
        "agent": "explicit",
        "model": "explicit",
        "effort": "project_host",
        "speed": "none",
        "permission": "none",
    }


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


@pytest.mark.parametrize("speed", ["standard", None])
def test_explicit_speed_wins_over_project_default(speed: str | None) -> None:
    result = _resolve(
        explicit={"agent_id": "codex-sdk", "speed": speed},
        explicit_fields={"agent_id", "speed"},
        project_config={"calling_defaults": {"HDS": {"harnesses": {"codex": {"speed": "fast"}}}}},
    )
    assert result.speed == speed
    assert result.sources["speed"] == "explicit"


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
    assert result.sources == {
        "agent": "explicit",
        "model": "none",
        "effort": "none",
        "speed": "none",
        "permission": "none",
    }


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


def test_check_offered_speed_uses_selected_or_default_model_tiers() -> None:
    rows = [
        {
            "id": "gpt-6-sol",
            "isDefault": True,
            "serviceTiers": [{"id": "priority", "name": "Fast"}],
        },
        {"id": "gpt-6-luna", "serviceTiers": [{"id": "ultrafast", "name": "Ultrafast"}]},
    ]
    base = {
        "harness": "codex",
        "effort": None,
        "catalog": _catalog(rows),
        "sources": {"model": "none", "effort": "none", "speed": "master"},
        "host_id": "HDS",
    }
    assert check_offered(model=None, speed="fast", **base) == []
    assert [p["field"] for p in check_offered(model=None, speed="ultrafast", **base)] == ["speed"]
    assert check_offered(model="gpt-6-luna", speed="ultrafast", **base) == []


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


@pytest.mark.parametrize("field,value", [("speed", "fast"), ("permission", "approve-for-me")])
@pytest.mark.parametrize(
    "layer", ["project_host", "master", "project_host_native", "master_native"]
)
def test_session_modes_follow_harness_chain(field: str, value: str, layer: str) -> None:
    project_harnesses = {"codex": {field: "invalid!"}, "codex-native": {field: "invalid!"}}
    master_harnesses = {"codex": {field: "invalid!"}, "codex-native": {field: "invalid!"}}
    harness = "codex-native" if layer.endswith("native") else "codex"
    target = project_harnesses if layer.startswith("project") else master_harnesses
    target[harness][field] = value
    result = _resolve(
        explicit={"agent_id": "codex-sdk"},
        explicit_fields={"agent_id"},
        project_config={"calling_defaults": {"HDS": {"harnesses": project_harnesses}}},
        master={"HDS": master_harnesses},
    )
    assert getattr(result, field) == value
    assert result.sources[field] == layer


def test_session_modes_resolve_independently_and_filter_harness_vocabulary() -> None:
    result = _resolve(
        explicit={"agent_id": "codex-sdk"},
        explicit_fields={"agent_id"},
        project_config={
            "calling_defaults": {"HDS": {"harnesses": {"codex": {"speed": "standard"}}}}
        },
        master={"HDS": {"codex": {"speed": "fast", "permission": "read-only"}}},
    )
    assert (result.speed, result.permission) == ("standard", "read-only")
    result = _resolve(
        explicit={"agent_id": "claude-agent"},
        explicit_fields={"agent_id"},
        master={
            "HDS": {
                "claude-sdk": {"speed": "fast", "permission": "read-only"},
                "claude-native": {"permission": "plan"},
            }
        },
    )
    assert result.speed == "fast"
    assert result.sources["speed"] == "master"
    assert result.permission == "plan"
    assert result.sources["permission"] == "master_native"


def test_session_modes_use_the_canonical_override_harness() -> None:
    """An alias override finds the entry stored under the canonical harness."""
    result = _resolve(
        explicit={"agent_id": "codex-sdk", "harness_override": "claude"},
        explicit_fields={"agent_id", "harness_override"},
        master={"HDS": {"claude-sdk": {"permission": "plan", "model": "sonnet"}}},
    )
    assert result.harness == "claude"
    assert result.permission == "plan"
    assert result.sources["permission"] == "master"
    # Model / effort resolution still keys on the raw override.
    assert result.model is None


@pytest.mark.parametrize("override", [None, ""])
def test_session_modes_fall_back_to_the_agent_harness_for_a_null_override(
    override: str | None,
) -> None:
    """An explicit null / blank override launches the agent's own harness."""
    result = _resolve(
        explicit={"agent_id": "codex-sdk", "harness_override": override},
        explicit_fields={"agent_id", "harness_override"},
        master={"HDS": {"codex": {"speed": "fast", "permission": "read-only", "model": "gpt"}}},
    )
    assert (result.speed, result.permission) == ("fast", "read-only")
    assert (result.sources["speed"], result.sources["permission"]) == ("master", "master")
    # The pre-existing model lookup is untouched: no harness, no model default.
    assert result.harness is None
    assert result.model is None
    # No agent and no override leaves the modes unresolved.
    result = _resolve(
        explicit={"harness_override": override},
        explicit_fields={"harness_override"},
        master={"HDS": {"codex": {"speed": "fast"}}},
    )
    assert result.speed is None


@pytest.mark.parametrize(
    "harness,field,value",
    [
        ("codex", "speed", "fast"),
        ("codex", "speed", "ultrafast"),
        ("codex-native", "speed", "priority"),
        ("codex-native", "speed", "standard"),
        ("claude-native", "speed", "fast"),
        ("claude-sdk", "speed", "standard"),
        *[
            (harness, "permission", value)
            for harness, values in {
                "codex": ["ask-for-approval", "approve-for-me", "full-access", "read-only"],
                "codex-native": ["ask-for-approval", "approve-for-me", "full-access", "read-only"],
                "claude-sdk": ["acceptEdits", "auto", "plan", "dontAsk", "bypassPermissions"],
                "claude-native": ["acceptEdits", "auto", "plan", "dontAsk", "bypassPermissions"],
            }.items()
            for value in values
        ],
    ],
)
def test_validate_session_default_modes(harness: str, field: str, value: str) -> None:
    defaults = {"HDS": {"harnesses": {harness: {field: value}}}}
    assert validate_project_calling_defaults(defaults) == defaults


@pytest.mark.parametrize(
    "harness,field,value",
    [
        ("codex", "speed", "priority;bad"),
        ("claude-native", "speed", "priority"),
        ("pi-native", "permission", "plan"),
        ("codex", "permission", "plan"),
        ("claude-sdk", "permission", "read-only"),
        ("codex", "speed", None),
        ("codex", "permission", ["read-only"]),
    ],
)
def test_validate_session_default_modes_rejects_wrong_vocabulary(
    harness: str, field: str, value: object
) -> None:
    with pytest.raises(OmnigentError, match=field):
        validate_project_calling_defaults({"HDS": {"harnesses": {harness: {field: value}}}})
