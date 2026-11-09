"""Touch-viewport quick buttons drive the composer and real xterm input bridge."""

import json
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
            terminalButtons: [['Esc', 'Escape'], ['Tab', 'Tab'], ['Up', 'ArrowUp'],
                ['Down', 'ArrowDown'], ['Left', 'ArrowLeft'],
                ['Right', 'ArrowRight'], ['Enter', 'Enter']].map(([label, code]) => ({
                    id: code, label, kind: 'key', chord: {code, modifiers: []}
                })).concat([{id: 'ctrl', label: 'Ctrl', kind: 'modifier', modifier: 'ctrl'}]),
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
    expect(page.get_by_role("menu")).to_have_count(0)
    terminal = page.locator('[data-testid="terminal-view"]:visible').first
    expect(terminal).to_have_attribute("data-state", "connected", timeout=20_000)
    expect(terminal.get_by_role("toolbar", name="Terminal keys")).to_be_visible()
    expect(page.get_by_role("button", name="Open floating assistant", exact=True)).to_have_count(0)
    # A short viewport cannot fit the key rows; custom actions use the floating fallback.
    page.set_viewport_size({"width": 390, "height": 260})
    expect(terminal.get_by_role("toolbar", name="Terminal keys")).to_have_count(0)
    expect(page.get_by_role("button", name="Open floating assistant", exact=True)).to_be_visible()
    textarea = terminal.locator("textarea.xterm-helper-textarea")
    textarea.focus()
    return textarea, inputs


