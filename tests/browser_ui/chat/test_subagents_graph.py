"""Readability of the real Agents graph in the built SPA."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests.browser_ui.chat.session_contract import ChatSessionContract, list_payload


@pytest.mark.parametrize("count", [1, 5, 32])
@pytest.mark.parametrize("width", [None, 320, 800])
def test_agents_graph_opens_at_readable_size(
    page: Page,
    chat_session_contract: ChatSessionContract,
    output_path: str,
    count: int,
    width: int | None,
) -> None:
    chat = chat_session_contract
    children = [
        {
            "id": f"graph-child-{index}",
            "title": f"Review section {index + 1}",
            "session_name": f"Review section {index + 1}",
            "tool": "reviewer",
            "busy": False,
            "current_task_status": "completed",
            "last_message_preview": "Review complete",
            "pending_elicitations_count": 0,
        }
        for index in range(count)
    ]
    chat.contract.json(f"/v1/sessions/{chat.session_id}/child_sessions", list_payload(children))
    chat.contract.json(
        re.compile(r"/v1/sessions/graph-child-\d+/child_sessions"), list_payload([])
    )
    state: dict[str, object] = {"open": True, "rightRailTab": "subagents"}
    if width is not None:
        state["widthPx"] = width
    page.add_init_script(
        "localStorage.setItem('omnigent:session-workspace-state', "
        + json.dumps(json.dumps([{"id": chat.session_id, "state": state}]))
        + ");"
    )
    page.set_viewport_size({"width": 1920 if width == 800 else 1280, "height": 800})
    page.goto(chat.url)
    graph_toggle = page.get_by_test_id("view-mode-graph")
    expect(graph_toggle).to_be_visible(timeout=15_000)
    graph_toggle.click()
    graph = page.locator(".react-flow")
    expect(graph).to_be_visible()
    expect(graph.locator(".react-flow__node")).to_have_count(count + 1)
    root = graph.locator(f'.react-flow__node[data-id="{chat.session_id}"]')
    expect(root).to_be_in_viewport(ratio=1)
    page.wait_for_function(
        """() => {
            const graph = document.querySelector('.react-flow');
            const node = graph?.querySelector('.react-flow__node');
            return node && node.getBoundingClientRect().width >= 175;
        }""",
        timeout=5000,
    )
    measurement = root.evaluate(
        """node => {
            const label = node.querySelector('span.truncate');
            const zoom = node.getBoundingClientRect().width / node.offsetWidth;
            return {zoom, font: parseFloat(getComputedStyle(label).fontSize) * zoom,
                graphWidth: node.closest('.react-flow').getBoundingClientRect().width};
        }"""
    )
    assert measurement["font"] >= 13, measurement
    centered = root.bounding_box()
    graph_box = graph.bounding_box()
    assert centered is not None and graph_box is not None
    assert abs(centered["x"] + centered["width"] / 2 - graph_box["x"] - graph_box["width"] / 2) < 2
    artifacts = Path(output_path)
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / f"graph-{count}.json").write_text(json.dumps(measurement), encoding="utf-8")
    page.screenshot(path=str(artifacts / f"graph-{count}.png"))
    page.get_by_role("button", name="Zoom in", exact=True).click()
    page.get_by_role("button", name="Reset view", exact=True).click()
    page.wait_for_function(
        """() => {
            const node = document.querySelector('.react-flow__node');
            return Math.abs(node.getBoundingClientRect().width / node.offsetWidth - 1) < 0.01;
        }"""
    )
    expect(root).to_be_in_viewport()
    if count == 32:
        box = graph.bounding_box()
        assert box is not None
        before_pan = root.bounding_box()
        assert before_pan is not None
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.wheel(450, 0)
        page.wait_for_function(
            """({id, x}) => {
                const node = document.querySelector(`.react-flow__node[data-id="${id}"]`);
                return node.getBoundingClientRect().x < x - 50;
            }""",
            arg={"id": chat.session_id, "x": before_pan["x"]},
        )
        page.get_by_role("button", name="Reset view", exact=True).click()
        expect(root).to_be_in_viewport()
    page.get_by_test_id("view-mode-list").click()
    expect(page.get_by_test_id("subagent-main-row")).to_be_visible()
    expect(graph).to_have_count(0)
