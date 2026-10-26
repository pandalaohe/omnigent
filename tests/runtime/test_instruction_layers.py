"""Route selection and role composition without server or harness processes."""

import pytest

from omnigent.harness_plugins import accepted_harnesses, native_harnesses
from omnigent.runtime.instruction_layers import (
    agent_key_for_harness,
    compose_role,
    compose_route,
    compose_session_text,
    instruction_agents,
    select_role_layers,
)
from omnigent.stores.global_instructions_store import GlobalInstructionRevision


@pytest.mark.parametrize("role", ["mother", "child"])
@pytest.mark.parametrize("independent", ["", " \n\t", "  Independent\n"])
@pytest.mark.parametrize(
    "host_id,agent_key", [(None, None), ("h1", None), (None, "codex"), ("h1", "codex")]
)
def test_role_selection_preserves_revisions_order_and_bytes(role, independent, host_id, agent_key):
    def revision(text, agent="", host="", scope=role):
        return GlobalInstructionRevision(text, text, 1, None, scope, agent, host)

    shared = revision("  Shared\n")
    agent = revision("\tAgent  ", "codex")
    host = revision(" Host \n", host="h1")
    cell = revision(independent, "codex", "h1")
    revisions = [
        cell,
        host,
        revision("Other agent", "claude"),
        agent,
        revision("Other host", host="h2"),
        shared,
        revision("Wrong role", scope="child" if role == "mother" else "mother"),
        revision("Route independent", "codex", "h1", "independent"),
    ]
    expected = [shared]
    if agent_key:
        expected.append(agent)
    if host_id:
        expected.append(host)
    if host_id and agent_key and independent.strip():
        expected = [cell]
    selected = select_role_layers(revisions, role, host_id, agent_key)
    assert selected == expected
    assert all(actual is original for actual, original in zip(selected, expected, strict=True))
    assert compose_role(selected) == "\n\n".join(r.text for r in expected)


@pytest.mark.parametrize("role", ["mother", "child"])
def test_role_selection_skips_blank_sources(role):
    rows = [
        GlobalInstructionRevision("s", " \n", 1, None, role),
        GlobalInstructionRevision("a", "", 1, None, role, "codex"),
    ]
    assert select_role_layers(rows, role, "h1", "codex") == []
    assert compose_role([]) == ""


@pytest.mark.parametrize("independent", ["", "I"])
@pytest.mark.parametrize(
    ("shared", "agent", "host", "stack"),
    [
        ("", "", "", ""),
        ("", "", "H", "H"),
        ("", "A", "", "A"),
        ("", "A", "H", "A\n\nH"),
        ("S", "", "", "S"),
        ("S", "", "H", "S\n\nH"),
        ("S", "A", "", "S\n\nA"),
        ("S", "A", "H", "S\n\nA\n\nH"),
    ],
)
def test_every_layer_combination(shared, agent, host, stack, independent):
    layers = [
        GlobalInstructionRevision(scope, text, 1, None, scope=scope)
        for scope, text in [
            ("host", host),
            ("shared", shared),
            ("independent", independent),
            ("agent", agent),
        ]
    ]
    result = compose_route(layers)
    assert result.text == ("I" if independent else stack)
    assert all(revision.text for revision in result.revisions)


def test_blank_layers_skip_without_changing_nonblank_text_or_truncating():
    text = "  shared\n" + "x" * 10_001
    result = compose_route(
        [
            GlobalInstructionRevision("s", text, 1, None),
            GlobalInstructionRevision("i", " \n\t", 1, None, scope="independent"),
        ]
    )
    assert result.text == text
    assert [revision.id for revision in result.revisions] == ["s"]


@pytest.mark.parametrize(
    ("has_parent", "spawn", "expected"),
    [
        (False, False, "I"),
        (False, True, "I\n\nMother\n\nQuota"),
        (True, False, "I\n\nChild"),
        (True, True, "I\n\nChild"),
    ],
)
def test_role_blocks_are_exclusive_and_survive_independent(has_parent, spawn, expected):
    assert (
        compose_session_text(
            "I",
            has_parent=has_parent,
            spawn=spawn,
            mother_text="Mother",
            child_text="Child",
            quota_line="Quota",
        )
        == expected
    )


