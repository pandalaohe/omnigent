from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Locator, Page, Route, expect

from tests._helpers.workspace_geometry import workspace_bar_needs_collapse
from tests.browser_ui.chat.session_contract import ChatSessionContract

_MONITOR_TASK = {
    "id": "monitor-ci",
    "type": "shell",
    "status": "running",
    "description": "Watch PR checks and review comments",
    "command": "gh pr checks 123 --watch",
}


def _stub_session_bar(page: Page, chat: ChatSessionContract, pr_number: int) -> None:
    """Workspace, worktree branch, open PR, busy child, full context and one background task."""
    session_id = chat.session_id
    chat.contract.json("/v1/sessions/acceptance-child/child_sessions", {"data": []})
    chat.contract.json(
        f"/v1/sessions/{session_id}/resources/environments/default",
        {
            "metadata": {"root": "/work/repo", "home": "/opt/work/home"},
        },
    )

    chat.contract.json(
        f"/v1/sessions/{session_id}/resources/environments/default/filesystem", {"data": []}
    )
    chat.contract.json(
        f"/v1/sessions/{session_id}/resources/environments/default/changes", {"data": []}
    )
    chat.contract.json(f"/v1/sessions/{session_id}/resources/github/changes", {"data": []})
    chat.contract.json(f"/v1/sessions/{session_id}/resources/github/diff", {"patch": ""})
    chat.update_session(
        workspace="/work/repo",
        git_branch="old",
        context_window=1_000_000,
        last_total_tokens=1_000_000,
        background_task_count=1,
        background_tasks=[_MONITOR_TASK],
    )
    page.route(
        f"**/v1/hosts/{chat.host_id}/worktrees?*",
        lambda route: route.fulfill(
            json={
                "data": [
                    {
                        "path": "/work/repo",
                        "branch": "live-branch",
                        "is_main": False,
                        "detached": False,
                    }
                ]
            }
        ),
    )
    page.route(
        f"**/v1/sessions/{session_id}/resources/github",
        lambda route: route.fulfill(
            json={
                "object": "session.github.info",
                "available": True,
                "gh_available": True,
                "authenticated": True,
                "repo": {"name_with_owner": "example/repo"},
                "branch": "pr-head-not-checkout",
                "pr": {
                    "number": pr_number,
                    "title": "Acceptance PR",
                    "url": f"https://github.com/example/repo/pull/{pr_number}",
                    "state": "OPEN",
                    "is_draft": False,
                    "checks": {"passing": 0, "failing": 0, "pending": 0, "total": 0, "runs": []},
                },
            }
        ),
    )

    def child_sessions(route: Route) -> None:
        assert parse_qs(urlparse(route.request.url).query) == {
            "zone": ["active"],
            "limit": ["100"],
        }
        route.fulfill(
            json={
                "data": [
                    {
                        "id": "acceptance-child",
                        "busy": True,
                        "title": "Review changes",
                        "task_summary": "Review changes",
                        "tool": "agent",
                        "labels": {},
                    }
                ]
            }
        )

    page.route(
        re.compile(rf"/v1/sessions/{re.escape(session_id)}/child_sessions(?:\?.*)?$"),
        child_sessions,
    )


_BAR_HIT_REPORT = """(bar) => {
  const controls = [...bar.querySelectorAll("button, a")].filter((control) => {
    const box = control.getBoundingClientRect();
    return box.width > 0 && box.height > 0;
  });
  const owner = (x, y) => {
    const hit = document.elementFromPoint(x, y);
    return hit ? (controls.find((control) => control.contains(hit)) ?? null) : null;
  };
  const barBox = bar.getBoundingClientRect();
  return controls.map((control) => {
    const box = control.getBoundingClientRect();
    const midY = box.top + box.height / 2;
    const midX = box.left + box.width / 2;
    let ownedWidth = 0;
    let ownedHeight = 0;
    let stolen = 0;
    for (let x = Math.floor(barBox.left); x < barBox.right; x += 1) {
      const hit = owner(x + 0.5, midY);
      if (hit === control) ownedWidth += 1;
      else if (hit && x >= box.left && x < box.right) stolen += 1;
    }
    for (let y = Math.floor(barBox.top) - 12; y < barBox.bottom + 12; y += 1) {
      if (owner(midX, y + 0.5) === control) ownedHeight += 1;
    }
    const name = control.getAttribute("aria-label") || control.textContent.trim();
    return { name, ownedWidth, ownedHeight, stolen };
  });
}"""


