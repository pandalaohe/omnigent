"""Touch-viewport terminal keys drive the real xterm input bridge."""

import json
import re
import time

import pytest
from playwright.sync_api import Locator, Page, WebSocketRoute, expect

from tests._helpers.polling import poll_until
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
    page.add_init_script("""(() => {
        if (localStorage.getItem('omnigent:mobile-assistant-preferences') !== null) return;
        localStorage.setItem('omnigent:mobile-assistant-preferences', JSON.stringify({
            version: 2, enabled: true,
            terminalButtons: [['Esc', 'Escape'], ['Tab', 'Tab'], ['Up', 'ArrowUp'],
                ['Down', 'ArrowDown'], ['Left', 'ArrowLeft'],
                ['Right', 'ArrowRight'], ['Enter', 'Enter']].map(([label, code]) => ({
                    id: code, label, kind: 'key', chord: {code, modifiers: []}
                })).concat([{id: 'ctrl', label: 'Ctrl', kind: 'modifier', modifier: 'ctrl'}]),
            buttons: []
        }));
    })()""")


def _open_cli_terminal(page: Page, chat: ChatSessionContract) -> Locator:
    page.goto(chat.url)
    page.get_by_role("button", name="Conversation actions", exact=True).tap()
    page.get_by_role("menuitem", name="Terminal", exact=True).tap()
    expect(page.get_by_role("menu")).to_have_count(0)
    terminal = page.locator('[data-testid="terminal-view"]:visible').first
    expect(terminal).to_have_attribute("data-state", "connected", timeout=20_000)
    return terminal


@pytest.fixture
def cli_bridge(
    page: Page, chat_session_contract: ChatSessionContract
) -> tuple[Locator, list[bytes]]:
    chat = chat_session_contract
    _configure_buttons(page)
    chat.harness = "codex-native"
    chat.update_session(labels={"omnigent.ui": "terminal", "omnigent.wrapper": "codex-native-ui"})
    chat.contract.json(f"/v1/sessions/{chat.session_id}/codex_goal", {"goal": None})
    chat.contract.json(
        f"/v1/sessions/{chat.session_id}/host-terminals", {"terminals": [], "complete": True}
    )
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
    terminal = _open_cli_terminal(page, chat)
    expect(terminal.get_by_role("toolbar", name="Terminal keys")).to_be_visible()
    expect(page.get_by_role("button", name="Open floating assistant", exact=True)).to_have_count(0)
    # A short viewport cannot fit the key rows.
    page.set_viewport_size({"width": 390, "height": 260})
    expect(terminal.get_by_role("toolbar", name="Terminal keys")).to_have_count(0)
    expect(page.get_by_role("button", name="Open floating assistant", exact=True)).to_have_count(0)
    textarea = terminal.locator("textarea.xterm-helper-textarea")
    textarea.focus()
    page.set_viewport_size({"width": 390, "height": 844})
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
def test_keys_reach_cli_bridge(
    page: Page, cli_bridge: tuple[Locator, list[bytes]], label: str, expected: bytes
) -> None:
    textarea, inputs = cli_bridge
    page.get_by_role("button", name="Conversation actions", exact=True).focus()
    page.get_by_role("toolbar", name="Terminal keys").get_by_role(
        "button", name=label, exact=True
    ).tap()
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
    toolbar.get_by_role("button", name=re.compile(r"^Ctrl,")).tap()
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


def test_record_native_terminal_key_and_replay_its_chord(
    page: Page,
    chat_session_contract: ChatSessionContract,
    cli_bridge: tuple[Locator, list[bytes]],
) -> None:
    _, inputs = cli_bridge
    chat = chat_session_contract
    chat.contract.json("/v1/model-connections", {"connections": [], "can_manage": False})
    page.goto(f"{chat.base_url}/settings/shortcuts")
    page.get_by_role("tab", name="Native terminal").tap()
    panel = page.get_by_role("tabpanel", name="Native terminal")
    panel.get_by_role("button", name="Add key", exact=True).last.tap()
    sheet = page.get_by_role("dialog", name="Add terminal key")
    sheet.get_by_role("button", name="Record", exact=True).tap()
    page.keyboard.press("Control+b")
    expect(sheet.get_by_label("Send sequence")).to_have_value("^B")
    expect(sheet.get_by_label("Label", exact=True)).to_have_value("ctrl+b")
    sheet.get_by_role("radio", name="Icon", exact=True).tap()
    expect(sheet.get_by_text("Keys · Page 1 of 3")).to_be_visible()
    sheet.get_by_role("button", name="Next icon page").tap()
    expect(sheet.get_by_text("Tools and agents · Page 2 of 3")).to_be_visible()
    sheet.get_by_role("button", name="Next icon page").tap()
    expect(sheet.get_by_text("Symbols · Page 3 of 3")).to_be_visible()
    sheet.get_by_role("button", name="Previous icon page").tap()
    sheet.get_by_role("button", name="codex", exact=True).tap()
    sheet.get_by_role("button", name="Save", exact=True).tap()
    expect(sheet).to_have_count(0)

    panel.get_by_role("button", name=re.compile(r"^Edit ctrl\+b,")).tap()
    edit = page.get_by_role("dialog", name="Edit terminal key")
    edit.get_by_label("Label", exact=True).fill("Build")
    edit.get_by_role("button", name="Save", exact=True).tap()
    expect(edit).to_have_count(0)
    page.get_by_role("tab", name="Shell", exact=True).tap()
    expect(
        page.get_by_role("tabpanel", name="Shell").get_by_role(
            "button", name=re.compile(r"^Edit Build,")
        )
    ).to_have_count(0)

    terminal = _open_cli_terminal(page, chat)
    toolbar = terminal.get_by_role("toolbar", name="Terminal keys")
    key = toolbar.get_by_role("button", name="Build, sends a terminal sequence", exact=True)
    expect(key.locator("svg")).to_be_visible()
    key.tap()

    def chord_arrived() -> bool:
        page.wait_for_timeout(20)
        return b"".join(inputs) == b"\x02"

    poll_until(chord_arrived, timeout_s=5, interval_s=0.02)

    page.goto(f"{chat.base_url}/settings/shortcuts")
    page.get_by_role("tab", name="Native terminal").tap()
    page.get_by_role("button", name=re.compile(r"^Edit Build,")).tap()
    edit.get_by_role("button", name="Delete key", exact=True).tap()
    expect(edit).to_have_count(0)
    terminal = _open_cli_terminal(page, chat)
    expect(
        terminal.get_by_role("button", name="Build, sends a terminal sequence", exact=True)
    ).to_have_count(0)
