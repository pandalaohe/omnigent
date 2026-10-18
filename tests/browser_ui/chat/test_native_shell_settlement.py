"""Exercise shell settlement in the built SPA with controlled backend events."""

from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests.browser_ui.chat.session_contract import ChatSessionContract, message_item

_USER = '[data-testid="message-bubble"][data-role="user"]'


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "mobile"])
def test_shell_mirror_settles_its_bubble_before_the_next_prompt(
    page: Page,
    chat_session_contract: ChatSessionContract,
    output_path: str,
    width: int,
) -> None:
    """A shell prompt stays visible as one user turn across settlement and reload."""
    chat = chat_session_contract
    chat.harness = "claude-native"
    chat.event_ack = {"queued": True, "pending_id": "pending-shell"}
    artifacts = Path(output_path)
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(chat.url)
    chat.wait_for_stream()

    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=20_000)
    composer.fill("!echo shell-settled")
    with page.expect_response(
        lambda response: response.url.endswith(f"/{chat.session_id}/events")
    ):
        page.get_by_role("button", name="Send", exact=True).click()
    expect(page.locator(_USER)).to_have_count(1)
    page.screenshot(path=str(artifacts / "shell-pending.png"), full_page=True)

    shell_input = {
        "id": "shell-input",
        "response_id": "shell-turn",
        "type": "terminal_command",
        "kind": "input",
        "input": "echo shell-settled",
    }
    shell_output = {
        "id": "shell-output",
        "response_id": "shell-turn",
        "type": "terminal_command",
        "kind": "output",
        "stdout": "shell-settled\n",
    }
    for item in (shell_input, shell_output):
        chat.emit({"event": "response.output_item.done", "data": {"item": item}})
    shell = page.locator(f'{_USER}[data-user-message-id="shell-input"]')
    expect(shell).to_have_count(1)
    expect(shell).to_contain_text("!echo shell-settled")
    expect(page.locator(_USER)).to_have_count(1)
    expect(shell.get_by_test_id("copy-message-link")).to_be_enabled()
    output = page.locator('[data-testid="terminal-command-card"][data-terminal-kind="output"]')
    expect(output).to_have_count(1)
    output.click()
    expect(page.get_by_text("shell-settled", exact=True)).to_be_visible()
    chat.emit_idle(None)

    chat.event_ack = {"queued": True, "pending_id": "pending-next"}
    prompt = "Reply with prompt-settled"
    composer.fill(prompt)
    with page.expect_response(
        lambda response: response.url.endswith(f"/{chat.session_id}/events")
    ):
        page.get_by_role("button", name="Send", exact=True).click()
    expect(page.locator(_USER)).to_have_count(2)
    expect(page.locator(_USER).last).to_contain_text(prompt)
    user = message_item("next-user", "user", prompt, response_id="next-turn")
    assistant = message_item(
        "next-assistant", "assistant", "prompt-settled", response_id="next-turn"
    )
    chat.emit_busy("next-turn")
    chat.emit(
        {
            "event": "session.input.consumed",
            "data": {
                "type": "session.input.consumed",
                "data": {
                    "item_id": user["id"],
                    "type": "message",
                    "cleared_pending_id": "pending-next",
                    "data": {"role": "user", "content": user["content"], "user_authored": True},
                },
            },
        }
    )
    chat.emit({"event": "response.output_item.done", "data": {"item": assistant}})
    chat.emit_idle("next-turn")
    expect(page.locator(_USER)).to_have_count(2)
    expect(page.get_by_text("prompt-settled", exact=True)).to_be_visible()

    expect(page.get_by_test_id("working-indicator")).to_have_count(0)
    assert len(chat.event_posts) == 2
    page.screenshot(path=str(artifacts / "shell-settled.png"), full_page=True)

    chat.set_items([assistant, user, shell_output, shell_input])
    chat.update_session(pending_inputs=[])
    page.reload()
    chat.wait_for_stream()
    expect(page.locator(_USER)).to_have_count(2)
    expect(page.locator(_USER).last).to_contain_text(prompt)
    expect(shell).to_have_count(1)
    expect(page.get_by_text("prompt-settled", exact=True)).to_be_visible()


