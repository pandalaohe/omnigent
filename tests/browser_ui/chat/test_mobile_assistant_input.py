"""Touch-viewport quick buttons drive the composer and real xterm input bridge."""

import re
import time

import pytest
from playwright.sync_api import Locator, Page, WebSocketRoute, expect

from tests.browser_ui.chat.session_contract import ChatSessionContract, list_payload


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    return {
        **browser_context_args,
        "viewport": {"width": 390, "height": 844},
        "has_touch": True,
        "is_mobile": True,
    }


def _configure_buttons(page: Page) -> None:
    page.add_init_script("""
        localStorage.setItem('omnigent:mobile-assistant-preferences', JSON.stringify({
            version: 2, enabled: true,
            buttons: [
                {id: 'phrase', label: 'Phrase', binding: {kind: 'text', text: 'hello'}},
                ...[['Esc', 'Escape'], ['Tab', 'Tab'], ['Up', 'ArrowUp'],
                    ['Down', 'ArrowDown'], ['Left', 'ArrowLeft'],
                    ['Right', 'ArrowRight'], ['Enter', 'Enter']].map(([label, code]) => ({
                        id: code, label, repeat: code === 'ArrowUp' || code === 'ArrowDown',
                        binding: {kind: 'key', chord: {code, modifiers: []}}
                    }))
            ]
        }));
    """)


def test_phrase_stays_in_composer_at_selection(
    page: Page, chat_session_contract: ChatSessionContract
) -> None:
    _configure_buttons(page)
    page.goto(chat_session_contract.url)
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible()
    composer.fill("first last")
    composer.evaluate("el => el.setSelectionRange(6, 6)")

    page.get_by_role("button", name="Open floating assistant", exact=True).tap()
    expect(composer).to_be_focused()
    page.get_by_role("button", name="Conversation actions", exact=True).focus()
    page.get_by_role("button", name="Phrase", exact=True).tap()
    expect(composer).to_have_value("first hellolast")
    expect(composer).to_be_focused()
    page.get_by_role("button", name="Send", exact=True).tap()
    assert chat_session_contract.event_posts[0]["body"]["data"]["content"] == [
        {"type": "input_text", "text": "first hellolast"}
    ]


@pytest.fixture
def cli_bridge(
    page: Page, chat_session_contract: ChatSessionContract
) -> tuple[Locator, list[bytes]]:
    chat = chat_session_contract
    _configure_buttons(page)
    chat.harness = "codex-native"
    chat.update_session(labels={"omnigent.ui": "terminal", "omnigent.wrapper": "codex-native-ui"})
    chat.contract.json(f"/v1/sessions/{chat.session_id}/codex_goal", {"goal": None})
    terminal_id = "terminal_codex_main"
    chat.contract.json(
        f"/v1/sessions/{chat.session_id}/resources/terminals",
        list_payload(
            [
                {
                    "id": terminal_id,
                    "object": "terminal",
                    "name": "codex",
                    "metadata": {"running": True, "terminal_name": "codex", "session_key": "main"},
                }
            ]
        ),
    )
    inputs: list[bytes] = []

    def attach(socket: WebSocketRoute) -> None:
        socket.on_message(
            lambda message: inputs.append(message) if isinstance(message, bytes) else None
        )
        socket.send(b"\x1b[2J\x1b[HCLI ready\r\n")

    chat.contract.websocket(re.compile(rf"/resources/terminals/{terminal_id}/attach"), attach)
    page.goto(chat.url)
    page.get_by_role("button", name="Conversation actions", exact=True).tap()
    page.get_by_role("menuitem", name="Terminal", exact=True).tap()
    terminal = page.locator('[data-testid="terminal-view"]:visible').first
    expect(terminal).to_have_attribute("data-state", "connected", timeout=20_000)
    textarea = terminal.locator("textarea.xterm-helper-textarea")
    textarea.focus()
    return textarea, inputs


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Phrase", b"hello"),
        ("Up", b"\x1b[A"),
        ("Down", b"\x1b[B"),
        ("Left", b"\x1b[D"),
        ("Right", b"\x1b[C"),
        ("Tab", b"\t"),
        ("Esc", b"\x1b"),
        ("Enter", b"\r"),
    ],
)
def test_phrase_and_keys_reach_cli_bridge(
    page: Page, cli_bridge: tuple[Locator, list[bytes]], label: str, expected: bytes
) -> None:
    textarea, inputs = cli_bridge
    page.get_by_role("button", name="Conversation actions", exact=True).focus()
    page.get_by_role("button", name="Open floating assistant", exact=True).tap()
    page.get_by_role("button", name=label, exact=True).tap()
    deadline = time.monotonic() + 3
    while b"".join(inputs) != expected and time.monotonic() < deadline:
        page.wait_for_timeout(10)
    assert b"".join(inputs) == expected
    expect(textarea).to_be_focused()


def test_switching_to_chat_does_not_type_into_the_hidden_terminal(
    page: Page, cli_bridge: tuple[Locator, list[bytes]]
) -> None:
    _, inputs = cli_bridge
    page.get_by_role("button", name="Conversation actions", exact=True).tap()
    page.get_by_role("menuitem", name="Chat", exact=True).tap()
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible()

    page.get_by_role("button", name="Open floating assistant", exact=True).tap()
    page.get_by_role("button", name="Phrase", exact=True).tap()
    # Allow a wrongly dispatched WebSocket frame to reach the mocked receiver.
    page.wait_for_timeout(100)
    assert inputs == []

    composer.tap()
    page.get_by_role("button", name="Phrase", exact=True).tap()
    expect(composer).to_have_value("hello")
    assert inputs == []
