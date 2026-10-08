"""E2E: the home composer prefills from a project's stored ``config``.

Choosing ``New session in <name>`` from the command palette navigates to
``/?project=<name>`` and seeds the composer from that project's stored defaults
(``web/src/shell/projectPrefill.ts`` +
``web/src/shell/NewChatDialog.tsx``): host, working directory, and agent all
come from ``config``, silently falling back to the generic defaults for any
field the config leaves unset. This replaced the old newest-session inference —
stored config is now the single project-driven prefill source.

This drives the real chain: the composer resolves the project NAME → id via
``GET /v1/sessions/projects``, fetches ``GET /v1/projects/{id}`` for its config,
and seeds the host / workspace / agent, which then ride along to the create
``POST /v1/sessions``.

Heavy ``page.route`` stubbing mirrors ``test_start_session`` and is required for
the same reason: the e2e_ui harness's tunneled runner registers no *host* and
the host filesystem endpoint has nothing to browse, so ``/v1/hosts``,
``/v1/agents``, the project config, and the create ``POST`` are faked (the POST
handler *captures the body* — the thing under test — and returns a real seeded
session id so post-send navigation lands somewhere real). Default precedence
and worktree request variants are covered in
``web/src/shell/NewChatDialog.projectPrefill.test.tsx``.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from playwright.async_api import Route, async_playwright, expect

from tests._helpers.async_thread import run_in_fresh_loop as _run_in_fresh_loop
from tests._helpers.picker_routes import OWN_AGENTS
from tests.e2e_ui.start_session.helpers import (
    commit_landing_workspace_picker,
    open_landing_workspace_picker,
    stub_empty_host_picker_data,
)

_HOST_ID = "host_e2e_cfg"
_PROJECT_ID = "proj_e2e_cfg"
_PROJECT_NAME = "ConfiguredProject"
_CONFIG_WORKSPACE = "/work/configured-repo"
# Bare create endpoint (POST captured); NOT the /{id}/... sub-routes.
_SESSIONS_RE = re.compile(r"/v1/sessions(\?.*)?$")
# One project config endpoint: /v1/projects/<id> (not the bare list).
_PROJECT_CFG_RE = re.compile(r"/v1/projects/[^/?]+")
# One project host-roots endpoint: /v1/projects/<id>/host-roots.
_PROJECT_HOST_ROOTS_RE = re.compile(r"/v1/projects/[^/]+/host-roots")


async def _wait_until(predicate, *, timeout_s: float = 15.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"condition not met within {timeout_s:.0f}s")


def _hosts_body() -> str:
    return json.dumps(
        {"hosts": [{"host_id": _HOST_ID, "name": "e2e-host", "owner": "e2e", "status": "online"}]}
    )


def _agents_body() -> str:
    """Two agents: the default-ranked Claude Code and a second the config pins."""
    return json.dumps(
        {
            "data": [
                {
                    "id": "ag_claude_e2e",
                    "name": "claude-native-ui",
                    "display_name": "Claude Code",
                    "description": "Anthropic's coding agent",
                    "harness": None,
                    "skills": [],
                },
                {
                    "id": "ag_pinned_e2e",
                    "name": "polly",
                    "display_name": "Polly",
                    "description": "Multi-agent coding",
                    "harness": "claude-sdk",
                    "skills": [],
                },
            ]
        }
    )


def _projects_list_body() -> str:
    """``GET /v1/sessions/projects`` — the composer resolves the ?project= name
    to this id. Returns a bare ``ProjectSummary[]`` (``{id, name}``)."""
    return json.dumps([{"id": _PROJECT_ID, "name": _PROJECT_NAME}])


def _project_config_body() -> str:
    """``GET /v1/projects/{id}`` — the stored defaults the composer seeds from."""
    return json.dumps(
        {
            "id": _PROJECT_ID,
            "name": _PROJECT_NAME,
            "config": {
                "host_id": _HOST_ID,
                "workspace": _CONFIG_WORKSPACE,
                "agent_id": "ag_pinned_e2e",
            },
        }
    )


def test_composer_prefills_from_project_config(seeded_session: tuple[str, str]) -> None:
    """The project command seeds host / workspace / agent from stored config.

    The pinned agent (``ag_pinned_e2e``) and workspace (``/work/configured-repo``)
    come from ``config`` — NOT from the default-ranked Claude Code or a recent
    workspace — and ride along to ``POST /v1/sessions``.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_prefill(base_url, session_id))