def _assert_phone_bar_hit_boxes(page: Page, bar: Locator) -> None:
    """Every phone bar control owns a 44x36 tap area that no neighbour covers."""
    report = bar.evaluate(_BAR_HIT_REPORT)
    assert len(report) >= 4, report
    for control in report:
        assert control["stolen"] == 0, report
        assert control["ownedWidth"] >= 44, report
        assert control["ownedHeight"] >= 36, report
    # A tap one pixel inside each edge of the ring / approval boundary opens
    # only the control that edge belongs to.
    ring = bar.get_by_test_id("composer-context-ring").bounding_box()
    approval_button = bar.get_by_role("button", name=re.compile(r"^Standing approval:"))
    approval = approval_button.bounding_box()
    assert ring is not None and approval is not None
    page.touchscreen.tap(ring["x"] + ring["width"] - 1, ring["y"] + ring["height"] / 2)
    expect(page.get_by_test_id("composer-context-float")).to_be_visible()
    expect(page.get_by_role("heading", name="Standing approval")).to_have_count(0)
    page.keyboard.press("Escape")
    expect(page.get_by_test_id("composer-context-float")).to_be_hidden()
    page.touchscreen.tap(approval["x"] + 1, approval["y"] + approval["height"] / 2)
    expect(page.get_by_role("heading", name="Standing approval")).to_be_visible()
    expect(page.get_by_test_id("composer-context-float")).to_be_hidden()
    page.keyboard.press("Escape")
    expect(page.get_by_role("heading", name="Standing approval")).to_have_count(0)


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize(
    ("viewport_width", "font_size", "pr_number", "font_family"),
    [
        pytest.param(1280, 13, 1234, None, id="desktop-default"),
        pytest.param(
            390,
            18,
            1234,
            None,
            marks=pytest.mark.browser_context_args(has_touch=True),
            id="mobile-crowded",
        ),
        pytest.param(
            375,
            18,
            1234,
            None,
            marks=pytest.mark.browser_context_args(has_touch=True),
            id="mobile-narrow-crowded",
        ),
        pytest.param(
            375,
            18,
            1234567,
            None,
            marks=pytest.mark.browser_context_args(has_touch=True),
            id="mobile-long-pr",
        ),
        pytest.param(
            375,
            18,
            1234,
            "Verdana",
            marks=pytest.mark.browser_context_args(has_touch=True),
            id="mobile-wide-font",
        ),
    ],
)
def test_pr_context_and_background_tasks_share_workspace_bar(
    page: Page,
    chat_session_contract: ChatSessionContract,
    tmp_path: Path,
    theme: str,
    viewport_width: int,
    font_size: int,
    pr_number: int,
    font_family: str | None,
) -> None:
    chat = chat_session_contract
    session_id = chat.session_id
    page.add_init_script("localStorage.setItem('omnigent:default-workspace-panel', 'open')")
    is_mobile = viewport_width < 768
    _stub_session_bar(page, chat, pr_number)
    page.emulate_media(color_scheme=theme)
    page.add_init_script(f"localStorage.setItem('web-theme', '{theme}')")
    page.add_init_script(f"localStorage.setItem('omnigent:ui-font-size', '{font_size}')")
    if font_family is not None:
        page.add_init_script(
            f"localStorage.setItem('omnigent:ui-font-family', JSON.stringify('{font_family}'))"
        )
    page.set_viewport_size({"width": viewport_width, "height": 844 if is_mobile else 900})
    page.goto(chat.url)
    chat.wait_for_stream()
    bar = page.get_by_test_id("composer-workspace-controls")
    if is_mobile:
        # Phone keeps the session bar above the composer; background tasks stay in the header.
        expect(bar.get_by_test_id("composer-pr-link")).to_have_accessible_name(
            f"#{pr_number}", timeout=30_000
        )
        expect(bar.get_by_test_id("background-task-pill")).to_have_count(0)
        expect(bar.get_by_test_id("subagent-task-pill")).to_be_visible()
        expect(bar.get_by_role("button", name=re.compile(r"^Standing approval:"))).to_be_visible()
        _assert_phone_bar_hit_boxes(page, bar)
        pill = page.get_by_test_id("mobile-header-controls-slot").get_by_test_id(
            "background-task-pill"
        )
        expect(pill).to_have_accessible_name("1 background task still running")
        pill.click()
        expect(page.get_by_text(_MONITOR_TASK["command"], exact=True)).to_be_visible()
        page.keyboard.press("Escape")
        menu_button = page.get_by_role(
            "button", name=re.compile(r"^(?:Conversation|Session) actions$")
        )
        menu_button.click()
        expect(
            page.get_by_role("menu").get_by_label("Working directory: /work/repo", exact=True)
        ).to_have_text("repo")
        page.get_by_role("menuitem", name="Pull Requests", exact=True).click()
        panel = page.get_by_test_id("github-panel-drawer")
        expect(panel).to_have_attribute("data-state", "open")
        expect(panel.get_by_text("Acceptance PR", exact=True)).to_be_visible()
        panel.get_by_role("button", name="Close", exact=True).click()
        expect(panel).to_have_attribute("data-state", "closed")
        expect(menu_button).to_be_in_viewport()
        chat.emit(
            {
                "event": "session.status",
                "data": {
                    "conversation_id": session_id,
                    "status": "idle",
                    "background_task_count": 0,
                },
            }
        )
        expect(pill).to_have_count(0)
        return
    pr_link = bar.get_by_test_id("composer-pr-link")
    pr_label = pr_link.locator("span").last
    expect(pr_label).to_have_text(f"#{pr_number}", timeout=30_000)
    expect(pr_link).to_have_accessible_name(f"#{pr_number}")
    context = bar.get_by_test_id("composer-context-ring")
    expect(context).to_have_accessible_name("100% of context used")
    if font_family is not None:
        applied_family = context.evaluate("el => getComputedStyle(el).fontFamily")
        assert applied_family.split(",")[0].strip("\"' ") == font_family
    expect(bar.get_by_test_id("background-task-pill")).to_have_text("1")
    subagent = bar.get_by_test_id("subagent-task-pill")
    expect(subagent).to_have_text("1")
    expect(subagent).to_have_accessible_name("1 sub-agent: 1 active")
    expect(bar).to_contain_text("live-branch")
    expect(bar).not_to_contain_text("pr-head-not-checkout")
    bounds = bar.bounding_box()
    assert bounds is not None
    status_ids = (
        "composer-pr-link",
        "background-task-pill",
        "subagent-task-pill",
        "composer-context-ring",
    )
    control_bounds = {}
    icon_bounds = {}
    center_hits = {}
    for test_id in ("composer-workspace-dir", "composer-git-branch", *status_ids):
        control = bar.get_by_test_id(test_id)
        rect = control.bounding_box()
        assert rect is not None
        control_bounds[test_id] = rect
        center_hits[test_id] = control.evaluate(
            """element => {
                const rect = element.getBoundingClientRect();
                const hit = document.elementFromPoint(
                    rect.x + rect.width / 2, rect.y + rect.height / 2
                );
                return {ownsCenter: element.contains(hit),
                    target: hit?.closest('[data-testid]')?.getAttribute('data-testid')};
            }"""
        )
        icon_bounds[test_id] = []
        for icon in control.locator("svg").all():
            icon_rect = icon.bounding_box()
            assert icon_rect is not None
            icon_bounds[test_id].append(icon_rect)
    measured_font = context.evaluate("el => parseFloat(getComputedStyle(el).fontSize)")
    touch_target = context.evaluate(
        """element => {
            const rect = element.getBoundingClientRect();
            const after = getComputedStyle(element, '::after');
            return {coarse: matchMedia('(pointer: coarse)').matches,
                width: rect.width - parseFloat(after.left) - parseFloat(after.right),
                height: rect.height - parseFloat(after.top) - parseFloat(after.bottom)};
        }"""
    )
    print(
        f"Status bar ({viewport_width}px, {font_size}px preference, {theme}): "
        f"bar={bounds}, controls={control_bounds}, icons={icon_bounds}, "
        f"font={measured_font}, family={font_family or 'system'}, "
        f"center_hits={center_hits}, ring_target={touch_target}"
    )
    assert all(hit["ownsCenter"] for hit in center_hits.values()), center_hits
    if touch_target["coarse"]:
        assert touch_target["width"] >= 44 and touch_target["height"] >= 44, touch_target
    bar.screenshot(path=tmp_path / f"status-bar-{theme}.png", animations="disabled")
    page.screenshot(path=tmp_path / f"status-page-{theme}.png", animations="disabled")
    directory_icon = icon_bounds["composer-workspace-dir"][0]
    center_y = directory_icon["y"] + directory_icon["height"] / 2
    if not is_mobile:
        assert bounds["height"] == pytest.approx(37, abs=0.5)
        assert center_y == pytest.approx(bounds["y"] + 19, abs=0.5)
    # Touch layouts may wrap the status controls beneath the directory/branch.
    # Keep each row aligned and every control contained and in reading order.
    branch_icon = icon_bounds["composer-git-branch"][0]
    assert branch_icon["y"] + branch_icon["height"] / 2 == pytest.approx(center_y, abs=0.5)
    status_center_y = (
        control_bounds["background-task-pill"]["y"]
        + control_bounds["background-task-pill"]["height"] / 2
    )
    if not is_mobile:
        assert status_center_y == pytest.approx(center_y, abs=0.5)
    trailing = control_bounds[status_ids[-1]]
    # The context bar uses its compact content inset (1px computed border +
    # 8px padding).
    assert trailing["x"] + trailing["width"] == pytest.approx(
        bounds["x"] + bounds["width"] - 9, abs=0.5
    )
    background = control_bounds["background-task-pill"]
    subagent = control_bounds["subagent-task-pill"]
    context_ring = control_bounds["composer-context-ring"]
    assert background["x"] + background["width"] == pytest.approx(subagent["x"], abs=0.5)
    assert subagent["x"] + subagent["width"] + 4 == pytest.approx(context_ring["x"], abs=0.5)
    # Labels collapse when they no longer fit. The PR's full identity remains
    # available in its hover preview, or its title on touch devices.
    collapsed = bar.get_attribute("data-labels") == "collapsed"
    assert collapsed == workspace_bar_needs_collapse(bar), (viewport_width, font_size, pr_number)
    if is_mobile:
        expect(pr_label).to_have_attribute("title", f"#{pr_number}")
    else:
        assert pr_label.get_attribute("title") is None
        pr_link.hover()
        preview = page.get_by_test_id("composer-pr-preview")
        expect(preview.get_by_text(f"example/repo #{pr_number}", exact=True)).to_be_visible()
        page.mouse.move(0, 0, steps=10)
        expect(preview).to_be_hidden()
    # The PR number stays visible and the context ring stays accessible; a
    # crowded bar collapses only the directory and branch text to their icons.
    expect(pr_label).to_be_visible()
    expect(context).to_have_accessible_name("100% of context used")
    for chip in ("composer-workspace-dir", "composer-git-branch"):
        chip_label = bar.get_by_test_id(chip).locator("[data-workspace-collapse-label]")
        if collapsed:
            expect(chip_label).to_be_hidden()
        else:
            expect(chip_label).to_be_visible()
    # From 390px up the number shows in full. A 375px bar with the large font
    # setting is the one place it may still ellipsize even with the directory
    # and branch text gone (CI's fonts run wider than macOS's, so it is
    # font-dependent there); its full value stays in the title.
    if viewport_width >= 390:
        assert pr_label.evaluate("el => el.scrollWidth <= el.clientWidth + 1")
    pr_center_y = (
        control_bounds["composer-pr-link"]["y"] + control_bounds["composer-pr-link"]["height"] / 2
    )
    assert min(abs(pr_center_y - center_y), abs(pr_center_y - status_center_y)) <= 0.5
    for test_id in status_ids[1:]:
        rect = control_bounds[test_id]
        assert rect["y"] + rect["height"] / 2 == pytest.approx(status_center_y, abs=0.5)
    for test_id, rect in control_bounds.items():
        assert rect["x"] >= bounds["x"] - 0.5, (test_id, rect, bounds)
        assert rect["x"] + rect["width"] <= bounds["x"] + bounds["width"] + 0.5, (
            test_id,
            rect,
            bounds,
        )
        assert rect["y"] >= bounds["y"] - 0.5, (test_id, rect, bounds)
        assert rect["y"] + rect["height"] <= bounds["y"] + bounds["height"] + 0.5, (
            test_id,
            rect,
            bounds,
        )
        for icon in icon_bounds[test_id]:
            assert icon["x"] >= rect["x"] - 0.5, (test_id, icon, rect)
            assert icon["x"] + icon["width"] <= rect["x"] + rect["width"] + 0.5, (
                test_id,
                icon,
                rect,
            )
    ordered_icons = [
        (test_id, icon)
        for test_id in (
            "composer-workspace-dir",
            "composer-git-branch",
            "composer-pr-link",
            "background-task-pill",
            "subagent-task-pill",
            "composer-context-ring",
        )
        for icon in icon_bounds[test_id]
    ]
    for (left_id, left), (right_id, right) in pairwise(ordered_icons):
        assert (
            left["x"] + left["width"] <= right["x"] + 0.5
            or left["y"] + left["height"] <= right["y"] + 0.5
        ), (
            left_id,
            left,
            right_id,
            right,
        )
    if is_mobile and (pr_number == 1234567 or font_family is not None):
        pr_link.tap()
        panel = page.get_by_test_id("github-panel-drawer")
        expect(panel).to_have_attribute("data-state", "open")
        expect(panel).to_be_visible()
        panel.screenshot(path=tmp_path / "truncated-pr-open.png", animations="disabled")
        panel.get_by_role("button", name="Close", exact=True).tap()
        expect(panel).to_have_attribute("data-state", "closed")
        expect(pr_link).to_be_in_viewport()
    chat.emit(
        {
            "event": "session.status",
            "data": {
                "conversation_id": session_id,
                "status": "idle",
                "background_task_count": 0,
            },
        }
    )
    expect(bar.get_by_test_id("background-task-pill")).to_have_count(0)


