"""Route selection and role composition without server or harness processes."""

import pytest

from omnigent.harness_plugins import accepted_harnesses, native_harnesses
from omnigent.runtime.instruction_layers import (
    agent_key_for_harness,
    compose_route,
    compose_session_text,
)
from omnigent.stores.global_instructions_store import GlobalInstructionRevision


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
        assert agent_key_for_harness(harness) == expected.get(harness), harness
    assert agent_key_for_harness(None) is None
    assert agent_key_for_harness("unknown-native") is None
    assert agent_key_for_harness("acp:codex") is None