async def _drive_prefill(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []

            async def handle_hosts(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_hosts_body()
                )

            async def handle_agents(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_agents_body()
                )

            async def handle_projects_list(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_projects_list_body()
                )

            async def handle_project_config(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_project_config_body()
                )

            async def handle_events(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"queued": True, "item_id": "ci_e2e"}),
                )

            async def handle_sessions(route: Route) -> None:
                if route.request.method == "POST":
                    create_bodies.append(route.request.post_data_json)
                    await route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({"id": session_id}),
                    )
                else:
                    await route.continue_()

            # Neutralize the agent-discovery scan so only the stubbed catalog
            # feeds the picker (a leftover native agent would rank ahead).
            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=json.dumps({"data": []})
                )

            await page.route("**/v1/hosts", handle_hosts)
            await stub_empty_host_picker_data(page, _HOST_ID)
            await page.route("**/v1/agents", handle_agents)
            await page.route("**/v1/sessions/projects", handle_projects_list)
            await page.route(_PROJECT_CFG_RE, handle_project_config)
            await _stub_project_host_roots(page, _project_config_body())
            await page.route("**/v1/sessions/*/events", handle_events)
            await page.route(_SESSIONS_RE, handle_sessions)
            await page.route(
                re.compile(r"/v1/sessions\?(?!.*pinned=).*visibility=mine"), handle_agent_scan
            )
            await page.route(OWN_AGENTS, lambda route: route.fulfill(json={"data": []}))

            await page.goto(f"{base_url}/c/{session_id}")
            await page.get_by_placeholder("Send a message…").wait_for(
                state="visible", timeout=30_000
            )
            await page.keyboard.press("ControlOrMeta+k")
            palette = page.get_by_role("dialog")
            await expect(palette).to_be_visible(timeout=10_000)
            await palette.get_by_text(f"New session in {_PROJECT_NAME}", exact=True).click()
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            # The agent picker trigger reflects the config-pinned agent (Polly),
            # not the default-ranked Claude Code — proof the config seed won.
            await expect(page.get_by_test_id("new-chat-landing-agent-select")).to_contain_text(
                "Polly", timeout=15_000
            )

            await page.get_by_test_id("new-chat-landing-input").fill("start here")
            await page.get_by_test_id("new-chat-landing-submit").click()

            await _wait_until(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            assert body["host_id"] == _HOST_ID, body
            # The create names its project and OMITS the fields still holding
            # their untouched config seed, so the server default-fills them
            # from that same config (one source of truth instead of a copy).
            assert body["project_id"] == _PROJECT_ID, body
            assert "agent_id" not in body, body
            assert "workspace" not in body, body
        finally:
            await browser.close()


# The sandbox sentinel a project can store as its default host (mirrors
# SANDBOX_HOST_CHOICE in web/src/lib/hostPreferences.ts).
_SANDBOX_CHOICE = "__sandbox__"


def _managed_info_body() -> str:
    """``GET /v1/info`` for a managed deployment that offers a sandbox."""
    return json.dumps(
        {
            "accounts_enabled": False,
            "login_url": None,
            "needs_setup": False,
            "databricks_features": True,
            "managed_sandboxes_enabled": True,
            "sandbox_provider": "lakebox",
            "server_version": "0.0.0-e2e",
            "smart_routing_enabled": False,
        }
    )


def _sandbox_config_body() -> str:
    """``GET /v1/projects/{id}`` whose stored default host is the sandbox."""
    return json.dumps(
        {
            "id": _PROJECT_ID,
            "name": _PROJECT_NAME,
            "config": {"host_id": _SANDBOX_CHOICE},
        }
    )


def test_composer_prefills_sandbox_default_from_project_config(
    seeded_session: tuple[str, str],
) -> None:
    """A project whose stored default host is the sandbox selects the sandbox.

    Regression: the settings dialog offers (and persists) the sandbox sentinel as
    a default host, but prefill only seeded real online hosts, so the sandbox
    default was silently dropped and the composer fell back to a connected host.
    With the sandbox handled in prefill, a ``?project=`` visit selects the
    sandbox — the create posts ``host_type: "managed"`` (no ``host_id``).
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_sandbox_prefill(base_url, session_id))


async def _drive_sandbox_prefill(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []

            async def handle_info(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_managed_info_body()
                )

            async def handle_sandbox_models(route: Route) -> None:
                await route.fulfill(
                    json={
                        "configured": False,
                        "models": [],
                        "configuration_revision": None,
                        "provider_label": None,
                        "default_model": None,
                        "status": "unconfigured",
                    }
                )

            async def handle_hosts(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_hosts_body()
                )

            async def handle_agents(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_agents_body()
                )

            async def handle_projects_list(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_projects_list_body()
                )

            async def handle_project_config(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_sandbox_config_body()
                )

            async def handle_events(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"queued": True, "item_id": "ci_e2e"}),
                )

            async def handle_sessions(route: Route) -> None:
                if route.request.method == "POST":
                    create_bodies.append(route.request.post_data_json)
                    await route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({"id": session_id}),
                    )
                else:
                    await route.continue_()

            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=json.dumps({"data": []})
                )

            await page.route("**/v1/info", handle_info)
            await page.route(
                "**/v1/sandbox-providers/*/harnesses/*/model-options*", handle_sandbox_models
            )
            await page.route("**/v1/hosts", handle_hosts)
            await page.route("**/v1/agents", handle_agents)
            await page.route("**/v1/sessions/projects", handle_projects_list)
            await page.route(_PROJECT_CFG_RE, handle_project_config)
            await _stub_project_host_roots(page, _sandbox_config_body())
            await page.route("**/v1/sessions/*/events", handle_events)
            await page.route(_SESSIONS_RE, handle_sessions)
            await page.route(
                re.compile(r"/v1/sessions\?(?!.*pinned=).*visibility=mine"), handle_agent_scan
            )
            await page.route(OWN_AGENTS, lambda route: route.fulfill(json={"data": []}))

            await page.goto(f"{base_url}/?project={_PROJECT_NAME}")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            # The host chip shows the sandbox — proof the stored sandbox default
            # was honored rather than dropped for a connected host.
            await expect(page.get_by_test_id("new-chat-landing-host-chip")).to_have_attribute(
                "aria-label", re.compile(re.escape("Sandbox")), timeout=15_000
            )

            await page.get_by_test_id("new-chat-landing-input").fill("start here")
            await page.get_by_test_id("new-chat-landing-submit").click()

            await _wait_until(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            # A sandbox create is managed (server provisions the host); no host_id.
            assert body.get("host_type") == "managed", body
            assert "host_id" not in body or body["host_id"] is None, body
        finally:
            await browser.close()


def test_composer_files_new_session_into_project(seeded_session: tuple[str, str]) -> None:
    """A ``?project=`` visit files the new session at create time (born filed).

    The sidebar's per-project "new session" pencil lands here with the project
    pre-scoped. On Send the session must be *born filed*: the create
    ``POST /v1/sessions`` carries the legacy ``omni_project`` label so the
    sidebar groups the new row under its project from its first appearance —
    instead of briefly showing it under the ungrouped "Sessions" section until
    the follow-up ``project_id`` move (``moveConversationToProject``) catches up
    in the search-indexed session list. The sidebar, the ``?project=`` folder
    query, and the project list all dual-read membership from this label OR the
    first-class ``project_id`` the move then sets, so there is no ungrouped
    window either way.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_born_filed(base_url, session_id))