@pytest.mark.parametrize("width", [390, 768, 1440, 3200])
def test_long_model_and_permission_remain_single_row(
    page: Page, chat_session_contract: ChatSessionContract, tmp_path: Path, width: int
) -> None:
    chat = chat_session_contract
    page.add_init_script("localStorage.setItem('omnigent:default-workspace-panel', 'open')")
    model = "system.ai.claude-opus-4-8[1m]"
    display_name = "Opus 4.8 (1M context)"
    chat.set_catalog(
        harness="claude",
        selected_model=model,
        models=[{"id": model, "model": model, "displayName": display_name}],
    )
    chat.update_session(
        labels={
            "omnigent.wrapper": "claude-code-native-ui",
            "omnigent.claude_native.permission_mode": "bypassPermissions",
        }
    )
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(chat.url)
    label = page.get_by_test_id("composer-agent-config-value")
    expect(label).to_contain_text("Opus 4.8 1M", timeout=30_000)
    permission = page.get_by_test_id("composer-permission-chip")
    if width < 768:
        page.get_by_label("Message the agent").focus()
    expect(permission).to_be_visible()
    if width == 3200:
        for text in (
            page.get_by_test_id("composer-agent-model-value"),
            permission.locator("span"),
        ):
            assert text.evaluate("element => element.scrollWidth <= element.clientWidth + 1")
    page.screenshot(path=tmp_path / f"long-model-{width}.png", animations="disabled")
    permission_bounds = permission.bounding_box()
    send_bounds = page.get_by_role("button", name="Send", exact=True).bounding_box()
    assert permission_bounds is not None and send_bounds is not None
    page.get_by_test_id("composer-config-gear").click()
    summary = page.get_by_test_id("composer-agent-model-summary")
    expect(summary).to_contain_text(display_name)
    page.get_by_test_id("composer-agent-menu").screenshot(
        path=tmp_path / f"harness-row-{width}.png", animations="disabled"
    )
    summary_dimensions = summary.evaluate("""element => {
      const range = document.createRange(); range.selectNodeContents(element);
      const rects = [...range.getClientRects()].filter(rect => rect.width > 0);
      return {lines: [...new Set(rects.map(rect => Math.round(rect.top)))],
        width: element.clientWidth, scrollWidth: element.scrollWidth};
    }""")
    results = {
        "toolbar_single_row": abs(permission_bounds["y"] - send_bounds["y"]) < 10,
        "harness_summary_single_line": len(summary_dimensions["lines"]) == 1,
        "send_on_screen": send_bounds["x"] + send_bounds["width"] <= width,
    }
    expect(page.get_by_test_id("composer-agent-model-value")).to_have_attribute(
        "title", "Opus 4.8 1M"
    )
    expect(summary).to_have_attribute("title", display_name)
    page.get_by_test_id("composer-agent-edit").click()
    expect(page.get_by_role("menuitemcheckbox", name=display_name, exact=True)).to_be_visible()
    assert all(results.values()), results


