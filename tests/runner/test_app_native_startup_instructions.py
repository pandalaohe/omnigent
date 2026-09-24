"""Tests for the shared native startup-instruction resolution.

``_native_startup_instructions_from_spec`` is the seam that turns a session's
``AgentSpec`` into the value passed to the managed-host launch paths'
startup-additive channels: claude-native's ``--append-system-prompt``
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
from omnigent.runtime.prompt import EMBEDDED_BROWSER_PRIORITY_INSTRUCTION
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
            f"Be a concise assistant.\n\n{EMBEDDED_BROWSER_PRIORITY_INSTRUCTION}",
        ),
        (None, EMBEDDED_BROWSER_PRIORITY_INSTRUCTION),
        ("   \n  ", EMBEDDED_BROWSER_PRIORITY_INSTRUCTION),
    ],
    ids=["present", "absent", "whitespace-only"],
)
def test_native_startup_instructions_from_spec(instructions: str | None, expected: str) -> None:
    """Author text leads, then the framework text every session carries."""
    assert _native_startup_instructions_from_spec(_spec(instructions)) == expected


def test_native_startup_instructions_from_spec_none_spec() -> None:
    """A missing spec yields no instructions (neither channel is injected)."""
    assert _native_startup_instructions_from_spec(None) is None


def test_native_startup_instructions_from_spec_resolved_wrapper() -> None:
    """A ResolvedSpec wrapper unwraps to the same composed text."""
    wrapped = ResolvedSpec(spec=_spec("Be a concise assistant."), workdir=Path("/tmp"))
    assert _native_startup_instructions_from_spec(wrapped) == (
        f"Be a concise assistant.\n\n{EMBEDDED_BROWSER_PRIORITY_INSTRUCTION}"
    )


def test_native_startup_instructions_from_spec_never_returns_stripped_form() -> None:
    """The author text is preserved verbatim, not the stripped form."""
    padded = "  Keep leading/trailing whitespace exactly.  "
    assert _native_startup_instructions_from_spec(_spec(padded)) == (
        f"{padded}\n\n{EMBEDDED_BROWSER_PRIORITY_INSTRUCTION}"
    )
