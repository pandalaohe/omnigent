"""Tests for the shared native startup-instruction resolution.

``_native_startup_instructions_from_spec`` is the seam that turns a session's
``AgentSpec`` into the value passed to the managed-host launch paths'
startup-additive channels: claude-native's ``--append-system-prompt-file``
(``_auto_create_claude_terminal``) and codex-native's ``developer_instructions``
(``_auto_create_codex_terminal``). It composes the author's text with the
spec-level framework instructions that apply to every turn of the session —
never the fully framework-composed per-turn string, since a startup channel is
not tied to any one turn.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.runner.app import ResolvedSpec, _native_startup_instructions_from_spec
from omnigent.runner.native.orchestration import _opencode_session_instructions
from omnigent.runtime.prompt import (
    EMBEDDED_BROWSER_PRIORITY_INSTRUCTION,
    WORKSPACE_IMAGE_INSTRUCTION,
)
from omnigent.spec.types import AgentSpec, ExecutorSpec


def _spec(instructions: str | None) -> AgentSpec:
    """Build a minimal agent spec carrying *instructions*."""
    return AgentSpec(
        spec_version=1,
        name="claude_code",
        instructions=instructions,
        executor=ExecutorSpec(),
    )


@pytest.mark.parametrize(
    ("instructions", "expected"),
    [
        (
            "Be a concise assistant.",
            "Be a concise assistant.\n\n"
            f"{EMBEDDED_BROWSER_PRIORITY_INSTRUCTION}\n\n{WORKSPACE_IMAGE_INSTRUCTION}",
        ),
        (None, f"{EMBEDDED_BROWSER_PRIORITY_INSTRUCTION}\n\n{WORKSPACE_IMAGE_INSTRUCTION}"),
        ("   \n  ", f"{EMBEDDED_BROWSER_PRIORITY_INSTRUCTION}\n\n{WORKSPACE_IMAGE_INSTRUCTION}"),
    ],
    ids=["present", "absent", "whitespace-only"],
)
def test_native_startup_instructions_from_spec(instructions: str | None, expected: str) -> None:
    """Author text leads, then the framework text every session carries."""
    assert _native_startup_instructions_from_spec(_spec(instructions)) == expected


def test_native_startup_instructions_from_spec_none_spec() -> None:
    """A missing spec still receives stable image guidance."""
    assert _native_startup_instructions_from_spec(None) == WORKSPACE_IMAGE_INSTRUCTION


@pytest.mark.parametrize("has_spec", [False, True])
def test_opencode_startup_carries_image_guidance(has_spec: bool) -> None:
    """OpenCode's session snapshot carries guidance even without authored text."""
    result = _opencode_session_instructions(_spec(None) if has_spec else None)
    assert result is not None
    assert result.count(WORKSPACE_IMAGE_INSTRUCTION) == 1


def test_native_startup_instructions_from_spec_resolved_wrapper() -> None:
    """A ResolvedSpec wrapper unwraps to the same instructions text."""
    wrapped = ResolvedSpec(spec=_spec("Be a concise assistant."), workdir=Path("/tmp"))
    assert _native_startup_instructions_from_spec(wrapped) == (
        "Be a concise assistant.\n\n"
        f"{EMBEDDED_BROWSER_PRIORITY_INSTRUCTION}\n\n{WORKSPACE_IMAGE_INSTRUCTION}"
    )


def test_native_startup_instructions_from_spec_never_returns_stripped_form() -> None:
    """The author text is preserved verbatim, not the stripped form."""
    padded = "  Keep leading/trailing whitespace exactly.  "
    assert _native_startup_instructions_from_spec(_spec(padded)) == (
        f"{padded}\n\n{EMBEDDED_BROWSER_PRIORITY_INSTRUCTION}\n\n{WORKSPACE_IMAGE_INSTRUCTION}"
    )
