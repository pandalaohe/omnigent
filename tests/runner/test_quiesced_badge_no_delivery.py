"""The quiescence badge must not feed sub-agent terminal delivery.

A >5s transcript gap in a claude-native Task sub-agent posted status "idle",
which the runner consumed as an authoritative completion: a false "sub-agent
finished" inbox notice ~1-2 min after spawn, and the real completion was
discarded once ``delivered`` latched. The badge now posts a distinct
"quiesced" status that the server publishes for the UI but never forwards to
the runner.
"""

from __future__ import annotations

from omnigent.server.routes._sessions.common import _EXTERNAL_SESSION_STATUS_VALUES


def test_quiesced_is_an_accepted_status_value() -> None:
    assert "quiesced" in _EXTERNAL_SESSION_STATUS_VALUES


def test_forwarder_quiescence_posts_quiesced_not_idle() -> None:
    """The forwarder's quiescence branch emits the badge value, never idle."""
    import inspect

    from omnigent.harnesses.claude_native import forwarder as fwd

    src = inspect.getsource(fwd)
    # The quiescence branch chooses the badge value; asserting on the source
    # keeps the regression tied to the branch itself (any revert to "idle"
    # reintroduces the false-terminal path). Anchored on the branch's own
    # condition rather than an offset from the enclosing block's first line:
    # this fork has branches of its own between the two.
    anchor = src.index("> _SUBAGENT_IDLE_QUIESCENCE_S")
    quiescence_block = src[anchor : anchor + 400]
    assert 'else "quiesced"' in quiescence_block
    assert 'else "idle"' not in quiescence_block


def test_runner_terminal_branch_ignores_quiesced() -> None:
    """The runner's terminal-delivery condition admits only idle/failed."""
    import inspect

    from omnigent.runner import app as runner_app

    src = inspect.getsource(runner_app)
    # This fork delivers the structured terminal edges too, so the condition
    # lists more than upstream's idle/failed pair — the claim under test is
    # only that the badge value is not one of them.
    anchor = 'if status in ("idle", "completed", "failed", "stopped", "killed"):'
    terminal_block = src[src.index(anchor) : src.index(anchor) + 400]
    assert 'status in ("idle", "completed")' in terminal_block
    assert '"quiesced"' not in terminal_block, (
        "the badge value must never appear in the terminal path"
    )
