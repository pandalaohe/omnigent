"""Next-message notes preserve user content and stay held across ineligible events."""

import pytest

from omnigent.server import shell_notes


@pytest.mark.parametrize(
    "text",
    ["/model x", "!pwd", "/btw hi", "/side hi", "[System: x]", "[Peer message from session x]"],
)
def test_commands_and_runtime_posts_do_not_take_a_note(text: str) -> None:
    data = {"role": "user", "content": [{"type": "input_text", "text": text}]}
    assert not shell_notes.eligible("message", data, {})


def test_eligibility_excludes_peers_and_joint_sessions() -> None:
    data = {"role": "user", "content": [{"type": "input_text", "text": "hello"}]}
    assert shell_notes.eligible("message", data, {})
    assert not shell_notes.eligible("message", {**data, "peer_delivery": {"attempt_id": "x"}}, {})
    assert not shell_notes.eligible(
        "message", data, {"omnigent.member.a": "{}", "omnigent.member.b": "{}"}
    )
    assert not shell_notes.eligible("message", {**data, "role": "assistant"}, {})
    assert not shell_notes.eligible("message", {**data, "content": []}, {})


def test_notes_are_bounded_one_shot_and_restored_on_failure() -> None:
    for index in range(6):
        shell_notes.hold("notes", f"terminal-{index}", f"instance-{index}", f"line-{index}")
    shell_notes.hold("notes", "terminal-5", "instance-5", "line-5")
    taken = shell_notes.take("notes")
    assert taken is not None
    assert taken.prefix == "line-1\nline-2\nline-3\nline-4\nline-5\n\n"
    assert shell_notes.take("notes") is None
    shell_notes.restore("notes", taken)
    shell_notes.drop("notes", "terminal-3")
    restored = shell_notes.take("notes")
    assert restored is not None and "line-3" not in restored.prefix
    assert shell_notes.take("notes") is None
    shell_notes.release("notes", restored)


def test_failed_forward_does_not_restore_a_closed_instance() -> None:
    shell_notes.hold("closed-notes", "shell", "old", "old line")
    taken = shell_notes.take("closed-notes")
    assert taken is not None
    shell_notes.drop("closed-notes", "shell")
    shell_notes.hold("closed-notes", "shell", "new", "new line")
    shell_notes.restore("closed-notes", taken)
    restored = shell_notes.take("closed-notes")
    assert restored is not None and restored.prefix == "new line\n\n"
    shell_notes.release("closed-notes", taken)
    shell_notes.restore("closed-notes", restored)
    assert shell_notes.take("closed-notes") is not None
    shell_notes.release("closed-notes", restored)
    assert shell_notes.take("closed-notes") is None