def test_prompt_history_preserves_drafts_during_real_arrow_navigation(
    page: Page, chat_session_contract: ChatSessionContract
) -> None:
    """Native caret movement stops at the draft's top; empty input can recall."""
    chat = chat_session_contract
    history = ["Explain the previous change.", "Summarize the latest change."]
    history_key = f"omnigent:prompt-history:{chat.session_id}"
    page.add_init_script(
        f"localStorage.setItem({json.dumps(history_key)}, {json.dumps(json.dumps(history))})"
    )
    page.set_viewport_size({"width": 1100, "height": 900})
    page.goto(chat.url)
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible()

    draft = "Keep this unfinished thought while I move back through its wrapped lines. " * 8
    composer.fill(draft)
    positions = [composer.evaluate("element => element.selectionStart")]
    for _ in range(30):
        composer.press("ArrowUp")
        expect(composer).to_have_value(draft)
        positions.append(composer.evaluate("element => element.selectionStart"))
        if positions[-1] == 0:
            break
    assert positions[-1] == 0, positions
    assert len(set(positions)) >= 3, "The draft must wrap across multiple visual lines"
    composer.press("ArrowUp")
    expect(composer).to_have_value(draft)
    composer.press("X")
    expect(composer).to_have_value(f"X{draft}")

    composer.fill("")
    composer.press("ArrowUp")
    expect(composer).to_have_value(history[-1])
    assert composer.evaluate("element => element.selectionStart") == len(history[-1])
    # The first Up traverses the recalled line; the next walks older history.
    composer.press("ArrowUp")
    expect(composer).to_have_value(history[-1])
    assert composer.evaluate("element => element.selectionStart") == 0
    composer.press("ArrowUp")
    expect(composer).to_have_value(history[0])
    composer.press("ArrowDown")
    expect(composer).to_have_value(history[-1])
    composer.press("ArrowDown")
    expect(composer).to_have_value("")

    composer.press("ArrowUp")
    expect(composer).to_have_value(history[-1])
    composer.press("!")
    edited = f"{history[-1]}!"
    expect(composer).to_have_value(edited)
    composer.press("ArrowDown")
    expect(composer).to_have_value(edited)
    composer.press("ArrowUp")
    assert composer.evaluate("element => element.selectionStart") == 0
    composer.press("ArrowUp")
    expect(composer).to_have_value(edited)
    assert chat.event_posts == []


