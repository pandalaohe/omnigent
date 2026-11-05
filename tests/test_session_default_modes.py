"""The catalog's default and create contexts are distinct wire contracts."""

from __future__ import annotations

import pytest

from omnigent.harness_capabilities import ModeOption
from omnigent.session_default_modes import apply_mode, mode_for_concept, mode_values, valid_mode


@pytest.mark.parametrize(
    "harness,expected",
    [
        ("claude-native", ("default", "acceptEdits", "auto", "plan")),
        ("claude-sdk", ("default", "acceptEdits", "auto", "plan")),
        ("codex-native", ("ask-for-approval", None, "approve-for-me", "read-only")),
        ("codex", ("ask-for-approval", None, "approve-for-me", "read-only")),
        ("pi", ("ask", "accept-edits", "auto", "plan")),
        ("pi-native", ("ask", "accept-edits", "auto", "plan")),
        ("antigravity-native", ("ask", "accept-edits", "auto", "plan")),
    ],
)
def test_permission_concepts(harness: str, expected: tuple) -> None:
    assert (
        tuple(
            mode_for_concept(harness, concept)
            for concept in ("manual", "edit-automatic", "automatic", "read-only")
        )
        == expected
    )


def test_acp_concepts_only_use_declared_meanings_and_option_order() -> None:
    options = [
        {"value": "default", "concept": "manual"},
        {"value": "other", "concept": "manual"},
        {"value": "auto"},
    ]
    assert mode_for_concept("acp:example", "manual", options) == "default"
    assert mode_for_concept("acp:example", "automatic", options) is None
    assert mode_for_concept("acp:example", "read-only", options) is None
    assert mode_for_concept("codex", "manual", []) == "ask-for-approval"


def test_builtin_permission_contexts_and_application() -> None:
    for harness in ("pi", "pi-native", "antigravity-native"):
        assert mode_values(harness, "permission", "default") == {
            "ask",
            "plan",
            "accept-edits",
            "auto",
            "full",
        }
        assert mode_values(harness, "permission", "create") == {
            "ask",
            "plan",
            "accept-edits",
            "auto",
            "full",
        }
        assert valid_mode("full", harness, "permission", "default")
        assert apply_mode(harness, "permission", "full", context="default").tier == "full"
    for harness in ("claude-native", "claude-sdk"):
        assert "default" in mode_values(harness, "permission", "default")
        assert "default" in mode_values(harness, "permission", "create")
    for harness in ("codex", "codex-native"):
        assert mode_values(harness, "permission", "default") == {
            "ask-for-approval",
            "approve-for-me",
            "full-access",
            "read-only",
        }

    assert apply_mode("claude-native", "permission", "acceptEdits").args == (
        "--permission-mode",
        "acceptEdits",
    )
    assert apply_mode("claude-sdk", "permission", "acceptEdits").labels == {
        "omnigent.claude_sdk.permission_mode": "acceptEdits"
    }
    assert apply_mode("codex", "permission", "read-only").labels == {
        "omnigent.codex_sdk.approval_mode": "read-only"
    }


def test_invalid_context_and_create_only_default_are_rejected() -> None:
    with pytest.raises(ValueError, match="contexts"):
        ModeOption("foo", "Foo", "manual", frozenset({"later"}))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="context"):
        mode_values("pi", "permission", "later")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unsupported"):
        apply_mode("pi", "permission", "unknown", context="default")


def test_legacy_codex_sdk_default_still_requires_approval() -> None:
    assert valid_mode("default", "codex", "permission", "create")
    assert apply_mode("codex", "permission", "default").labels == {
        "omnigent.codex_sdk.approval_mode": "ask-for-approval"
    }
    assert not valid_mode("default", "codex-native", "permission", "create")