async def _drive_born_filed(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []

            async def handle_hosts(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_hosts_body()
                )

            async def handle_agents(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_agents_body()
                )

            async def handle_projects_list(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_projects_list_body()
                )

            async def handle_project_config(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=_project_config_body()
                )

            async def handle_events(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"queued": True, "item_id": "ci_e2e"}),
                )

            async def handle_sessions(route: Route) -> None:
                if route.request.method == "POST":
                    create_bodies.append(route.request.post_data_json)
                    await route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({"id": session_id}),
                    )
                else:
                    await route.continue_()

            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=json.dumps({"data": []})
                )

            await page.route("**/v1/hosts", handle_hosts)
            await stub_empty_host_picker_data(page, _HOST_ID)
            await page.route("**/v1/agents", handle_agents)
            await page.route("**/v1/sessions/projects", handle_projects_list)
            await page.route(_PROJECT_CFG_RE, handle_project_config)
            await _stub_project_host_roots(page, _project_config_body())
            await page.route("**/v1/sessions/*/events", handle_events)
            await page.route(_SESSIONS_RE, handle_sessions)
            await page.route(
                re.compile(r"/v1/sessions\?(?!.*pinned=).*visibility=mine"), handle_agent_scan
            )
            await page.route(OWN_AGENTS, lambda route: route.fulfill(json={"data": []}))

            # The per-project pencil destination: the composer lands pre-scoped
            # to this project (no interaction needed to file into it).
            await page.goto(f"{base_url}/?project={_PROJECT_NAME}")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            await page.get_by_test_id("new-chat-landing-input").fill("write the docs")
            await page.get_by_test_id("new-chat-landing-submit").click()

            await _wait_until(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            assert body["host_id"] == _HOST_ID, body
            # Born filed, first-class: the create names the project so the
            # server files it at insert. No legacy omni_project label and no
            # follow-up move — the row is a member from its first appearance,
            # with no window where it exists unfiled.
            assert body["project_id"] == _PROJECT_ID, body
            assert (body.get("labels") or {}).get("omni_project") is None, body
        finally:
            await browser.close()


# The worktree-list endpoint the composer probes for the seeded workspace.
_WORKTREES_RE = re.compile(r"/v1/hosts/[^/]+/worktrees")
# A git repo whose main tree is the seeded workspace — the composer's worktree
# probe reads this to decide the workspace is a git repo and can host a worktree.
_GIT_REPO = "/work/omnigent"


def _git_repo_worktrees_body() -> str:
    """``GET /v1/hosts/{id}/worktrees`` — a plain git repo (one main tree).

    The composer only auto-seeds a worktree when the workspace is a git repo,
    which it detects by a returned ``is_main`` entry."""
    return json.dumps(
        {
            "object": "list",
            "data": [
                {"path": _GIT_REPO, "branch": "main", "is_main": True, "detached": False},
            ],
        }
    )


def _plain_config_body() -> str:
    """``GET /v1/projects/{id}`` config that sets host/workspace but no worktree
    preference — so the effective worktree default falls through to the global."""
    return json.dumps(
        {
            "id": _PROJECT_ID,
            "name": _PROJECT_NAME,
            "config": {"host_id": _HOST_ID, "workspace": _GIT_REPO},
        }
    )


def _host_roots_body(config_body: str) -> str:
    """``GET /v1/projects/{id}/host-roots`` derived from the test's own config.

    Mirrors the server: a root needs a real (non-sandbox) config host with a
    workspace; the default host follows the config host alone, so a host
    without a workspace still seeds the host but no directory.
    """
    config = json.loads(config_body).get("config") or {}
    host_id = config.get("host_id")
    if host_id == _SANDBOX_CHOICE:
        host_id = None
    workspace = config.get("workspace")
    return json.dumps(
        {
            "roots": (
                [{"host_id": host_id, "workspace": workspace, "source": "config"}]
                if host_id and workspace
                else []
            ),
            "default_host_id": host_id,
            "default_host_reason": "config" if host_id else "none",
        }
    )


async def _stub_project_host_roots(page, config_body: str) -> None:
    """Serve host-roots derived from *config_body*.

    Must be registered after the config route: ``_PROJECT_CFG_RE`` swallows the
    host-roots URL too, and Playwright tries the most recently registered match
    first.
    """

    async def handle_host_roots(route: Route) -> None:
        await route.fulfill(
            status=200, content_type="application/json", body=_host_roots_body(config_body)
        )

    await page.route(_PROJECT_HOST_ROOTS_RE, handle_host_roots)


async def _route_composer_stubs(
    page,
    *,
    config_body: str,
    create_bodies: list[dict[str, Any]],
    session_id: str,
) -> None:
    """Wire the standard composer stubs (hosts/agents/projects/config/host-roots/
    worktrees/create). ``create_bodies`` captures the create POST — the thing
    under test."""

    async def handle_hosts(route: Route) -> None:
        await route.fulfill(status=200, content_type="application/json", body=_hosts_body())

    async def handle_agents(route: Route) -> None:
        await route.fulfill(status=200, content_type="application/json", body=_agents_body())

    async def handle_projects_list(route: Route) -> None:
        await route.fulfill(
            status=200, content_type="application/json", body=_projects_list_body()
        )

    async def handle_project_config(route: Route) -> None:
        await route.fulfill(status=200, content_type="application/json", body=config_body)

    async def handle_worktrees(route: Route) -> None:
        await route.fulfill(
            status=200, content_type="application/json", body=_git_repo_worktrees_body()
        )

    async def handle_events(route: Route) -> None:
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"queued": True, "item_id": "ci_e2e"}),
        )

    async def handle_sessions(route: Route) -> None:
        if route.request.method == "POST":
            create_bodies.append(route.request.post_data_json)
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"id": session_id}),
            )
        else:
            await route.continue_()

    async def handle_agent_scan(route: Route) -> None:
        await route.fulfill(
            status=200, content_type="application/json", body=json.dumps({"data": []})
        )

    await page.route("**/v1/hosts", handle_hosts)
    await page.route("**/v1/agents", handle_agents)
    await page.route("**/v1/sessions/projects", handle_projects_list)
    await page.route(_PROJECT_CFG_RE, handle_project_config)
    await _stub_project_host_roots(page, config_body)
    await page.route(_WORKTREES_RE, handle_worktrees)
    await page.route("**/v1/sessions/*/events", handle_events)
    await page.route(_SESSIONS_RE, handle_sessions)
    await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)