_ELIGIBLE_ROTATION = {
    "eligible": True,
    "ineligible_reason": None,
    "harness": "claude-native",
    "enabled": True,
    "compactions_setting": 4,
    "remaining_pct_setting": 25,
    "sources": {"enabled": "session", "compactions": "default", "remaining_pct": "default"},
    "compactions": 2,
    "remaining_pct": 40,
    "approximate": [],
    "due": False,
    "rotation_pending": False,
}

_SCROLLER_REPORT = """(scroller) => {
  const view = scroller.getBoundingClientRect();
  return [...scroller.querySelectorAll("button, a")]
    .filter((control) => control.getBoundingClientRect().width > 0)
    .map((control) => {
      const box = control.getBoundingClientRect();
      return {
        name: control.getAttribute("aria-label") || control.textContent.trim(),
        left: box.left - view.left + scroller.scrollLeft,
        right: box.right - view.left + scroller.scrollLeft,
        width: box.width,
        height: box.height,
      };
    });
}"""

_REVEAL_AND_PROBE = """(scroller, index) => {
  const controls = [...scroller.querySelectorAll("button, a")].filter(
    (control) => control.getBoundingClientRect().width > 0,
  );
  const control = controls[index];
  control.scrollIntoView({ block: "nearest", inline: "nearest" });
  const view = scroller.getBoundingClientRect();
  const box = control.getBoundingClientRect();
  const owns = (x) => {
    const hit = document.elementFromPoint(x, box.top + box.height / 2);
    return hit !== null && control.contains(hit);
  };
  return {
    name: control.getAttribute("aria-label") || control.textContent.trim(),
    inside: box.left >= view.left - 0.5 && box.right <= view.right + 0.5,
    ownsEdges: owns(box.left + 1) && owns(box.right - 1),
  };
}"""


