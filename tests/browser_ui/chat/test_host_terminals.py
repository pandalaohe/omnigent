"""Host-row entry reaches the real xterm UI over the frozen host-terminal contract.

This browser lane mocks HTTP/WS, as all browser_ui tests do. Runner/tmux behavior
belongs to the backend integration checks; this proves navigation, attach,
binary input, resize, reload, and the phone layout without a live host.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page, Request, WebSocketRoute, expect

from tests._helpers.polling import poll_until
from tests.browser_ui.chat.session_contract import ChatSessionContract


def test_host_row_opens_working_terminal_page(
    page: Page, chat_session_contract: ChatSessionContract, tmp_path: Path
) -> None:
    chat = chat_session_contract
    contract = chat.contract
    host_id = chat.host_id
    collection = f"/v1/hosts/{host_id}/terminals"
    terminal_id = "a" * 32
    live: list[dict[str, Any]] = []
    creates: list[dict[str, Any]] = []
    inputs: list[bytes] = []
    resizes: list[tuple[int, int]] = []
    connections: list[str] = []
    contract.json(
        "/v1/hosts",
        {
            "hosts": [
                {
                    "host_id": host_id,
                    "name": "host-a",
                    "owner": "local",
                    "status": "online",
                    "configured_harnesses": {chat.harness: True},
                    "capabilities": ["host_terminals_v1"],
                }
            ]
        },
    )
    contract.json(collection, lambda _: {"terminals": live})

    def create(request: Request) -> dict[str, Any]:
        creates.append(request.post_data_json)
        resource = {
            "terminal_id": terminal_id,
            "elevated": False,
            "created_at": 1,
            "attach_path": f"{collection}/{terminal_id}/attach",
        }
        live.append(resource)
        return resource

    contract.json(collection, create, method="POST", status=201)

    def attach(socket: WebSocketRoute) -> None:
        connections.append(socket.url)
        socket.send(b"host-terminal-ready\r\n$ ")

        def receive(message: str | bytes) -> None:
            if isinstance(message, bytes):
                inputs.append(message)
                socket.send(message)
            else:
                control = json.loads(message)
                if control.get("type") == "resize":
                    resizes.append((control["cols"], control["rows"]))

        socket.on_message(receive)

    contract.websocket(f"**{collection}/{terminal_id}/attach*", attach)
    contract.json(
        f"/v1/hosts/{host_id}/filesystem",
        {"object": "list", "data": [], "has_more": False},
    )
    page.goto(chat.base_url)
    page.get_by_test_id("new-chat-landing-host-chip").click()
    page.get_by_role("menuitem", name="Terminal", exact=True).click()
    expect(page).to_have_url(f"{chat.base_url}/hosts/{host_id}/terminals")
    page.get_by_role("button", name="New terminal", exact=True).click()
    page.get_by_role("menuitem", name="Shell", exact=True).click()
    terminal = page.get_by_test_id("terminal-view")
    expect(terminal).to_have_attribute("data-state", "connected")
    terminal.locator("textarea.xterm-helper-textarea").press_sequentially("host-input")

    def input_arrived() -> bool:
        page.wait_for_timeout(20)  # Pump Playwright's WS callbacks while polling.
        return b"host-input" in b"".join(inputs)

    poll_until(input_arrived, timeout_s=5, interval_s=0.02)
    assert creates == [{"elevated": False}]
    assert connections and resizes
    assert all(cols > 0 and rows > 0 for cols, rows in resizes)
    assert b"host-input" in b"".join(inputs)
    main_bounds = page.get_by_role("main").last.bounding_box()
    terminal_bounds = terminal.bounding_box()
    assert main_bounds is not None and terminal_bounds is not None
    assert terminal_bounds["height"] > main_bounds["height"] - 200
    assert terminal_bounds["y"] + terminal_bounds["height"] <= (
        main_bounds["y"] + main_bounds["height"]
    )
    page.screenshot(path=str(tmp_path / "host-terminals-desktop.png"))
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.get_by_role("main").last).to_have_css("position", "fixed")
    bounds = page.get_by_role("main").last.bounding_box()
    assert bounds is not None and bounds["width"] == 390 and bounds["height"] == 844
    terminal_bounds = terminal.bounding_box()
    assert terminal_bounds is not None and terminal_bounds["height"] > 640
    assert terminal_bounds["y"] + terminal_bounds["height"] <= 844
    page.screenshot(path=str(tmp_path / "host-terminals-phone.png"))
    page.reload()
    expect(page.get_by_role("tab", name="Shell · aaaaaa")).to_be_visible()
    expect(page.get_by_test_id("terminal-view")).to_have_attribute("data-state", "connected")
    assert len(connections) >= 2
    assert len(creates) == 1


@pytest.mark.parametrize("width", [1280, 390])
def test_standalone_admin_terminal_approval_page(
    page: Page, chat_session_contract: ChatSessionContract, tmp_path: Path, width: int
) -> None:
    chat = chat_session_contract
    path = f"/v1/sessions/{chat.session_id}/elicitations/terminal-access"
    answers: list[dict[str, Any]] = []
    chat.contract.json(
        path,
        {
            "status": "pending",
            "policy_name": "__ask_on_terminal_agent_access",
            "message": "Allow the agent to repair the requested package",
            "terminal_access_owner": "local",
            "terminal_access_terminal_id": "terminal_shell_admin",
            "terminal_access_instance_id": "admin-instance",
            "terminal_access_host_id": chat.host_id,
        },
    )

    def resolve(request: Request) -> dict[str, bool]:
        answers.append(request.post_data_json)
        return {"queued": False}

    chat.contract.json(path + "/resolve", resolve, method="POST", status=202)
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(f"{chat.base_url}/approve/{chat.session_id}/terminal-access")
    details = page.get_by_test_id("terminal-access-details")
    expect(details).to_contain_text("admin-instance")
    expect(details).to_contain_text("terminal_shell_admin")
    expect(details).to_contain_text(chat.host_id)
    expect(details).to_contain_text("local")
    expect(page.get_by_text("Allow the agent to repair the requested package")).to_be_visible()
    approve = page.get_by_role("button", name="Approve", exact=True)
    expect(approve).to_be_enabled()
    page.screenshot(path=str(tmp_path / f"admin-approval-{width}.png"))
    approve.click()
    expect(page.get_by_text("Approved", exact=True)).to_be_visible()
    assert answers == [{"action": "accept"}]