def test_typed_picker_preserves_posix_trailing_space_in_create(
    seeded_session: tuple[str, str],
) -> None:
    """A legal trailing space survives the rendered picker and create request."""
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_typed_picker_trailing_space(base_url, session_id))


async def _drive_typed_picker_trailing_space(base_url: str, session_id: str) -> None:
    exact_workspace = "/work/trailing-space "
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            await _route_composer_stubs(
                page,
                config_body=_plain_config_body(),
                create_bodies=create_bodies,
                session_id=session_id,
            )

            async def handle_filesystem(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "object": "list",
                            "data": [
                                {
                                    "name": "child",
                                    "path": f"{exact_workspace}/child",
                                    "type": "directory",
                                    "bytes": None,
                                    "modified_at": 0,
                                }
                            ],
                            "has_more": False,
                        }
                    ),
                )

            # Registered after the broad host route so the picker receives a
            # real rendered listing for its exact typed directory.
            await page.route("**/v1/hosts/*/filesystem*", handle_filesystem)
            await page.goto(f"{base_url}/?project={_PROJECT_NAME}")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            await open_landing_workspace_picker(page)
            path_input = page.get_by_test_id("workspace-picker-path-input")
            await path_input.fill(f"  {exact_workspace}")
            await path_input.press("Enter")
            await expect(path_input).to_have_value(exact_workspace)
            await commit_landing_workspace_picker(page)

            await page.get_by_test_id("new-chat-landing-input").fill("keep the exact cwd")
            await page.get_by_test_id("new-chat-landing-submit").click()

            await _wait_until(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            assert body["host_id"] == _HOST_ID, body
            assert body["workspace"] == exact_workspace, body
        finally:
            await browser.close()
