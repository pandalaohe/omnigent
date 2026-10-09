"""Browser e2e for the project Code settings tab.

The Code surface (``web/src/shell/ProjectCodeSection.tsx``, reached inside the
Project settings dialog) reads and writes the ``/v1/projects/{id}`` routes for
repositories, entries and host roots, plus the host folder-facts route.

The shared ``live_server`` registers no host, so the host, its filesystem and
its folder facts are stubbed at the browser layer (the
``test_browse_outside_workspace.py`` precedent). The project itself is real
(created via ``POST /v1/projects``), so the dialog's own config fetch and the
folder kebab are the production paths.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
from playwright.sync_api import Page, Route, expect

_HOST_ID = "host_e2e_code"
_WORKSPACE = "/opt/work/omnigent/fork/wt"
_REPO_URL = "https://git.example.test/team/web.git"


def _create_project(base_url: str, name: str) -> str:
    """Create an empty first-class project via the API; return its id."""
    resp = httpx.post(f"{base_url}/v1/projects", json={"name": name}, timeout=10.0)
    resp.raise_for_status()
    return resp.json()["id"]


def _open_project_settings(page: Page, project: str) -> None:
    """Open the folder kebab → "Project settings" for *project*."""
    actions = page.get_by_role("button", name=f"Project actions for {project}", exact=True)
    expect(actions).to_be_visible()
    actions.click()
    page.get_by_test_id("project-settings").click()
    expect(page.get_by_test_id("project-settings-save")).to_be_enabled()


def _stub_single_host(page: Page) -> None:
    """Serve one online host; the e2e server registers none."""

    def handle_hosts(route: Route) -> None:
        if route.request.method != "GET":
            route.continue_()
            return
        route.fulfill(
            status=200,
            headers={"content-type": "application/json"},
            body=json.dumps(
                {"hosts": [{"host_id": _HOST_ID, "name": "e2e-host", "status": "online"}]}
            ),
        )

    def handle_filesystem(route: Route) -> None:
        route.fulfill(
            status=200,
            headers={"content-type": "application/json"},
            body=json.dumps(
                {
                    "object": "list",
                    "data": [
                        {
                            "name": "wt",
                            "path": _WORKSPACE,
                            "type": "directory",
                            "bytes": None,
                            "modified_at": None,
                        }
                    ],
                    "has_more": False,
                }
            ),
        )

    page.route(re.compile(r"/v1/hosts(\?|$)"), handle_hosts)
    page.route(re.compile(rf"/v1/hosts/{re.escape(_HOST_ID)}/filesystem"), handle_filesystem)


def _stub_code_routes(
    page: Page,
    repo_puts: list[dict[str, Any]],
    entry_puts: list[dict[str, Any]],
    facts_reads: list[dict[str, Any]],
    unexpected: list[dict[str, Any]],
) -> None:
    """Serve the Code tab's routes from an in-test state dict.

    Only the methods and paths the scenario uses are served — anything else is
    recorded in ``unexpected`` (asserted empty at the end of the test). The
    host-roots answer mirrors the stored entry, so "New sessions open in"
    appears only after the project folder is saved.
    """

    state: dict[str, Any] = {"repositories": {}, "entries": {}}

    def reject(route: Route, method: str, path: str) -> None:
        unexpected.append({"method": method, "path": path})
        route.fulfill(
            status=500,
            headers={"content-type": "application/json"},
            body=json.dumps(
                {"error": {"code": "unexpected", "message": f"unexpected {method} {path}"}}
            ),
        )

    def handle_collaboration(route: Route) -> None:
        path = urlparse(route.request.url).path
        if route.request.method != "GET" or not re.fullmatch(
            r"/v1/projects/[^/]+/collaboration", path
        ):
            reject(route, route.request.method, path)
            return
        route.fulfill(
            status=200,
            headers={"content-type": "application/json"},
            body=json.dumps(
                {
                    "repositories": list(state["repositories"].values()),
                    "bindings": [],
                    "problems": [],
                    "setup_outcomes": [],
                }
            ),
        )

    def handle_repositories(route: Route) -> None:
        path = urlparse(route.request.url).path
        method = route.request.method
        match = re.fullmatch(r"/v1/projects/([^/]+)/repositories/([^/]+)", path)
        if method != "PUT" or match is None:
            reject(route, method, path)
            return
        project_id = unquote(match.group(1))
        name = unquote(match.group(2))
        body = json.loads(route.request.post_data or "{}")
        repo_puts.append({"path": path, "body": body})
        repo = {
            "id": f"repo_{name}",
            "project_id": project_id,
            "name": name,
            "role": body.get("role", "related"),
            "remote_url": body["remote_url"],
            "default_branch": body["default_branch"],
            "context_manifest_path": body.get(
                "context_manifest_path", ".agents/project/manifest.json"
            ),
            "revision": 1,
            "created_at": 1,
            "updated_at": None,
        }
        state["repositories"][name] = repo
        route.fulfill(
            status=200,
            headers={"content-type": "application/json"},
            body=json.dumps(repo),
        )

    def handle_entries(route: Route) -> None:
        path = urlparse(route.request.url).path
        if route.request.method != "GET" or not re.fullmatch(r"/v1/projects/[^/]+/entries", path):
            reject(route, route.request.method, path)
            return
        route.fulfill(
            status=200,
            headers={"content-type": "application/json"},
            body=json.dumps({"entries": list(state["entries"].values())}),
        )

    def handle_entry(route: Route) -> None:
        path = urlparse(route.request.url).path
        method = route.request.method
        match = re.fullmatch(r"/v1/projects/([^/]+)/entries/([^/]+)", path)
        if method != "PUT" or match is None:
            reject(route, method, path)
            return
        host_id = unquote(match.group(2))
        body = json.loads(route.request.post_data or "{}")
        entry_puts.append({"path": path, "body": body})
        entry = {
            "host_id": host_id,
            "workspace": body["workspace"],
            "updated_at": 1,
            "checked": True,
        }
        state["entries"][host_id] = entry
        route.fulfill(
            status=200,
            headers={"content-type": "application/json"},
            body=json.dumps(entry),
        )

    def handle_host_roots(route: Route) -> None:
        path = urlparse(route.request.url).path
        if route.request.method != "GET" or not re.fullmatch(
            r"/v1/projects/[^/]+/host-roots(\?|$)", path
        ):
            reject(route, route.request.method, path)
            return
        entry = state["entries"].get(_HOST_ID)
        roots = (
            [
                {
                    "host_id": _HOST_ID,
                    "workspace": entry["workspace"],
                    "source": "entry",
                    "checkout": entry["workspace"],
                }
            ]
            if entry
            else []
        )
        route.fulfill(
            status=200,
            headers={"content-type": "application/json"},
            body=json.dumps(
                {
                    "roots": roots,
                    "default_host_id": _HOST_ID if roots else None,
                    "default_host_reason": "single_root" if roots else "none",
                }
            ),
        )

    def handle_folder_facts(route: Route) -> None:
        path = urlparse(route.request.url).path
        if route.request.method != "GET" or not re.fullmatch(
            rf"/v1/hosts/{re.escape(_HOST_ID)}/folder-facts", path
        ):
            reject(route, route.request.method, path)
            return
        facts_reads.append({"path": path, "query": route.request.url})
        route.fulfill(
            status=200,
            headers={"content-type": "application/json"},
            body=json.dumps(
                {
                    "exists": True,
                    "is_dir": True,
                    "is_repo": True,
                    "toplevel": _WORKSPACE,
                    "branch": "main",
                    "head": "abc1234def5678901234",
                    "detached": False,
                    "dirty": False,
                    "remotes": [{"name": "origin", "url": _REPO_URL}],
                    "setup_command_configured": False,
                    "error": None,
                }
            ),
        )

    def handle_agent_note(route: Route) -> None:
        path = urlparse(route.request.url).path
        if route.request.method != "GET" or not re.fullmatch(
            r"/v1/projects/[^/]+/hosts/[^/]+/agent-code-note", path
        ):
            reject(route, route.request.method, path)
            return
        route.fulfill(
            status=200,
            headers={"content-type": "application/json"},
            body=json.dumps({"text": None, "delivered": False, "reason": None}),
        )

    page.route(re.compile(r"/v1/projects/[^/]+/collaboration(\?|$)"), handle_collaboration)
    page.route(re.compile(r"/v1/projects/[^/]+/repositories/[^/?]+(\?|$)"), handle_repositories)
    page.route(re.compile(r"/v1/projects/[^/]+/entries(\?|$)"), handle_entries)
    page.route(re.compile(r"/v1/projects/[^/]+/entries/[^/?]+(\?|$)"), handle_entry)
    page.route(re.compile(r"/v1/projects/[^/]+/host-roots(\?|$)"), handle_host_roots)
    page.route(re.compile(r"/v1/hosts/[^/]+/folder-facts(\?|$)"), handle_folder_facts)
    page.route(re.compile(r"/v1/projects/[^/]+/hosts/[^/]+/agent-code-note"), handle_agent_note)


def test_code_tab_repository_and_project_folder(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Add a repository by address, set the host's project folder, see the root.

    The repository is registered from a typed git address (no folder facts);
    the project folder is picked on the stubbed host through the shared folder
    browser, and the entry PUT plus the host-roots read make the
    "New sessions open in" line name the saved workspace.
    """
    base_url, session_id = seeded_session
    project = f"Project {uuid.uuid4().hex[:6]}"
    project_id = _create_project(base_url, project)
    repo_puts: list[dict[str, Any]] = []
    entry_puts: list[dict[str, Any]] = []
    facts_reads: list[dict[str, Any]] = []
    unexpected: list[dict[str, Any]] = []

    _stub_single_host(page)
    _stub_code_routes(page, repo_puts, entry_puts, facts_reads, unexpected)
    page.goto(f"{base_url}/c/{session_id}")

    _open_project_settings(page, project)
    page.get_by_role("tab", name="Code").click()

    # Register a repository from its git address.
    page.get_by_test_id("project-code-add-repo-open").click()
    page.get_by_test_id("project-code-add-by-address").click()
    page.get_by_test_id("project-code-add-url").fill(_REPO_URL)
    page.get_by_test_id("project-code-add-name").fill("web")
    page.get_by_test_id("project-code-add-submit").click()
    expect(page.get_by_test_id("project-code-repo-remove-web")).to_be_visible()

    # Give the stubbed host a card, then set its project folder through the
    # shared browser.
    page.get_by_test_id("project-code-add-host-picker").select_option(_HOST_ID)
    page.get_by_test_id(f"project-code-entry-browse-{_HOST_ID}").click()
    picker = page.get_by_test_id("workspace-picker")
    expect(picker).to_be_visible()
    picker.get_by_test_id("workspace-picker-entry-wt").click()
    picker.get_by_test_id("workspace-picker-select").click()

    # The saved entry makes host-roots name where new sessions open and what
    # new worktrees fork from.
    expect(page.get_by_test_id(f"project-code-root-{_HOST_ID}")).to_contain_text(
        f"New sessions open in: {_WORKSPACE}"
    )
    expect(page.get_by_test_id(f"project-code-root-{_HOST_ID}")).to_contain_text(
        f"New worktrees come from: {_WORKSPACE}"
    )

    # Exact request contract: the repository PUT body, the entry PUT path +
    # body, and a folder-facts read for the saved folder. No handler saw an
    # unexpected method/path.
    assert repo_puts == [
        {
            "path": f"/v1/projects/{project_id}/repositories/web",
            "body": {
                "remote_url": _REPO_URL,
                "default_branch": "main",
                "role": "code",
            },
        }
    ]
    assert entry_puts == [
        {
            "path": f"/v1/projects/{project_id}/entries/{_HOST_ID}",
            "body": {"workspace": _WORKSPACE},
        }
    ]
    assert facts_reads, "the saved folder's facts were read"
    assert unexpected == []