def _open_full_phone_bar(page: Page, chat: ChatSessionContract) -> tuple[Locator, Locator]:
    """A 360px phone session with every session-bar control present; returns bar and scroller."""
    _stub_session_bar(page, chat, 1234)
    chat.update_session(
        labels={
            "omnigent.runner_log_runaway": "2026-09-23T09:25:00+00:00",
            "omnigent.runner_log_runaway_mb": "5",
            "omnigent.runner_log_runaway_seen": datetime.now(UTC).isoformat(timespec="seconds"),
        }
    )
    chat.contract.json(f"/v1/sessions/{chat.session_id}/auto-rotation", _ELIGIBLE_ROTATION)
    page.set_viewport_size({"width": 360, "height": 780})
    page.goto(chat.url)
    chat.wait_for_stream()
    page.get_by_test_id("runner-log-runaway-dismiss").click()

    bar = page.get_by_test_id("composer-workspace-controls")
    scroller = bar.get_by_test_id("composer-workspace-scroller")
    for test_id in (
        "composer-pr-link",
        "subagent-task-pill",
        "runner-log-runaway-indicator",
        "composer-context-ring",
        "auto-rotation-chip-trigger",
    ):
        expect(bar.get_by_test_id(test_id)).to_be_attached(timeout=30_000)
    expect(bar.get_by_role("button", name=re.compile(r"^Standing approval:"))).to_be_attached()
    expect(bar).to_have_attribute("data-labels", "collapsed")
    return bar, scroller