def test_quota_requires_nonblank_mother_block_and_empty_roles_add_no_separators():
    assert (
        compose_session_text(
            "S", has_parent=False, spawn=True, mother_text=" \n", quota_line="Quota"
        )
        == "S"
    )
    assert compose_session_text("", has_parent=True, spawn=True, child_text="Child") == "Child"
    assert compose_session_text("", has_parent=False, spawn=False) == ""


def test_every_declared_harness_maps_to_its_product_or_no_product():
    groups = {
        "claude": "claude claude-sdk claude-native native-claude claude_sdk",
        "codex": "codex codex-native native-codex",
        "pi": "pi pi-native native-pi",
        "opencode": "opencode opencode-native native-opencode",
        "cursor": "cursor cursor-native native-cursor",
        "kiro": "kiro-native native-kiro",
        "goose": "goose goose-native native-goose",
        "devin": "devin devin-acp devin-native native-devin",
        "antigravity": (
            "antigravity agy google-antigravity agy-native native-agy "
            "native-antigravity antigravity-native"
        ),
        "qwen": "qwen qwen-code qwen-native native-qwen",
        "kimi": "kimi kimi-code kimi-native native-kimi",
        "hermes": "hermes hermes-native native-hermes",
    }
    expected = {name: key for key, names in groups.items() for name in names.split()}
    for harness in accepted_harnesses() | native_harnesses() | expected.keys():
        if harness in expected:
            assert agent_key_for_harness(harness) == expected[harness], harness
    for agent in instruction_agents():
        for harness in agent.harnesses:
            assert agent_key_for_harness(harness) == agent.key
    assert agent_key_for_harness(None) is None
    assert agent_key_for_harness("unknown-native") is None
    assert agent_key_for_harness("openai-agents-native") is None
    assert agent_key_for_harness("acp:codex") is None


def test_configured_acp_and_deliverable_harnesses_share_one_descriptor_set(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        "omnigent.onboarding.acp_auth.acp_agents",
        lambda: [SimpleNamespace(slug="neutral_agent", name="Neutral agent")],
    )
    descriptors = {agent.key: agent for agent in instruction_agents()}
    assert descriptors["acp:neutral_agent"].harnesses == ("acp:neutral_agent",)
    assert descriptors["acp:neutral_agent"].subject == ("acp:neutral_agent", "acp:neutral_agent")
    assert agent_key_for_harness("acp:neutral_agent") == "acp:neutral_agent"
    assert agent_key_for_harness("openai-agents") == "openai-agents"
    assert all(len(key) <= 64 for key in descriptors)
    assert "none" not in descriptors


def test_instruction_agents_excludes_undelivered_and_unknown_harnesses(monkeypatch):
    from types import SimpleNamespace

    from omnigent.harness_capabilities import InstructionDelivery
    from omnigent.runtime import instruction_layers as module

    original = module.capability_for_harness
    monkeypatch.setattr(module, "valid_harnesses", lambda: {"no-delivery", "uncertain", "x" * 65})
    monkeypatch.setattr("omnigent.onboarding.acp_auth.acp_agents", list)
    monkeypatch.setattr(
        module,
        "capability_for_harness",
        lambda h: (
            SimpleNamespace(
                instruction_delivery=InstructionDelivery.NOT_DELIVERED
                if h == "no-delivery"
                else InstructionDelivery.UNKNOWN
            )
            if h in {"no-delivery", "uncertain"}
            else original(h)
        ),
    )
    keys = {agent.key for agent in instruction_agents()}
    assert not keys.intersection({"no-delivery", "uncertain", "x" * 65})


def test_invalid_acp_config_keeps_existing_native_layers(monkeypatch):
    def invalid():
        raise ValueError("invalid ACP option")

    monkeypatch.setattr("omnigent.onboarding.acp_auth.acp_agents", invalid)
    assert agent_key_for_harness("codex-native") == "codex"
