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
def test_keep_warm_copy_skips_incompatible_interval(
    page: Page, appearance_url: str, browser_contract: BrowserContract, width: int
) -> None:
    agents = [
        {"id": "claude-agent", "name": "claude", "harness": "claude-native"},
        {"id": "codex-agent", "name": "codex", "harness": "codex-native"},
        {"id": "other-agent", "name": "other", "harness": "openai-agents"},
    ]
    browser_contract.json("/v1/agents", {"data": agents, "has_more": False})
    preferences = {
        "agents": {
            "claude-agent": {
                "main": True,
                "child": False,
                "intervalSeconds": 3300,
                "maxSeconds": 7200,
            },
            "codex-agent": {
                "main": False,
                "child": False,
                "intervalSeconds": 600,
                "maxSeconds": 14400,
            },
        },
        "hostOfflineArchiveSeconds": 0,
    }
    browser_contract.json(
        "/v1/me/preferences",
        {
            "user_id": "local",
            "preferences": {"version": 1, "settings": {"keep_warm": preferences}},
        },
    )
    browser_contract.response("/v1/me/preferences/keep_warm", method="PATCH")
    browser_contract.response("/v1/me/preferences/session_collab", method="PATCH")
    page.add_init_script(
        f"localStorage.setItem('omnigent:keep-warm', {json.dumps(json.dumps(preferences))})"
    )
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{appearance_url}/settings/keep-warm")
    expect(page.get_by_role("combobox", name="Keep-warm Agent")).to_be_visible()
    expect(page.get_by_test_id("keep-warm-agent-row")).to_have_count(1)
    page.get_by_role("button", name="Apply to other deployed agents", exact=True).click()
    dialog = page.get_by_role("dialog", name="Apply to other deployed agents", exact=True)
    expect(dialog.get_by_text("Skip: Every (Outside 5–29 min for this agent)")).to_be_visible()
    expect(dialog.get_by_text("This agent has no keep-warm channel", exact=False)).to_be_visible()
    assert dialog.evaluate("element => element.scrollWidth <= element.clientWidth")
    dialog.get_by_role("button", name="Apply selected fields", exact=True).click()
    saved = page.evaluate("JSON.parse(localStorage.getItem('omnigent:keep-warm'))")
    assert saved["agents"]["codex-agent"]["intervalSeconds"] == 600
    assert saved["agents"]["codex-agent"]["main"] is True
    assert saved["agents"]["codex-agent"]["maxSeconds"] == 7200
    assert "other-agent" not in saved["agents"]
    assert saved["hostOfflineArchiveSeconds"] == 0


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
    master = {"host-a": {"codex-native": {"model": "master-model"}}}
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
    expect(model).to_contain_text("Inherit master table · Master model")
    output = Path(request.config.getoption("--output"))
    output.mkdir(parents=True, exist_ok=True)
    model.evaluate("element => element.scrollIntoView({block: 'center'})")
    page.screenshot(path=str(output / f"project-{width}.png"), full_page=True)
    copy = page.get_by_test_id("project-settings-host-copy-host-a-codex-native")
    expect(copy).to_be_disabled()
    page.get_by_role("button", name="Cancel", exact=True).click()
    open_project()
    expect(model).to_have_text("Project model")
    expect(copy).to_be_enabled()
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
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.screenshot(path=str(output / f"buffer-manual-{width}.png"), full_page=True)
