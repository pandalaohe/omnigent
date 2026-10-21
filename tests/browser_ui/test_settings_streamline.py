"""Browser contracts for consolidated settings and safe per-source edits."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from playwright.sync_api import Page, Request, expect

from tests.browser_ui.conftest import BrowserContract
from tests.browser_ui.test_sessions_appearance import appearance_url as appearance_url


@pytest.mark.parametrize("width", [1440, 390])
def test_child_freeze_default_and_exception_stay_compact(
    page: Page,
    appearance_url: str,
    browser_contract: BrowserContract,
    width: int,
    request: pytest.FixtureRequest,
) -> None:
    agents = [
        {
            "id": "claude-product",
            "name": "claude-native-ui",
            "harness": "claude-native",
            "builtin": True,
            "product_of": "claude-native",
            "integration_mode": "native-tui",
        },
        *[
            {
                "id": f"agent-{index}",
                "name": f"agent-{index}",
                "harness": f"harness-{index}",
                "builtin": True,
                "product_of": f"harness-{index}",
                "integration_mode": "native-tui",
            }
            for index in range(18)
        ],
    ]
    harnesses = [
        {
            "id": row["harness"],
            "label": row["name"],
            "capabilities": {"integration_mode": "native-tui"},
        }
        for row in agents
    ]
    capabilities = {row["harness"]: {"integration_mode": "native-tui"} for row in agents}
    browser_contract.json("/v1/agents", {"data": agents, "has_more": False})
    browser_contract.json(
        "/v1/harnesses",
        {"data": harnesses, "capabilities_by_harness": capabilities},
    )
    browser_contract.json(
        "/v1/hosts",
        {
            "hosts": [
                {
                    "host_id": "example-host",
                    "name": "Example host",
                    "owner": "local",
                    "status": "offline",
                    "configured_harnesses": {row["harness"]: True for row in agents},
                }
            ]
        },
    )
    saved = {"enabled": False, "agents": {}}

    def save(request: Request) -> dict[str, bool]:
        saved.update(request.post_data_json["value"])
        return {"ok": True}

    browser_contract.json(
        "/v1/me",
        lambda _: {
            "user_id": "local",
            "is_admin": True,
            "preferences": {"version": 1, "settings": {"child_idle": saved}},
        },
    )
    browser_contract.json(
        "/v1/me/preferences",
        lambda _: {
            "user_id": "local",
            "preferences": {"version": 1, "settings": {"child_idle": saved}},
        },
    )
    browser_contract.json("/v1/me/preferences/child_idle", save, method="PATCH")
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{appearance_url}/settings/general#child-idle")
    section = page.get_by_test_id("child-idle-settings")
    expect(section.get_by_text("All agents", exact=True)).to_be_visible()
    expect(section.get_by_role("switch", name="Monitor all agents children")).to_have_count(1)
    expect(section.get_by_role("switch", name=re.compile(r"Monitor .* children"))).to_have_count(1)
    expect(section.get_by_role("button", name="Add exception")).to_have_count(1)
    expect(section.get_by_role("button", name="Edit Claude Code")).to_have_count(0)
    with page.expect_response(
        lambda response: (
            response.request.method == "PATCH"
            and response.url.endswith("/v1/me/preferences/child_idle")
        )
    ):
        section.get_by_role("switch", name="Monitor all agents children").click()
        assert (
            page.evaluate(
                "JSON.parse(localStorage.getItem('omnigent:child-idle')).freezeDefault.enabled"
            )
            is True
        )
    section.get_by_role("button", name="Add exception").click()
    picker = page.get_by_role("dialog", name="Add exception")
    expect(picker.get_by_role("textbox", name="Search agents")).to_be_focused()
    picker.get_by_role("textbox", name="Search agents").fill("Claude")
    expect(
        picker.get_by_role("button", name=re.compile("Claude Code.*Native CLI"))
    ).to_be_visible()
    assert picker.evaluate("element => element.scrollWidth <= element.clientWidth")
    output = Path(request.config.getoption("--output"))
    output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output / f"child-freeze-picker-{width}.png"), full_page=True)
    with page.expect_response(
        lambda response: (
            response.request.method == "PATCH"
            and response.url.endswith("/v1/me/preferences/child_idle")
        )
    ):
        picker.get_by_role("button", name=re.compile("Claude Code.*Native CLI")).click()
        local_preferences = page.evaluate(
            "JSON.parse(localStorage.getItem('omnigent:child-idle'))"
        )
        assert "harness:claude-native" in local_preferences["agents"]
    expect(section.get_by_role("button", name="Edit Claude Code")).to_be_visible()
    assert saved["freezeDefault"]["enabled"] is True
    assert "harness:claude-native" in saved["agents"]
    page.reload()
    expect(section.get_by_role("switch", name="Monitor all agents children")).to_be_checked()
    expect(section.get_by_role("button", name="Edit Claude Code")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


@pytest.mark.parametrize("width", [1440, 390])
def test_quota_default_and_registry_product_exception_persist(
    page: Page,
    appearance_url: str,
    browser_contract: BrowserContract,
    width: int,
    request: pytest.FixtureRequest,
) -> None:
    agents = [
        {
            "id": f"{harness}-agent",
            "name": "codex-native-ui" if harness == "codex-native" else "kite",
            "harness": harness,
            "builtin": True,
            "product_of": harness,
            "integration_mode": "native-tui",
        }
        for harness in ("codex-native", "kite-native")
    ]
    browser_contract.json("/v1/agents", {"data": agents, "has_more": False})
    browser_contract.json(
        "/v1/instruction-layers",
        {
            "data": [],
            "agents": [],
            "order": [],
            "route_sizes": [],
            "size_reminder_chars": 20000,
        },
    )
    browser_contract.json(
        "/v1/harnesses",
        {
            "data": [],
            "capabilities_by_harness": {
                "codex-native": {"integration_mode": "native-tui", "native_agent_key": "codex"},
                "kite-native": {"integration_mode": "native-tui", "native_agent_key": "kite"},
            },
        },
    )
    browser_contract.json(
        "/v1/hosts",
        {
            "hosts": [
                {
                    "host_id": "example-host",
                    "name": "Example host",
                    "owner": "local",
                    "status": "online",
                    "configured_harnesses": {"codex-native": True, "kite-native": True},
                }
            ]
        },
    )
    saved = {"quotaPeriods": {"codex": "7d"}}

    def save(incoming: Request) -> dict[str, bool]:
        saved.update(incoming.post_data_json["value"])
        return {"ok": True}

    browser_contract.json(
        "/v1/me",
        lambda _: {
            "user_id": "local",
            "is_admin": True,
            "preferences": {"version": 1, "settings": {"session_collab": saved}},
        },
    )
    browser_contract.json(
        "/v1/me/preferences",
        lambda _: {
            "user_id": "local",
            "preferences": {"version": 1, "settings": {"session_collab": saved}},
        },
    )
    browser_contract.json("/v1/me/preferences/session_collab", save, method="PATCH")
    page.add_init_script(
        "if (!localStorage.getItem('omnigent:session-collab')) "
        f"localStorage.setItem('omnigent:session-collab', {json.dumps(json.dumps(saved))})"
    )
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{appearance_url}/settings/instructions")
    section = page.locator('section[aria-labelledby="child-sessions-heading"]')
    expect(section.get_by_role("textbox", name="All products")).to_have_value("")
    expect(section.get_by_role("button", name="Edit Codex")).to_be_visible()
    section.get_by_role("textbox", name="All products").fill("5h")
    section.get_by_role("button", name="Add exception").click()
    picker = page.get_by_role("dialog", name="Add exception")
    expect(picker.get_by_role("textbox", name="Search agents")).to_be_focused()
    picker.get_by_role("textbox", name="Search agents").fill("kite")
    expect(picker.get_by_role("button", name=re.compile("Kite.*Native CLI"))).to_be_visible()
    assert picker.evaluate("element => element.scrollWidth <= element.clientWidth")
    picker.get_by_role("button", name=re.compile("Kite.*Native CLI")).click()
    section.get_by_role("textbox", name="Kite period").fill("month")
    with page.expect_response(
        lambda response: (
            response.request.method == "PATCH"
            and response.url.endswith("/v1/me/preferences/session_collab")
        )
    ):
        section.get_by_role("button", name="Save child settings").click()
    assert saved["quotaPeriodDefault"] == "5h"
    assert saved["quotaPeriods"] == {"codex": "7d", "kite": "month"}
    page.reload()
    expect(section.get_by_role("textbox", name="All products")).to_have_value("5h")
    expect(section.get_by_role("button", name="Edit Kite")).to_be_visible()
    expect(section.get_by_role("button", name="Edit Codex")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    output = Path(request.config.getoption("--output"))
    output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output / f"quota-periods-{width}.png"), full_page=True)


@pytest.mark.parametrize("width", [1440, 390])
def test_reserve_width_persists_and_settings_fit(
    page: Page, appearance_url: str, width: int, request: pytest.FixtureRequest
) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{appearance_url}/settings/appearance")
    expect(page.get_by_role("heading", name="Appearance", exact=True)).to_be_visible()
    reserve = page.get_by_role("spinbutton", name="Chat reserve width", exact=True)
    expect(reserve).to_have_value("600")
    reserve.fill("800")
    reserve.blur()
    expect(reserve).to_have_value("800")
    page.reload()
    expect(reserve).to_have_value("800")
    reserve.fill("1400")
    reserve.blur()
    expect(reserve).to_have_value("800")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    output = Path(request.config.getoption("--output"))
    output.mkdir(parents=True, exist_ok=True)
    reserve.evaluate("element => element.scrollIntoView({block: 'center'})")
    page.screenshot(path=str(output / f"appearance-{width}.png"), full_page=True)
    page.get_by_test_id("reset-appearance-button").click()
    page.get_by_test_id("reset-appearance-confirm").click()
    expect(reserve).to_have_value("600")


@pytest.mark.parametrize(
    ("old", "destination"),
    [
        ("keep-warm", "/settings/general#keep-warm"),
        ("context-usage", "/settings/general#context-usage"),
        ("runtime-resources", "/settings/general#runtime-resources"),
        ("calling-defaults", "/settings/harnesses#calling-defaults"),
        ("agents", "/settings/custom-agents"),
    ],
)
def test_legacy_links_open_the_new_group(
    page: Page, appearance_url: str, browser_contract: BrowserContract, old: str, destination: str
) -> None:
    browser_contract.json("/v1/calling-defaults/catalogs", {"rows": []})
    page.goto(f"{appearance_url}/settings/{old}")
    expect(page).to_have_url(f"{appearance_url}{destination}")
    if "#" in destination:
        anchor = destination.split("#", 1)[1]
        expect(page.locator(f"#{anchor}")).to_be_visible()
        if anchor != "calling-defaults":
            assert page.locator(f"#{anchor}").evaluate("element => element.open")
    expect(page.get_by_test_id(f"settings-nav-{old}")).to_have_count(0)


@pytest.mark.parametrize("width", [1440, 390])
def test_keep_warm_default_and_exception_persist_with_support_reasons(
    page: Page, appearance_url: str, browser_contract: BrowserContract, width: int
) -> None:
    agents = [
        {
            "id": "claude-agent",
            "name": "claude-native-ui",
            "harness": "claude-native",
            "builtin": True,
            "product_of": "claude-native",
            "integration_mode": "native-tui",
        },
        {
            "id": "codex-agent",
            "name": "codex-native-ui",
            "harness": "codex-native",
            "builtin": True,
            "product_of": "codex-native",
            "integration_mode": "native-tui",
        },
        {"id": "other-agent", "name": "other", "harness": "openai-agents", "builtin": False},
    ]
    browser_contract.json("/v1/agents", {"data": agents, "has_more": False})
    browser_contract.json(
        "/v1/harnesses",
        {
            "data": [],
            "capabilities_by_harness": {
                "claude-native": {"integration_mode": "native-tui", "keep_warm_family": "claude"},
                "codex-native": {"integration_mode": "native-tui", "keep_warm_family": "codex"},
                "openai-agents": {"integration_mode": "sdk-in-process", "keep_warm_family": None},
            },
        },
    )
    preferences = {
        "default": {"main": False, "child": False, "maxSeconds": 14400},
        "agents": {
            "claude-agent": {
                "main": True,
                "child": False,
                "intervalSeconds": 3300,
                "maxSeconds": 7200,
            },
        },
        "hostOfflineArchiveSeconds": 0,
    }

    def save(request: Request) -> dict[str, bool]:
        preferences.update(request.post_data_json["value"])
        return {"ok": True}

    browser_contract.json(
        "/v1/me",
        lambda _: {
            "user_id": "local",
            "is_admin": True,
            "preferences": {"version": 1, "settings": {"keep_warm": preferences}},
        },
    )
    browser_contract.json(
        "/v1/me/preferences",
        lambda _: {
            "user_id": "local",
            "preferences": {"version": 1, "settings": {"keep_warm": preferences}},
        },
    )
    browser_contract.json("/v1/me/preferences/keep_warm", save, method="PATCH")
    browser_contract.response("/v1/me/preferences/session_collab", method="PATCH")
    page.add_init_script(
        f"localStorage.setItem('omnigent:keep-warm', {json.dumps(json.dumps(preferences))})"
    )
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{appearance_url}/settings/keep-warm")
    section = page.get_by_test_id("keep-warm-settings")
    expect(section.get_by_text("All agents", exact=True)).to_be_visible()
    expect(section.get_by_role("switch")).to_have_count(2)
    expect(section.get_by_text("Claude Code · direct sessions only", exact=False)).to_be_visible()
    section.get_by_role("switch", name="Keep main sessions warm for all agents").click()
    section.get_by_role("button", name="Add exception").click()
    picker = page.get_by_role("dialog", name="Add exception")
    picker.get_by_role("button", name="Not supported here (1)").click()
    expect(
        picker.get_by_role("button", name=re.compile("Other.*no declared keep-warm channel"))
    ).to_be_disabled()
    assert picker.evaluate("element => element.scrollWidth <= element.clientWidth")
    picker.get_by_role("button", name=re.compile("Codex.*Native CLI")).click()
    section.get_by_role("switch", name="Keep children warm for Codex").click()
    with page.expect_response(
        lambda response: (
            response.request.method == "PATCH"
            and response.url.endswith("/v1/me/preferences/keep_warm")
            and response.request.post_data_json["value"]["agents"]
            .get("harness:codex-native", {})
            .get("intervalSeconds")
            == 600
        )
    ):
        section.get_by_role("spinbutton", name="Keep-warm interval for Codex in minutes").fill(
            "10"
        )
    saved = page.evaluate("JSON.parse(localStorage.getItem('omnigent:keep-warm'))")
    assert saved["default"] == {"main": True, "child": False, "maxSeconds": 14400}
    assert saved["agents"]["harness:codex-native"]["intervalSeconds"] == 600
    assert saved["agents"]["harness:codex-native"]["child"] is True
    assert saved["agents"]["claude-agent"]["intervalSeconds"] == 3300
    assert "other-agent" not in saved["agents"]
    assert saved["hostOfflineArchiveSeconds"] == 0
    assert preferences["agents"]["harness:codex-native"]["intervalSeconds"] == 600
    page.reload()
    expect(
        section.get_by_role("switch", name="Keep main sessions warm for all agents")
    ).to_be_checked()
    expect(section.get_by_role("button", name="Edit Codex")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


@pytest.mark.parametrize("width", [1440, 390])
def test_project_inheritance_cancel_and_copy_preview_fit(
    page: Page,
    appearance_url: str,
    browser_contract: BrowserContract,
    width: int,
    request: pytest.FixtureRequest,
) -> None:
    host = {
        "host_id": "host-a",
        "name": "Example host",
        "owner": "local",
        "status": "online",
        "configured_harnesses": {"codex-native": True},
    }
    source = {
        "id": "source",
        "name": "Example",
        "config_revision": "a" * 64,
        "config": {
            "host_id": "host-a",
            "calling_defaults": {
                "host-a": {
                    "agent_id": "codex-agent",
                    "harnesses": {"codex-native": {"model": "project-model"}},
                }
            },
        },
    }
    target = {"id": "target", "name": "Other", "config_revision": "b" * 64, "config": {}}
    absent = {"id": "absent", "name": "Without folder", "config_revision": "c" * 64, "config": {}}
    models = [
        {"id": "project-model", "displayName": "Project model"},
        {"id": "master-model", "displayName": "Master model", "isDefault": True},
    ]
    master = {
        "host-a": {
            "codex-native": {
                "model": "master-model",
                "effort": "xhigh",
                "speed": "standard",
                "permission": "read-only",
            }
        }
    }
    browser_contract.json("/v1/hosts", {"hosts": [host]})
    browser_contract.json("/v1/skills", {"skills": []})
    browser_contract.json("/v1/hosts/host-a/mcp-servers", {"servers": []})
    browser_contract.json(
        "/v1/hosts/host-a/filesystem", {"error": {"message": "No fixture filesystem"}}, status=404
    )
    browser_contract.json(
        "/v1/hosts/host-a/folder-facts",
        {"error": {"message": "No fixture folder facts"}},
        status=404,
    )
    browser_contract.json(
        "/v1/projects/source/hosts/host-a/agent-code-note",
        {"error": {"message": "No fixture code locations"}},
        status=404,
    )
    browser_contract.json(
        "/v1/agents",
        {
            "data": [{"id": "codex-agent", "name": "codex", "harness": "codex-native"}],
            "has_more": False,
        },
    )
    browser_contract.json("/v1/projects", {"object": "list", "data": [source, target, absent]})
    browser_contract.json("/v1/sessions/projects", [source, target, absent])
    browser_contract.json(
        "/v1/calling-defaults/catalogs",
        {
            "rows": [
                {
                    "host_id": "host-a",
                    "harness": "codex-native",
                    "models": models,
                    "stale": False,
                    "error": None,
                }
            ]
        },
    )
    browser_contract.json(
        "/v1/calling-defaults/sync",
        {
            "rows": [
                {
                    "host_id": "host-a",
                    "harness": "codex-native",
                    "models": models,
                    "stale": False,
                    "error": None,
                }
            ]
        },
        method="POST",
    )
    browser_contract.json(
        "/v1/calling-defaults/resolve",
        {
            "agent_id": "codex-agent",
            "harness": "codex-native",
            "model": "master-model",
            "effort": None,
            "sources": {},
            "problems": [],
        },
    )
    browser_contract.json(
        re.compile(r"/v1/hosts/host-a/harnesses/[^/]+/model-options(?:\?.*)?$"), {"models": models}
    )
    browser_contract.json(
        "/v1/me/preferences",
        {
            "user_id": "local",
            "preferences": {"version": 1, "settings": {"calling_defaults": master}},
        },
    )
    for project in (source, target, absent):
        project_id = project["id"]
        entries = (
            []
            if project_id == "absent"
            else [
                {
                    "id": f"entry-{project_id}",
                    "project_id": project_id,
                    "host_id": "host-a",
                    "workspace": "/opt/work/example",
                    "created_at": 0,
                    "updated_at": None,
                }
            ]
        )
        browser_contract.json(f"/v1/projects/{project_id}", project)
        browser_contract.json(f"/v1/projects/{project_id}/entries", {"entries": entries})
        browser_contract.json(f"/v1/projects/{project_id}/host-roots", {"roots": []})
        browser_contract.json(
            f"/v1/projects/{project_id}/collaboration",
            {"repositories": [], "bindings": [], "problems": []},
        )
    page.add_init_script(
        f"localStorage.setItem('omnigent:calling-defaults', {json.dumps(json.dumps(master))})"
    )
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(appearance_url)
    actions = page.get_by_role("button", name="Project actions for Example", exact=True)
    if not actions.is_visible() and page.get_by_role("button", name="Open sidebar").is_visible():
        page.get_by_role("button", name="Open sidebar").click()

    def open_project() -> None:
        expect(actions).to_be_visible()
        actions.click()
        page.get_by_test_id("project-settings").click()
        expect(page.get_by_role("dialog", name="Project settings", exact=True)).to_be_visible()
        edit = page.get_by_role("button", name="Edit Example host", exact=True)
        expect(edit).to_be_visible()
        if not page.get_by_test_id("project-settings-host-detail-host-a").is_visible():
            edit.click()

    open_project()
    model = page.get_by_test_id("project-settings-host-model-host-a")
    expect(model).to_have_text("Project model")
    page.get_by_test_id("project-settings-host-reset-model-host-a").click()
    expect(model).to_have_text("Master model")
    for field, label in [("effort", "xHigh"), ("speed", "Standard"), ("permission", "Read Only")]:
        expect(page.get_by_test_id(f"project-settings-host-{field}-host-a")).to_have_text(label)
    model.click()
    expect(page.get_by_role("option", name="Default", exact=True)).to_have_attribute(
        "aria-selected", "true"
    )
    explicit = page.get_by_role("option", name="Master model", exact=True)
    expect(explicit).to_have_attribute("aria-selected", "false")
    explicit.click()
    expect(model).to_have_text("Master model")
    expect(page.get_by_test_id("project-settings-host-reset-model-host-a")).to_be_enabled()
    page.get_by_test_id("project-settings-host-reset-model-host-a").click()
    output = Path(request.config.getoption("--output"))
    output.mkdir(parents=True, exist_ok=True)
    detail = page.get_by_test_id("project-settings-host-detail-host-a")
    expect(detail.get_by_text("Default agent", exact=True)).to_be_visible()
    more = page.get_by_test_id("project-settings-host-more-host-a-codex-native")
    more.scroll_into_view_if_needed()
    more.click()
    copy = page.get_by_test_id("project-settings-host-copy-host-a-codex-native")
    expect(copy).to_be_disabled()
    expect(copy).to_have_text("Apply to other projects")
    expect(page.get_by_role("menuitem", name="Set as agent defaults", exact=True)).to_be_visible()
    assert detail.evaluate("element => element.scrollWidth <= element.clientWidth")
    menu = page.get_by_role("menu")
    assert menu.evaluate("element => element.scrollWidth <= element.clientWidth")
    page.keyboard.press("Escape")
    expect(menu).not_to_be_visible()
    expect(page.get_by_role("dialog", name="Project settings", exact=True)).to_be_visible()
    expect(model).to_have_text("Master model")
    if width == 390:
        page.set_viewport_size({"width": width, "height": 1200})
    detail.scroll_into_view_if_needed()
    for field in ("model", "effort", "speed", "permission"):
        expect(page.get_by_test_id(f"project-settings-host-{field}-host-a")).to_be_in_viewport()
    page.screenshot(path=str(output / f"project-values-only-{width}.png"), full_page=True)
    page.set_viewport_size({"width": width, "height": 900})
    expect(page.get_by_test_id("project-settings-host-reset-model-host-a")).to_be_disabled()
    page.get_by_role("button", name="Cancel", exact=True).click()
    open_project()
    expect(model).to_have_text("Project model")
    more.click()
    page.get_by_role("menuitem", name="Set as agent defaults", exact=True).click()
    defaults_panel = page.get_by_test_id("project-settings-master-panel")
    expect(defaults_panel).to_contain_text("Example host")
    expect(defaults_panel).to_contain_text("every project on this host")
    expect(defaults_panel).to_contain_text("projects with their own changes keep them")
    expect(defaults_panel).to_contain_text("default agent is not changed")
    defaults_panel.get_by_role("button", name="Cancel", exact=True).click()
    more.click()
    expect(copy).to_be_enabled()
    if width == 390:
        page.set_viewport_size({"width": width, "height": 1200})
    expect(detail.get_by_text("Default agent", exact=True)).to_be_in_viewport()
    page.screenshot(path=str(output / f"project-host-row-{width}.png"), full_page=True)
    page.set_viewport_size({"width": width, "height": 900})
    copy.click()
    panel = page.get_by_test_id("project-settings-copy-panel")
    expect(panel).to_be_visible()
    for field in ("model", "effort", "speed", "permission"):
        expect(page.get_by_test_id(f"project-settings-copy-field-{field}")).to_be_checked()
    expect(page.get_by_test_id("project-settings-copy-field-agent")).not_to_be_checked()
    expect(page.get_by_test_id("project-settings-copy-target-target")).to_be_checked()
    expect(page.get_by_test_id("project-settings-copy-target-absent")).to_be_checked()
    expect(panel).to_contain_text("Skip: no entry folder on this host")
    dialog = page.get_by_role("dialog", name="Project settings", exact=True)
    assert dialog.evaluate("element => element.scrollWidth <= element.clientWidth")
    panel.evaluate("element => element.scrollIntoView({block: 'center'})")
    page.screenshot(path=str(output / f"project-copy-{width}.png"), full_page=True)
    page.keyboard.press("Escape")
    expect(dialog).not_to_be_visible()


def test_manual_compact_buffer_modes_preserve_values_and_validate_both_edits(
    page: Page,
    appearance_url: str,
    browser_contract: BrowserContract,
    request: pytest.FixtureRequest,
) -> None:
    storage_key = "omnigent:usage-context-preferences"
    source_key = '["host-friendly","polly","pi","model-a"]'
    legacy = {
        "version": 5,
        "showProviderUsageLimits": True,
        "overrides": {
            source_key: {"contextWindowTokens": 100000, "autoCompactBufferTokens": 20000}
        },
        "lastProviderUsageLimits": {},
    }
    synced = {"value": legacy}

    def save_preferences(incoming: Request) -> dict[str, bool]:
        synced["value"] = incoming.post_data_json["value"]
        return {"ok": True}

    browser_contract.json(
        "/v1/me",
        lambda _: {
            "user_id": "local",
            "is_admin": True,
            "preferences": {"version": 1, "settings": {"usage_context": synced["value"]}},
        },
    )
    browser_contract.json(
        "/v1/me/preferences",
        lambda _: {
            "user_id": "local",
            "preferences": {"version": 1, "settings": {"usage_context": synced["value"]}},
        },
    )
    browser_contract.json("/v1/me/preferences/usage_context", save_preferences, method="PATCH")
    browser_contract.response("/v1/me/preferences/session_collab", method="PATCH")
    browser_contract.json("/v1/hosts/host-friendly/mcp-servers", {"servers": []})
    browser_contract.json(
        "/v1/hosts",
        {
            "hosts": [
                {
                    "host_id": "host-friendly",
                    "name": "Example host",
                    "status": "online",
                    "configured_harnesses": {"pi": True},
                }
            ]
        },
    )
    browser_contract.json(
        "/v1/agents",
        {"data": [{"id": "polly-agent", "name": "polly", "harness": "pi"}], "has_more": False},
    )
    page.add_init_script(
        f"if (!localStorage.getItem({json.dumps(storage_key)})) "
        f"localStorage.setItem({json.dumps(storage_key)}, {json.dumps(json.dumps(legacy))})"
    )
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{appearance_url}/settings/context-usage")
    section = page.locator("#context-usage")
    edit = section.get_by_role("button", name="Edit Example host polly model-a", exact=True)
    edit.click()
    capacity = section.get_by_role("spinbutton", name="Edit context total", exact=True)
    tokens = section.get_by_role("spinbutton", name="Edit compact buffer", exact=True)
    percent = section.get_by_role("spinbutton", name="Edit compact buffer percent", exact=True)
    expect(capacity).to_have_value("100000")
    expect(tokens).to_have_value("20000")

    def saved() -> dict[str, object]:
        return page.evaluate(
            "([key, source]) => JSON.parse(localStorage.getItem(key)).overrides[source]",
            [storage_key, source_key],
        )

    section.get_by_role("button", name="Percent", exact=True).last.click()
    percent.fill("10")
    percent.blur()
    section.get_by_role("button", name="Tokens", exact=True).last.click()
    expect(tokens).to_have_value("20000")
    save = section.get_by_role("button", name="Save", exact=True)
    save.click()
    assert saved()["autoCompactBufferPercent"] == 10
    assert saved()["autoCompactBufferTokens"] == 20000
    edit.click()
    tokens.fill("100000")
    tokens.blur()
    expect(section.get_by_role("alert")).to_be_visible()
    expect(save).to_be_disabled()
    assert saved()["autoCompactBufferTokens"] == 20000
    tokens.fill("20000")
    tokens.blur()
    capacity.fill("20000")
    capacity.blur()
    expect(section.get_by_role("alert")).to_be_visible()
    expect(save).to_be_disabled()
    assert saved()["contextWindowTokens"] == 100000
    capacity.fill("100000")
    capacity.blur()
    tokens.fill("0")
    tokens.blur()
    section.get_by_role("button", name="Percent", exact=True).last.click()
    expect(percent).to_have_value("10")
    percent.fill("0")
    percent.blur()
    section.get_by_role("button", name="Tokens", exact=True).last.click()
    expect(tokens).to_have_value("0")
    with page.expect_response(
        lambda response: (
            response.request.method == "PATCH"
            and response.url.endswith("/v1/me/preferences/usage_context")
        )
    ):
        save.click()
    assert saved()["autoCompactBufferPercent"] == 0
    assert saved()["autoCompactBufferTokens"] == 0
    page.reload()
    edit.click()
    expect(tokens).to_have_value("0")
    assert saved()["autoCompactBufferPercent"] == 0
    output = Path(request.config.getoption("--output"))
    output.mkdir(parents=True, exist_ok=True)
    for width in (1440, 390):
        page.set_viewport_size({"width": width, "height": 900})
        tokens.evaluate("element => element.scrollIntoView({block: 'center'})")
        if width == 390:
            expect(page.locator('[data-sidebar-open="true"]')).to_have_count(0)
            expect(page.get_by_test_id("settings-nav-general")).not_to_be_in_viewport()
            tokens.click()
            tokens.blur()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.screenshot(path=str(output / f"buffer-manual-{width}.png"), full_page=True)