@pytest.mark.browser_context_args(has_touch=True, is_mobile=True)
def test_phone_bar_keeps_every_control_reachable_at_360(
    page: Page, chat_session_contract: ChatSessionContract
) -> None:
    """At 360px every session-bar control keeps its own 44x36 box in one scrolling row."""
    bar, scroller = _open_full_phone_bar(page, chat_session_contract)

    # One 40px row that scrolls: the far edge fades, the near edge does not.
    assert bar.bounding_box()["height"] == pytest.approx(40, abs=1)
    assert scroller.evaluate("(el) => el.scrollWidth > el.clientWidth")
    expect(scroller).to_have_attribute("data-hidden-end", "true")
    expect(scroller).not_to_have_attribute("data-hidden-start", "true")

    controls = scroller.evaluate(_SCROLLER_REPORT)
    assert len(controls) >= 6, json.dumps(controls)
    for control in controls:
        assert control["width"] >= 44 and control["height"] >= 36, controls
    for first, second in pairwise(sorted(controls, key=lambda control: control["left"])):
        assert first["right"] <= second["left"] + 0.5, controls

    for index in range(len(controls)):
        probe = scroller.evaluate(_REVEAL_AND_PROBE, index)
        assert probe["inside"] and probe["ownsEdges"], probe
    expect(scroller).to_have_attribute("data-hidden-start", "true")
    expect(scroller).not_to_have_attribute("data-hidden-end", "true")

    # The last control, revealed by scrolling, opens its own popover from a touch.
    rotation = bar.get_by_test_id("auto-rotation-chip-trigger").bounding_box()
    assert rotation is not None
    page.touchscreen.tap(
        rotation["x"] + rotation["width"] - 2, rotation["y"] + rotation["height"] / 2
    )
    expect(page.get_by_text("Auto-rotation", exact=True).first).to_be_visible()


_FOCUS_SETTLED = """(scroller) =>
  (scroller.scrollLeft > 1) === scroller.hasAttribute("data-hidden-start") &&
  (scroller.scrollLeft + scroller.clientWidth < scroller.scrollWidth - 1) ===
    scroller.hasAttribute("data-hidden-end")"""