@pytest.mark.parametrize(
    ("label", "expected"),
    [
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
    # Terminal keys preserve the dismissed keyboard; only Keyboard explicitly opens it.
    expect(textarea).not_to_be_focused()


def test_sticky_ctrl_arrow_reaches_cli_bridge(
    page: Page, cli_bridge: tuple[Locator, list[bytes]]
) -> None:
    _, inputs = cli_bridge
    page.set_viewport_size({"width": 390, "height": 844})
    toolbar = page.get_by_role("toolbar", name="Terminal keys")
    expect(toolbar).to_be_visible()
    toolbar.get_by_role("button", name="Ctrl", exact=True).tap()
    toolbar.get_by_role("button", name="Up", exact=True).tap()
    toolbar.get_by_role("button", name="Down", exact=True).tap()
    deadline = time.monotonic() + 3
    while len(inputs) < 2 and time.monotonic() < deadline:
        page.wait_for_timeout(10)
    assert inputs == [b"\x1b[1;5A", b"\x1b[B"]


def _install_arranged_bar(page: Page) -> None:
    """Store a new-client layout with its old-client compatibility projection."""
    old = page.evaluate(
        "JSON.parse(localStorage.getItem('omnigent:mobile-assistant-preferences'))"
    )
    legacy = old["terminalButtons"]
    serialized = json.dumps(
        {"terminalButtons": legacy, "terminalSecondRow": True},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    fingerprint = 0x811C9DC5
    for character in serialized:
        fingerprint = ((fingerprint ^ ord(character)) * 0x01000193) & 0xFFFFFFFF
    keys = [
        {
            **item,
            "width": 2,
            "display": "text",
            **({"repeat": True} if item["id"] == "ArrowUp" else {}),
        }
        for item in legacy
    ]
    old.update(
        terminalBar={
            "top": {
                "keys": [
                    {
                        "id": "shift-key",
                        "label": "Shift",
                        "width": 1,
                        "display": "text",
                        "kind": "modifier",
                        "modifier": "shift",
                    },
                    *keys[:6],
                    {
                        "id": "phrase-key",
                        "label": "Greeting",
                        "width": 2,
                        "display": "text",
                        "kind": "text",
                        "text": "hello",
                        "submit": True,
                    },
                    {
                        "id": "keyboard",
                        "label": "Keyboard",
                        "width": 2,
                        "display": "icon",
                        "icon": "keyboard",
                        "kind": "action",
                        "action": "keyboard",
                    },
                ],
                "pinned": 2,
                "enabled": True,
            },
            "bottom": {
                "keys": [
                    *keys[6:],
                    *[
                        {
                            "id": f"extra-{index}",
                            "label": f"F{index + 1}",
                            "width": 2,
                            "display": "text",
                            "kind": "key",
                            "chord": {"code": f"F{index + 1}", "modifiers": []},
                        }
                        for index in range(5)
                    ],
                ],
                "pinned": 1,
                "enabled": True,
            },
        },
        terminalSecondRow=True,
        terminalLegacyBasis=f"fnv1a:{fingerprint:08x}",
    )
    page.evaluate(
        "value => {localStorage.setItem('omnigent:mobile-assistant-preferences',"
        " JSON.stringify(value));"
        " window.dispatchEvent(new Event('omnigent:mobile-assistant-changed'));}",
        old,
    )


def test_arranged_rows_page_independently_and_preserve_pins(
    page: Page, cli_bridge: tuple[Locator, list[bytes]]
) -> None:
    textarea, inputs = cli_bridge
    page.set_viewport_size({"width": 390, "height": 844})
    _install_arranged_bar(page)
    toolbar = page.get_by_role("toolbar", name="Terminal keys")
    top = toolbar.get_by_role("button", name=re.compile(r"Top row: next page"))
    bottom = toolbar.get_by_role("button", name=re.compile(r"Bottom row: next page"))
    expect(top).to_be_visible()
    expect(bottom).to_be_visible()
    expect(toolbar.locator('[data-key-id="Escape"]')).to_be_visible()
    expect(toolbar.locator('[data-key-id="Enter"]')).to_be_visible()
    boxes = [button.bounding_box() for button in (top, bottom)]
    assert all(box and box["height"] >= 24 and box["width"] >= 56 for box in boxes)
    assert boxes[0]["x"] < boxes[1]["x"]
    for key in toolbar.locator("button[data-key-id]").all():
        box = key.bounding_box()
        assert box is not None
        assert box["y"] + box["height"] <= boxes[0]["y"]
    bottom_name = bottom.get_attribute("aria-label")
    top.focus()
    top.press("Enter")
    expect(top).to_have_attribute("aria-label", re.compile(r"2 of 2"))
    expect(bottom).to_have_attribute("aria-label", bottom_name)
    expect(toolbar.locator('[data-key-id="Escape"]')).to_be_visible()
    toolbar.get_by_role("button", name="Greeting", exact=True).tap()
    expect(page.locator('[data-testid="terminal-view"]:visible')).to_be_visible()
    deadline = time.monotonic() + 3
    while not inputs and time.monotonic() < deadline:
        page.wait_for_timeout(10)
    assert inputs == [b"hello\r"]
    toolbar.get_by_role("button", name="Keyboard", exact=True).tap()
    expect(textarea).to_be_focused()
    bottom.focus()
    bottom.press("Space")
    expect(bottom).to_have_attribute("aria-label", re.compile(r"2 of 2"))
    expect(toolbar.locator('[data-key-id="Enter"]')).to_be_visible()


def test_shift_bar_key_modifies_the_real_terminal_input(
    page: Page, cli_bridge: tuple[Locator, list[bytes]]
) -> None:
    textarea, inputs = cli_bridge
    page.set_viewport_size({"width": 390, "height": 844})
    _install_arranged_bar(page)
    toolbar = page.get_by_role("toolbar", name="Terminal keys")
    toolbar.locator('[data-key-id="shift-key"]').tap()
    toolbar.get_by_role("button", name="Up", exact=True).tap()
    textarea.focus()
    textarea.press("a")
    deadline = time.monotonic() + 3
    while len(inputs) < 2 and time.monotonic() < deadline:
        page.wait_for_timeout(10)
    assert inputs == [b"\x1b[1;2A", b"a"]


def test_wheel_shares_text_modifiers_long_press_and_repeat(
    page: Page, cli_bridge: tuple[Locator, list[bytes]]
) -> None:
    _, inputs = cli_bridge
    _install_arranged_bar(page)
    page.get_by_role("button", name="Open floating assistant", exact=True).tap()
    shift = page.get_by_role("button", name="Shift", exact=True)
    shift.tap()
    page.get_by_role("button", name="Greeting", exact=True).tap()
    expect(shift).to_have_attribute("data-sticky-state", "once")
    up = page.get_by_role("button", name="Up", exact=True)
    up.tap()
    expect(shift).to_have_attribute("data-sticky-state", "off")
    deadline = time.monotonic() + 3
    while len(inputs) < 2 and time.monotonic() < deadline:
        page.wait_for_timeout(10)
    assert inputs == [b"hello\r", b"\x1b[1;2A"]

    box = shift.bounding_box()
    assert box is not None
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()
    expect(shift).to_have_attribute("data-sticky-state", "locked")
    page.mouse.up()
    expect(shift).to_have_attribute("data-sticky-state", "locked")
    up.tap()
    expect(shift).to_have_attribute("data-sticky-state", "locked")
    shift.tap()
    box = up.bounding_box()
    assert box is not None
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()
    deadline = time.monotonic() + 3
    while len(inputs) < 5 and time.monotonic() < deadline:
        page.wait_for_timeout(10)
    page.mouse.up()
    assert inputs[2] == b"\x1b[1;2A"
    assert len(inputs) >= 5
    assert all(payload == b"\x1b[A" for payload in inputs[3:])


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