def test_phone_reconnect_settles_shell_before_later_replies(
    page: Page,
    chat_session_contract: ChatSessionContract,
) -> None:
    """A shell mirror missed by the stream must settle during history recovery."""
    chat = chat_session_contract
    chat.harness = "claude-native"
    chat.event_ack = {"queued": True, "pending_id": "pending-gap-shell"}
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(chat.url)
    chat.wait_for_stream()
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=20_000)
    composer.fill("! echo gap-shell")
    with page.expect_response(
        lambda response: response.url.endswith(f"/{chat.session_id}/events")
    ):
        page.get_by_role("button", name="Send", exact=True).click()
    expect(page.locator(_USER)).to_have_count(1)

    shell_input = {
        "id": "gap-shell-input",
        "response_id": "gap-shell-turn",
        "type": "terminal_command",
        "kind": "input",
        "input": "echo gap-shell",
    }
    reply = message_item(
        "gap-reply", "assistant", "Reply after shell", response_id="gap-reply-turn"
    )
    chat.set_items([reply, shell_input])
    # End only the browser's current transport, without a terminal [DONE].
    chat.stream.disconnect()
    chat.wait_for_stream()
    expect(page.get_by_text("Reply after shell", exact=True)).to_be_visible(timeout=20_000)
    expect(page.locator(_USER)).to_have_count(1)
    expect(page.locator(_USER)).to_have_attribute("data-user-message-id", "gap-shell-input")

    chat.emit(
        {
            "event": "response.output_item.done",
            "data": {
                "item": message_item(
                    "later-reply",
                    "assistant",
                    "Another reply after shell",
                    response_id="later-turn",
                )
            },
        }
    )
    expect(page.get_by_text("Another reply after shell", exact=True)).to_be_visible()
    expect(page.locator(_USER)).to_have_count(1)
    shell_box = page.locator(_USER).bounding_box()
    reply_box = page.get_by_text("Another reply after shell", exact=True).bounding_box()
    assert shell_box is not None and reply_box is not None
    assert shell_box["y"] + shell_box["height"] <= reply_box["y"]


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "mobile"])
def test_terminal_shell_commands_keep_their_user_turns(
    page: Page,
    chat_session_contract: ChatSessionContract,
    output_path: str,
    width: int,
) -> None:
    """Terminal-origin commands, including repeats, remain outside assistant folds."""
    chat = chat_session_contract
    chat.harness = "claude-native"
    artifacts = Path(output_path)
    page.set_viewport_size({"width": width, "height": 844})
    items = [
        message_item("greeting-user", "user", "hey", response_id="greeting"),
        message_item("greeting-answer", "assistant", "Hello!", response_id="greeting"),
    ]
    chat.set_items(list(reversed(items)))
    page.goto(chat.url)
    chat.wait_for_stream()
    expect(page.locator(_USER)).to_have_count(1)

    commands = [('echo "hi"', "hi\n"), ("ls", "README.md\nsrc\n"), ('echo "hi"', "hi\n")]
    for index, (command, stdout) in enumerate(commands):
        turn = f"shell-{index}"
        turn_items = [
            {
                "id": f"{turn}-input",
                "response_id": turn,
                "type": "terminal_command",
                "kind": "input",
                "input": command,
            },
            {
                "id": f"{turn}-output",
                "response_id": turn,
                "type": "terminal_command",
                "kind": "output",
                "stdout": stdout,
            },
            message_item(
                f"{turn}-answer", "assistant", f"Command {index + 1} finished.", response_id=turn
            ),
        ]
        for item in turn_items:
            chat.emit({"event": "response.output_item.done", "data": {"item": item}})
        items.extend(turn_items)
    chat.emit_idle(None)
    expect(page.get_by_text("Command 3 finished.", exact=True)).to_be_visible()
    page.screenshot(path=str(artifacts / "terminal-shell-live.png"), full_page=True)
    expect(page.locator(_USER)).to_have_count(4)
    assert chat.event_posts == []
    for index, (command, _) in enumerate(commands):
        shell = page.locator(f'{_USER}[data-user-message-id="shell-{index}-input"]')
        expect(shell).to_contain_text(f"!{command}")
        expect(shell.get_by_test_id("copy-message-link")).to_be_enabled()

    chat.set_items(list(reversed(items)))
    page.reload()
    chat.wait_for_stream()
    expect(page.locator(_USER)).to_have_count(4)
    for index, (command, _) in enumerate(commands):
        shell = page.locator(f'{_USER}[data-user-message-id="shell-{index}-input"]')
        expect(shell).to_contain_text(f"!{command}")
    page.screenshot(path=str(artifacts / "terminal-shell-reloaded.png"), full_page=True)