_FOCUS_RING_REPORT = """(scroller) => {
  const controls = [...scroller.querySelectorAll("button, a")].filter(
    (control) => control.getBoundingClientRect().width > 0,
  );
  const control = document.activeElement;
  // Painted ring: the border box plus any outer box-shadow or outline.
  const style = getComputedStyle(control);
  const shadows = style.boxShadow === "none" ? [] : style.boxShadow.split(/,(?![^(]*\\))/);
  let painted = false;
  let outset = 0;
  for (const shadow of shadows) {
    const [x = 0, y = 0, blur = 0, spread = 0] = (shadow.match(/-?[\\d.]+px/g) ?? []).map(
      parseFloat,
    );
    if (blur === 0 && spread === 0) continue;
    painted = true;
    if (!/\\binset\\b/.test(shadow)) {
      outset = Math.max(outset, spread + blur + Math.max(Math.abs(x), Math.abs(y)));
    }
  }
  if (style.outlineStyle !== "none" && parseFloat(style.outlineWidth) > 0) {
    painted = true;
    outset = Math.max(outset, parseFloat(style.outlineWidth) + parseFloat(style.outlineOffset));
  }
  const box = control.getBoundingClientRect();
  const ring = {
    left: box.left - outset,
    right: box.right + outset,
    top: box.top - outset,
    bottom: box.bottom + outset,
  };
  // Visible area: every clipping ancestor, minus the scroller's faded edges.
  const clip = { left: 0, right: innerWidth, top: 0, bottom: innerHeight };
  for (let node = control.parentElement; node !== document.body; node = node.parentElement) {
    const nodeStyle = getComputedStyle(node);
    const rect = node.getBoundingClientRect();
    const fade = (side) =>
      node === scroller ? parseFloat(nodeStyle.getPropertyValue(`--bar-fade-${side}`)) : 0;
    if (nodeStyle.overflowX !== "visible") {
      clip.left = Math.max(clip.left, rect.left + node.clientLeft + fade("start"));
      clip.right = Math.min(
        clip.right,
        rect.left + node.clientLeft + node.clientWidth - fade("end"),
      );
    }
    if (nodeStyle.overflowY !== "visible") {
      clip.top = Math.max(clip.top, rect.top + node.clientTop);
      clip.bottom = Math.min(clip.bottom, rect.top + node.clientTop + node.clientHeight);
    }
  }
  return {
    index: controls.indexOf(control),
    name: control.getAttribute("aria-label") || control.textContent.trim(),
    focusVisible: control.matches(":focus-visible"),
    painted,
    visible:
      ring.left >= clip.left - 0.5 &&
      ring.right <= clip.right + 0.5 &&
      ring.top >= clip.top - 0.5 &&
      ring.bottom <= clip.bottom + 0.5,
    ring,
    clip,
    boxShadow: style.boxShadow,
  };
}"""


@pytest.mark.browser_context_args(has_touch=True, is_mobile=True)
def test_phone_bar_focus_rings_stay_visible_at_360(
    page: Page, chat_session_contract: ChatSessionContract
) -> None:
    """Tabbing along the scrolling 360px bar shows each control's whole focus ring."""
    _, scroller = _open_full_phone_bar(page, chat_session_contract)
    count = len(scroller.evaluate(_SCROLLER_REPORT))
    scroller.evaluate(
        """(scroller) => [...scroller.querySelectorAll("button, a")]
          .find((control) => control.getBoundingClientRect().width > 0)
          .focus()"""
    )
    # Arrive by keyboard so the browser shows its focus-visible ring.
    page.keyboard.press("Shift+Tab")
    page.keyboard.press("Tab")

    visited = []
    for _ in range(count + 1):
        page.wait_for_function(_FOCUS_SETTLED, arg=scroller.element_handle())
        report = scroller.evaluate(_FOCUS_RING_REPORT)
        if report["index"] < 0:
            break
        assert report["focusVisible"] and report["painted"] and report["visible"], report
        visited.append(report["index"])
        page.keyboard.press("Tab")
    # Tab reached the first, at least one middle and the last control, in order.
    assert visited[0] == 0 and visited[-1] == count - 1 and len(visited) >= 3, visited
    assert visited == sorted(set(visited)), visited
