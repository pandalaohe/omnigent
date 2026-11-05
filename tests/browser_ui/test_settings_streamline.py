"""Browser contracts for consolidated settings and safe per-source edits."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from playwright.sync_api import Page, Request, Response, expect

from omnigent.server.child_idle import DEFAULT_CHECKIN_INSTRUCTIONS
from tests.browser_ui.conftest import BrowserContract
from tests.browser_ui.test_sessions_appearance import appearance_url as appearance_url


@pytest.fixture(autouse=True)
def settings_contract(appearance_url: str, browser_contract: BrowserContract) -> None:
    browser_contract.json(
        "/v1/agents/with-sessions", {"agent_ids": [], "template_by_agent_id": {}}
    )
    browser_contract.json(
        "/v1/me/preferences/keep_warm/convert",
        {
            "layers": {"corner": {}, "hosts": {}, "agents": {}, "cells": {}, "hostWins": {}},
            "already": True,
        },
        method="POST",
    )
    browser_contract.json(
        "/v1/system/settings",
        {"health_check": {"project_id": None, "host_id": None, "prompt": None}},
    )


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
    expect(section.get_by_role("button", name="Done editing Claude Code")).to_be_visible()
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
    browser_contract.json(
        "/v1/child-checkin/defaults", {"instructions": DEFAULT_CHECKIN_INSTRUCTIONS}
    )
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
    section.locator("summary").click()
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
    section.locator("summary").click()
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
        ("runtime-resources", "/system"),
        ("calling-defaults", "/settings/harnesses#calling-defaults"),
        ("agents", "/settings/custom-agents"),
    ],
)
def test_legacy_links_open_the_new_group(
    page: Page, appearance_url: str, browser_contract: BrowserContract, old: str, destination: str
) -> None:
    browser_contract.json("/v1/calling-defaults/catalogs", {"rows": []})
    if old == "keep-warm":
        browser_contract.json(
            "/v1/me/preferences/keep_warm/convert",
            {
                "layers": {"corner": {}, "hosts": {}, "agents": {}, "cells": {}, "hostWins": {}},
                "already": False,
                "banner": (
                    "Converted from your previous settings · saved when you first edit. "
                    "New hosts start off; new agents start off unless their harness has a column."
                ),
                "bytes": 36,
                "migratedToLayersAt": 123,
            },
            method="POST",
        )
    page.goto(f"{appearance_url}/settings/{old}")
    expect(page).to_have_url(f"{appearance_url}{destination}")
    if "#" in destination:
        anchor = destination.split("#", 1)[1]
        expect(page.locator(f"#{anchor}")).to_be_visible()
        if anchor != "calling-defaults":
            assert page.locator(f"#{anchor}").evaluate("element => element.open")
    expect(page.get_by_test_id(f"settings-nav-{old}")).to_have_count(0)


@pytest.mark.parametrize("width", [1440, 390])
def test_keep_warm_table_convert_and_cell_persist_with_support_reasons(
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
    browser_contract.json(
        "/v1/hosts",
        {
            "hosts": [
                {
                    "host_id": "example-host",
                    "name": "Example host",
                    "owner": "local",
                    "status": "online",
                    "configured_harnesses": {
                        "claude-native": True,
                        "codex-native": True,
                        "openai-agents": True,
                    },
                }
            ]
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
            }
        },
        "hostOfflineArchiveSeconds": 0,
    }
    converted_layers = {
        "corner": {"main": False, "child": False, "maxSeconds": 14400},
        "hosts": {},
        "agents": {},
        "cells": {
            host: {"harness:claude-native": {"main": True, "maxSeconds": 7200}}
            for host in ("", "example-host")
        },
        "hostWins": {},
    }
    browser_contract.json(
        "/v1/agents/with-sessions",
        {"agent_ids": ["claude-agent", "other-agent"], "template_by_agent_id": {}},
    )
    conversion_calls: list[bool] = []

    def convert(incoming: Request) -> dict:
        payload = incoming.post_data_json
        assert isinstance(payload, dict)
        dry_run = payload["dry_run"]
        assert payload["target"] == "layers"
        conversion_calls.append(dry_run)
        already = "layers" in preferences
        layers = preferences["layers"] if already else converted_layers
        return {
            "layers": layers,
            "already": already,
            "migratedToLayersAt": preferences.get("migratedToLayersAt", 123),
        }

    def save(incoming: Request) -> dict[str, bool]:
        preferences.update(incoming.post_data_json["value"])
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
    browser_contract.json("/v1/me/preferences/keep_warm/convert", convert, method="POST")
    browser_contract.json("/v1/me/preferences/keep_warm", save, method="PATCH")
    browser_contract.response("/v1/me/preferences/session_collab", method="PATCH")
    page.add_init_script(
        "if (!localStorage.getItem('omnigent:keep-warm')) "
        f"localStorage.setItem('omnigent:keep-warm', {json.dumps(json.dumps(preferences))})"
    )
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{appearance_url}/settings/keep-warm")
    section = page.get_by_test_id("keep-warm-settings")
    expect(section.get_by_role("table", name="Keep-warm by agent and host")).to_be_visible()
    expect(
        section.get_by_text(
            "Converted from your previous settings · saved when you first edit", exact=True
        )
    ).to_be_visible()
    expect(
        section.get_by_role("button", name=re.compile(r"^Edit Claude Code on No host:"))
    ).to_have_attribute("aria-label", re.compile("main on"))
    assert conversion_calls == [True]
    add_agent = section.get_by_role("button", name="Show agents with no sessions (1)", exact=True)
    # Center the trigger before clicking in the long General section.
    add_agent.evaluate("element => element.scrollIntoView({block: 'center'})")
    add_agent.click()
    expect(section.get_by_text("No keep-warm:", exact=True)).to_be_visible()
    expect(section.get_by_title("other-agent", exact=True)).to_contain_text("Other")
    expect(section.get_by_role("button", name=re.compile(r"^Edit Other"))).to_have_count(0)
    section.get_by_role("button", name="About agents with no keep-warm channel").click()
    expect(
        page.get_by_text(
            "These agents have no cache channel to ping, and they show no warm or cold state.",
            exact=True,
        )
    ).to_be_visible()
    page.keyboard.press("Escape")
    assert section.evaluate("element => element.scrollWidth <= element.clientWidth")
    section.get_by_role("button", name=re.compile(r"^Edit Codex on No host:")).click()
    editor = page.locator('[data-slot="popover-content"][data-state="open"]')
    expect(editor.get_by_text("Codex on No host", exact=True)).to_be_visible()
    editor.get_by_role("radiogroup", name="Children", exact=True).get_by_role(
        "radio", name="On", exact=True
    ).click()
    editor.get_by_role("spinbutton", name="Ping every in minutes").fill("10")
    editor.get_by_role("spinbutton", name="Ping every in minutes").blur()
    with page.expect_response(
        lambda response: (
            response.request.method == "PATCH"
            and response.url.endswith("/v1/me/preferences/keep_warm")
            and response.request.post_data_json["value"]["layers"]["cells"]
            .get("", {})
            .get("harness:codex-native", {})
            .get("intervalSeconds")
            == 600
        )
    ):
        editor.get_by_role("button", name="Save", exact=True).click()
    expect(editor).not_to_be_visible()
    saved = page.evaluate("JSON.parse(localStorage.getItem('omnigent:keep-warm'))")
    assert saved["layers"]["cells"][""]["harness:codex-native"] == {
        "child": True,
        "intervalSeconds": 600,
    }
    assert saved["layers"]["cells"]["example-host"] == converted_layers["cells"]["example-host"]
    assert saved["migratedToLayersAt"] == 123
    assert saved["hostOfflineArchiveSeconds"] == 0
    assert preferences["agents"]["claude-agent"]["intervalSeconds"] == 3300
    assert preferences["default"] == {"main": False, "child": False, "maxSeconds": 14400}
    assert conversion_calls == [True]
    page.reload()
    reload_show = section.get_by_role(
        "button", name="Show agents with no sessions (1)", exact=True
    )
    reload_show.evaluate("element => element.scrollIntoView({block: 'center'})")
    reload_show.click()
    expect(
        section.get_by_role("button", name=re.compile(r"^Edit Codex on No host:"))
    ).to_have_attribute("aria-label", re.compile("children on"))
    expect(
        section.get_by_role("button", name=re.compile(r"^Edit Claude Code on No host:"))
    ).to_have_attribute("aria-label", re.compile("main on"))
    assert conversion_calls == [True, True]
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
        {
            "id": "master-model",
            "displayName": "Master model",
            "isDefault": True,
            "supportedReasoningEfforts": [{"reasoningEffort": "xhigh"}],
        },
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
    effort = page.get_by_test_id("project-settings-host-effort-host-a")
    expect(effort).to_have_text("Default")
    inherited_effort = effort.get_by_role("radio", name="xHigh", exact=True)
    expect(inherited_effort).not_to_be_checked()
    expect(inherited_effort.locator("span")).to_have_class(re.compile(r"bg-companion/45"))
    for field, label in [("speed", "Standard"), ("permission", "Read Only")]:
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
    detail.get_by_text("Default agent", exact=True).scroll_into_view_if_needed()
    expect(detail.get_by_text("Default agent", exact=True)).to_be_in_viewport()
    page.screenshot(path=str(output / f"project-host-row-{width}.png"), full_page=True)
    page.set_viewport_size({"width": width, "height": 900})
    copy.click()
    panel = page.get_by_test_id("project-settings-copy-panel")
    expect(panel).to_be_visible()
    for field in ("model", "effort", "speed", "permission"):
        expect(page.get_by_test_id(f"project-settings-copy-field-{field}")).to_be_checked()
    expect(page.get_by_test_id("project-settings-copy-field-agent")).not_to_be_checked()
    expect(page.get_by_test_id("project-settings-copy-summary")).to_contain_text(
        "All 1 applicable projects · 1 skipped"
    )
    panel.get_by_role("button", name="Change…", exact=True).click()
    picker = page.get_by_role("dialog", name="Apply to which projects", exact=True)
    expect(picker.get_by_role("checkbox", name=re.compile(r"^Other"))).to_be_checked()
    picker.get_by_role("button", name="Skipped (1) · no entry folder on this host").click()
    unavailable = picker.get_by_role("checkbox", name=re.compile(r"^Without folder"))
    expect(unavailable).to_be_disabled()
    expect(unavailable).not_to_be_checked()
    picker.get_by_role("button", name="Done", exact=True).click()
    expect(picker).not_to_be_visible()
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
    host_id = "11111111-1111-4111-8111-111111111111"
    source_key = json.dumps([host_id, "polly", "pi", "", "cell"], separators=(",", ":"))
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
    browser_contract.json(f"/v1/hosts/{host_id}/mcp-servers", {"servers": []})
    browser_contract.json(
        "/v1/hosts",
        {
            "hosts": [
                {
                    "host_id": host_id,
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
    edit = section.get_by_role("button", name=re.compile(r"^Edit Polly on Example host:"))
    edit.click()
    manager = page.locator('[data-slot="popover-content"][data-state="open"]')
    capacity = manager.get_by_role(
        "spinbutton", name="Context total for Polly on Example host", exact=True
    )
    tokens = manager.get_by_role(
        "spinbutton", name="Compact buffer for Polly on Example host", exact=True
    )
    percent = tokens
    expect(capacity).to_have_value("100000")
    expect(tokens).to_have_value("20000")

    def saved() -> dict[str, object]:
        return page.evaluate(
            "([key, source]) => JSON.parse(localStorage.getItem(key)).overrides[source]",
            [storage_key, source_key],
        )

    buffer_unit = manager.get_by_role(
        "radiogroup", name="Buffer unit for Polly on Example host", exact=True
    )
    buffer_unit.get_by_role("radio", name="%", exact=True).click()
    percent.fill("10")
    percent.blur()
    buffer_unit.get_by_role("radio", name="Tokens", exact=True).click()
    expect(tokens).to_have_value("20000")
    save = manager.get_by_role("button", name="Save", exact=True)
    save.click()
    assert saved()["autoCompactBufferPercent"] == 10
    assert saved()["autoCompactBufferTokens"] == 20000
    edit.click()
    tokens.fill("100000")
    tokens.blur()
    save.click()
    expect(manager.get_by_role("alert")).to_be_visible()
    expect(manager).to_be_visible()
    assert saved()["autoCompactBufferTokens"] == 20000
    tokens.fill("20000")
    tokens.blur()
    capacity.fill("20000")
    capacity.blur()
    save.click()
    expect(manager.get_by_role("alert")).to_be_visible()
    expect(manager).to_be_visible()
    assert saved()["contextWindowTokens"] == 100000
    capacity.fill("100000")
    capacity.blur()
    tokens.fill("0")
    tokens.blur()
    buffer_unit.get_by_role("radio", name="%", exact=True).click()
    expect(percent).to_have_value("10")
    percent.fill("0")
    percent.blur()
    buffer_unit.get_by_role("radio", name="Tokens", exact=True).click()
    expect(tokens).to_have_value("0")
    with page.expect_response(
        lambda response: (
            response.request.method == "PATCH"
            and response.url.endswith("/v1/me/preferences/usage_context")
            and response.request.post_data_json["value"]["overrides"][source_key][
                "autoCompactBufferPercent"
            ]
            == 0
            and response.request.post_data_json["value"]["overrides"][source_key][
                "autoCompactBufferTokens"
            ]
            == 0
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
        page.keyboard.press("Escape")
        page.set_viewport_size({"width": width, "height": 900})
        edit.click()
        tokens.evaluate("element => element.scrollIntoView({block: 'center'})")
        if width == 390:
            expect(page.locator('[data-sidebar-open="true"]')).to_have_count(0)
            expect(page.get_by_test_id("settings-nav-general")).not_to_be_in_viewport()
            tokens.click()
            tokens.blur()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.screenshot(path=str(output / f"buffer-manual-{width}.png"), full_page=True)


@pytest.mark.parametrize("width", [1440, 390])
def test_context_table_lists_saved_values_by_agent_and_keeps_their_layers(
    page: Page,
    appearance_url: str,
    browser_contract: BrowserContract,
    width: int,
    request: pytest.FixtureRequest,
) -> None:
    host_a = "11111111-1111-4111-8111-111111111111"
    host_b = "22222222-2222-4222-8222-222222222222"

    def stored_key(parts: list[str]) -> str:
        return json.dumps(parts, separators=(",", ":"))

    prefix = "claude-opus-provider-variant-with-a-long-shared-context-profile-name-" * 2
    models = [prefix + "standard", prefix + "extended", "sonnet", "sonnet-extended", "haiku"]
    claude_keys = [stored_key([host_a, "claude", "claude-native", model]) for model in models]
    codex_keys = [
        stored_key([host_a, "codex", "codex-native", model]) for model in ("gpt-x", "gpt-y")
    ]
    auto_key = stored_key([host_a, "opencode", "opencode", ""])
    other_host_key = stored_key([host_b, "codex-sdk", "codex", "gpt-x"])
    claude_cell = stored_key([host_a, "claude", "claude-native", "", "cell"])
    claude_column = stored_key(["", "claude", "claude-native", "", "column"])
    saved_values = {
        **{
            key: {"contextWindowTokens": 550000, "autoCompactBufferTokens": 32000}
            for key in claude_keys
        },
        **{
            key: {"contextWindowTokens": 400000, "autoCompactBufferTokens": 0}
            for key in codex_keys
        },
        auto_key: {"contextWindowTokens": 200000, "autoCompactBufferTokens": None},
        other_host_key: {"contextWindowTokens": 300000, "autoCompactBufferTokens": None},
    }
    synced = {
        "value": {
            "version": 6,
            "showProviderUsageLimits": True,
            "overrides": saved_values,
            "lastProviderUsageLimits": {},
        }
    }
    writes: list[dict] = []

    def save_preferences(incoming: Request) -> dict[str, bool]:
        value = incoming.post_data_json["value"]
        writes.append(value)
        synced["value"] = value
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
    harnesses = ("claude-native", "codex-native", "opencode", "codex")
    browser_contract.json(
        "/v1/agents",
        {
            "data": [
                {
                    "id": f"saved-source-{harness}",
                    "name": name,
                    "harness": harness,
                    "builtin": True,
                    "product_of": harness,
                }
                for name, harness in zip(
                    ("claude", "codex", "opencode", "codex-sdk"), harnesses, strict=True
                )
            ],
            "has_more": False,
        },
    )
    browser_contract.json(
        "/v1/harnesses",
        {
            "data": [{"id": harness, "label": harness} for harness in harnesses],
            "capabilities_by_harness": {
                harness: {
                    "integration_mode": "sdk-in-process" if harness == "codex" else "native-tui"
                }
                for harness in harnesses
            },
        },
    )
    browser_contract.json(
        "/v1/hosts",
        {
            "hosts": [
                {
                    "host_id": host_id,
                    "name": name,
                    "owner": "local",
                    "status": "online",
                    "configured_harnesses": dict.fromkeys(harnesses, True),
                }
                for host_id, name in ((host_a, "Alpha"), (host_b, "Beta"))
            ]
        },
    )
    for host_id in (host_a, host_b):
        browser_contract.json(f"/v1/hosts/{host_id}/mcp-servers", {"servers": []})

    def is_preferences_write(response: Response) -> bool:
        return response.request.method == "PATCH" and response.url.endswith(
            "/v1/me/preferences/usage_context"
        )

    page.set_viewport_size({"width": width, "height": 900})
    with page.expect_response(is_preferences_write):
        page.goto(f"{appearance_url}/settings/context-usage")
    section = page.locator("#context-usage")
    table = section.get_by_role("table", name="Context limits", exact=True)
    claude_group = table.get_by_test_id("context-group-claude-claude-native")
    codex_group = table.get_by_test_id("context-group-codex-codex-native")
    expect(table.get_by_role("rowheader")).to_have_count(5)
    expect(section.get_by_text(models[0], exact=True)).to_have_count(0)
    assert len(writes) == 1
    listed = synced["value"]["contextList"]["entries"]
    for agent_name, harness, model in (
        ("claude", "claude-native", models[0]),
        ("claude", "claude-native", models[4]),
        ("codex", "codex-native", "gpt-y"),
        ("opencode", "opencode", ""),
        ("codex-sdk", "codex", "gpt-x"),
    ):
        assert {"agentName": agent_name, "harness": harness, "model": model} in listed

    # The list is seeded once: a later load writes nothing.
    page.reload()
    expect(table.get_by_role("rowheader")).to_have_count(5)
    assert len(writes) == 1

    # Saved model values are rows of their agent, and one open group closes the others.
    claude_group.get_by_role("button", name=re.compile(r"^Expand ")).click()
    expect(table.get_by_role("rowheader")).to_have_count(10)
    output = Path(request.config.getoption("--output"))
    output.mkdir(parents=True, exist_ok=True)
    for index, model in enumerate(models[:2]):
        label = table.get_by_text(model, exact=True)
        label.scroll_into_view_if_needed()
        expect(label).to_be_in_viewport()
        assert label.evaluate(
            """element => {
                const style = getComputedStyle(element);
                return style.textOverflow !== 'ellipsis' && style.whiteSpace !== 'nowrap'
                    && (style.webkitLineClamp === 'none' || style.webkitLineClamp === '')
                    && element.scrollWidth <= element.clientWidth + 1
                    && element.scrollHeight <= element.clientHeight + 1;
            }"""
        )
        page.screenshot(
            path=str(output / f"context-full-model-{index}-{width}.png"), full_page=True
        )
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    codex_group.get_by_role("button", name=re.compile(r"^Expand ")).click()
    expect(codex_group.get_by_text("gpt-y", exact=True)).to_be_visible()
    expect(table.get_by_text(models[0], exact=True)).to_have_count(0)
    with page.expect_response(is_preferences_write):
        section.get_by_role("switch", name="Keep several open", exact=True).click()
    claude_group.get_by_role("button", name=re.compile(r"^Expand ")).click()
    expect(table.get_by_text(models[0], exact=True)).to_be_visible()
    expect(codex_group.get_by_text("gpt-y", exact=True)).to_be_visible()

    layered = {
        claude_keys[0]: {"contextWindowTokens": 300000, "autoCompactBufferTokens": None},
        claude_keys[1]: {"contextWindowTokens": None, "autoCompactBufferTokens": 10000},
        claude_cell: {"contextWindowTokens": 400000, "autoCompactBufferTokens": 20000},
        claude_column: {"contextWindowTokens": 500000, "autoCompactBufferTokens": 30000},
    }
    synced["value"] = {**synced["value"], "overrides": layered}
    writes.clear()
    page.reload()
    cell = page.get_by_test_id(f"context-cell-claude-claude-native-{host_a}")
    expect(cell).to_have_attribute(
        "aria-label", re.compile(r"context 400000 tokens.*compact buffer 20000 tokens")
    )
    claude_group.get_by_role("button", name=re.compile(r"^Expand ")).click()
    alpha_cell = re.compile(r"^Edit .+ on Alpha:")
    exact_row = claude_group.get_by_role("row").filter(has_text=models[0])
    inherited_row = claude_group.get_by_role("row").filter(has_text=models[1])
    expect(exact_row.get_by_role("button", name=alpha_cell)).to_have_attribute(
        "aria-label", re.compile(r"context 300000 tokens.*compact buffer 20000 tokens")
    )
    expect(inherited_row.get_by_role("button", name=alpha_cell)).to_have_attribute(
        "aria-label", re.compile(r"context 400000 tokens.*compact buffer 10000 tokens")
    )

    # Clearing the cell falls through to the agent's all-computers value.
    cell.click()
    editor = page.locator('[data-slot="popover-content"][data-state="open"]')
    editor.get_by_role("spinbutton", name=re.compile(r"^Context total for ")).fill("")
    with page.expect_response(is_preferences_write):
        editor.get_by_role("button", name="Save", exact=True).click()
    expect(inherited_row.get_by_role("button", name=alpha_cell)).to_have_attribute(
        "aria-label", re.compile(r"context 500000 tokens.*compact buffer 10000 tokens")
    )
    assert synced["value"]["overrides"][claude_cell] == {
        "contextWindowTokens": None,
        "autoCompactBufferTokens": 20000,
        "compactBufferMode": "tokens",
    }
    assert synced["value"]["overrides"][claude_column] == layered[claude_column]

    # A typed model lands in its agent's group; a removed row stays removed after a reload.
    section.get_by_test_id("context-add-entry").click()
    picker = page.get_by_test_id("context-add-picker")
    picker.get_by_role("combobox").fill("typed-model")
    with page.expect_response(is_preferences_write):
        picker.get_by_role("option", name=re.compile(r"^claude", re.IGNORECASE)).first.click()
    expect(claude_group.get_by_text("typed-model", exact=True)).to_be_visible()
    opencode_group = table.get_by_test_id("context-group-opencode-opencode")
    with page.expect_response(is_preferences_write):
        opencode_group.get_by_role("button", name=re.compile(r"^Remove ")).click()
    expect(opencode_group).to_have_count(0)
    page.reload()
    expect(table.get_by_role("rowheader").first).to_be_visible()
    expect(opencode_group).to_have_count(0)
    entries = synced["value"]["contextList"]["entries"]
    assert {"agentName": "opencode", "harness": "opencode", "model": ""} not in entries
    assert {"agentName": "claude", "harness": "claude-native", "model": "typed-model"} in entries
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=str(output / f"context-layers-{width}.png"), full_page=True)


@pytest.mark.parametrize("width", [1440, 390])
def test_instruction_columns_add_clear_and_reload(
    page: Page,
    appearance_url: str,
    browser_contract: BrowserContract,
    width: int,
    request: pytest.FixtureRequest,
) -> None:
    browser_contract.json(
        "/v1/child-checkin/defaults", {"instructions": DEFAULT_CHECKIN_INSTRUCTIONS}
    )
    layer = {
        "scope": "agent",
        "agent_key": "codex",
        "host_id": "",
        "text": "Configured",
        "revision_id": "first",
        "updated_at": 1,
        "updated_by": "local",
        "chars": 10,
        "host_removed": False,
        "stale_layers": [],
    }
    descriptors = [
        {"key": "codex", "display_name": "Codex", "harnesses": ["codex-native"]},
        {"key": "acp:neutral", "display_name": "Neutral ACP", "harnesses": ["acp:neutral"]},
    ]
    browser_contract.json(
        "/v1/instruction-layers",
        lambda _: {
            "data": [layer],
            "agents": descriptors,
            "order": ["shared", "agent", "host"],
            "route_sizes": [],
            "size_reminder_chars": 10000,
        },
    )

    def save(incoming: Request) -> dict:
        layer.update(incoming.post_data_json)
        layer.update(revision_id="cleared", chars=len(layer["text"]))
        return {**layer, "over_budget_routes": []}

    browser_contract.json("/v1/instruction-layers", save, method="PUT")
    browser_contract.json("/v1/instruction-layers/revisions", {"data": []})
    browser_contract.json(
        "/v1/harnesses",
        {
            "data": [],
            "capabilities_by_harness": {
                "codex-native": {"integration_mode": "native-tui"},
                "acp": {"integration_mode": "generic-acp"},
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
                    "status": "offline",
                    "configured_harnesses": {"codex-native": True, "acp:neutral": True},
                }
            ]
        },
    )
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{appearance_url}/settings/instructions")
    expect(page.get_by_role("button", name="Edit Codex", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Edit Neutral ACP", exact=True)).to_have_count(0)
    page.get_by_role("button", name="Add agent", exact=True).click()
    page.get_by_role("button", name=re.compile(r"Neutral ACP.*ACP")).click()
    expect(page.get_by_role("button", name="Edit Neutral ACP", exact=True)).to_be_visible()
    page.get_by_role("button", name="Edit Codex", exact=True).click()
    page.get_by_role("textbox", name="Instruction text").fill("")
    with page.expect_response(
        lambda response: (
            response.request.method == "PUT" and response.url.endswith("/v1/instruction-layers")
        )
    ):
        page.get_by_role("button", name="Save layer", exact=True).click()
    assert layer["text"] == ""
    page.reload()
    expect(page.get_by_role("button", name="Edit Shared", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Edit Codex", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Edit Neutral ACP", exact=True)).to_be_visible()
    page.get_by_role("button", name="Edit Neutral ACP", exact=True).click()
    expect(page.get_by_role("textbox", name="Instruction text")).to_have_value("")
    page.keyboard.press("Escape")
    output = Path(request.config.getoption("output"))
    output.mkdir(parents=True, exist_ok=True)
    page.get_by_role(
        "table", name="Instruction layers by host and agent"
    ).scroll_into_view_if_needed()
    page.screenshot(path=str(output / f"instruction-columns-{width}.png"), full_page=True)
