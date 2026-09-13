"""Browser e2e for the project collaboration settings section.

The collaboration surface (``web/src/shell/ProjectCollaborationSection.tsx``,
reached inside the Project settings dialog) reads and writes the
``/v1/projects/{id}/collaboration`` family of routes, which only exist when
the deployment enables the ``project_assignments`` feature.

The shared ``live_server`` runs with the feature off and registers no host,
so both are stubbed at the browser layer: ``/v1/info`` is fetched for real
and re-served with ``features.project_assignments = true`` (the
``test_usage_page_feature.py`` precedent), and ``/v1/hosts`` serves one
online host (the ``test_browse_outside_workspace.py`` precedent). The
collaboration routes themselves are stubbed with an in-test state dict that
mirrors the server contract (revision increments on PATCH; a stale
``expected_revision`` is a 409 ``conflict``).

The project itself is real (created via ``POST /v1/projects``), so the
dialog's own config fetch and the folder kebab are the production paths.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import fetch_with_retry

_HOST_ID = "host_e2e_collab"
_BAD_WORKSPACE = "/does/not/exist"
_BAD_PATH_MESSAGE = "host stat failed for path '/does/not/exist': No such file or directory"


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


def _stub_feature_on(page: Page) -> None:
    """Re-serve the real ``/v1/info`` with ``project_assignments`` enabled."""

    def handle_info(route: Route) -> None:
        response = fetch_with_retry(route)
        payload = response.json()
        payload.setdefault("features", {})["project_assignments"] = True
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    page.route(re.compile(r"/v1/info(\?|$)"), handle_info)


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

    page.route(re.compile(r"/v1/hosts(\?|$)"), handle_hosts)


def _stub_collaboration(
    page: Page,
    patch_bodies: list[dict[str, Any]],
    repo_puts: list[dict[str, Any]],
    binding_puts: list[dict[str, Any]],
    unexpected: list[dict[str, Any]],
) -> None:
    """Serve the collaboration routes the scenarios use from an in-test state dict.

    Mirrors the server contract: PATCH bumps ``revision`` and rejects a stale
    ``expected_revision`` with a 409 ``conflict``; a binding add for a path
    the host cannot stat is a 400 ``invalid_input`` with the server's message.
    Only the methods and paths the scenarios use are served — anything else
    is recorded in ``unexpected`` (asserted empty at the end of the test).
    """

    state: dict[str, Any] = {
        "enabled": False,
        "revision": 1,
        "repositories": {},
    }

    def snapshot() -> dict[str, Any]:
        return {
            "enabled": state["enabled"],
            "revision": state["revision"],
            "repositories": list(state["repositories"].values()),
            "bindings": [],
            "problems": [],
        }

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
        method = route.request.method
        if not re.fullmatch(r"/v1/projects/[^/]+/collaboration", path):
            reject(route, method, path)
            return
        if method == "GET":
            route.fulfill(
                status=200,
                headers={"content-type": "application/json"},
                body=json.dumps(snapshot()),
            )
            return
        if method != "PATCH":
            reject(route, method, path)
            return
        body = json.loads(route.request.post_data or "{}")
        patch_bodies.append(body)
        if body.get("expected_revision") != state["revision"]:
            route.fulfill(
                status=409,
                headers={"content-type": "application/json"},
                body=json.dumps(
                    {
                        "error": {
                            "code": "conflict",
                            "message": "collaboration settings changed elsewhere",
                        }
                    }
                ),
            )
            return
        state["enabled"] = body["enabled"]
        state["revision"] += 1
        route.fulfill(
            status=200,
            headers={"content-type": "application/json"},
            body=json.dumps({"enabled": state["enabled"], "revision": state["revision"]}),
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

    def handle_bindings(route: Route) -> None:
        path = urlparse(route.request.url).path
        method = route.request.method
        match = re.fullmatch(r"/v1/projects/([^/]+)/hosts/([^/]+)/bindings/([^/]+)", path)
        if method != "PUT" or match is None:
            reject(route, method, path)
            return
        body = json.loads(route.request.post_data or "{}")
        binding_puts.append({"path": path, "body": body})
        if body.get("workspace") != _BAD_WORKSPACE:
            reject(route, method, path)
            return
        route.fulfill(
            status=400,
            headers={"content-type": "application/json"},
            body=json.dumps({"error": {"code": "invalid_input", "message": _BAD_PATH_MESSAGE}}),
        )

    page.route(re.compile(r"/v1/projects/[^/]+/collaboration(\?|$)"), handle_collaboration)
    page.route(re.compile(r"/v1/projects/[^/]+/repositories/[^/?]+(\?|$)"), handle_repositories)
    page.route(re.compile(r"/v1/projects/[^/]+/hosts/.+/bindings/.+"), handle_bindings)


def test_collaboration_enable_repo_and_rejected_binding(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Enable collaboration, register a repo, and surface a rejected binding path.

    The toggle PATCHes ``{enabled, expected_revision}`` off the loaded
    revision; the repository add registers ``web``; the binding add for a
    path the host cannot stat fails with the server's 400 message verbatim
    and leaves no binding row behind.
    """
    base_url, session_id = seeded_session
    project = f"Project {uuid.uuid4().hex[:6]}"
    project_id = _create_project(base_url, project)
    patch_bodies: list[dict[str, Any]] = []
    repo_puts: list[dict[str, Any]] = []
    binding_puts: list[dict[str, Any]] = []
    unexpected: list[dict[str, Any]] = []

    _stub_feature_on(page)
    _stub_single_host(page)
    _stub_collaboration(page, patch_bodies, repo_puts, binding_puts, unexpected)
    page.goto(f"{base_url}/c/{session_id}")

    _open_project_settings(page, project)

    # The collaboration switch starts OFF (revision 1 in the stub).
    toggle = page.get_by_test_id("project-collaboration-enabled")
    expect(toggle).to_be_visible()
    expect(toggle).to_have_attribute("data-state", "unchecked")

    # Flip it ON — the PATCH carries the loaded revision.
    toggle.click()
    expect(toggle).to_have_attribute("data-state", "checked")
    expect(page.get_by_test_id("project-collaboration-repo-add")).to_be_enabled()
    assert patch_bodies[0] == {"enabled": True, "expected_revision": 1}

    # Register a repository (manifest left blank → the server default applies).
    page.get_by_test_id("project-collaboration-repo-name").fill("web")
    page.get_by_test_id("project-collaboration-repo-url").fill("https://example.com/web.git")
    page.get_by_test_id("project-collaboration-repo-add").click()
    expect(page.get_by_test_id("project-collaboration-repo-remove-web")).to_be_visible()

    # A binding whose workspace the host rejects: the server's message shows
    # verbatim and no binding row appears.
    page.get_by_test_id("project-collaboration-binding-workspace").fill(_BAD_WORKSPACE)
    page.get_by_test_id("project-collaboration-binding-add").click()
    expect(page.get_by_test_id("project-collaboration-error")).to_have_text(_BAD_PATH_MESSAGE)
    expect(
        page.get_by_test_id(f"project-collaboration-binding-verify-{_HOST_ID}-primary")
    ).to_have_count(0)

    # Exact request contract: the enable PATCH, the repository PUT body, and
    # the binding PUT path + body. No handler saw an unexpected method/path.
    assert patch_bodies == [{"enabled": True, "expected_revision": 1}]
    assert repo_puts == [
        {
            "path": f"/v1/projects/{project_id}/repositories/web",
            "body": {
                "remote_url": "https://example.com/web.git",
                "default_branch": "main",
            },
        }
    ]
    assert binding_puts == [
        {
            "path": f"/v1/projects/{project_id}/hosts/{_HOST_ID}/bindings/primary",
            "body": {
                "workspace": _BAD_WORKSPACE,
                "repository_name": "web",
                "is_primary": True,
            },
        }
    ]
    assert unexpected == []


def test_collaboration_section_absent_when_feature_is_off(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """With the real ``/v1/info`` (feature off), the section never renders."""
    base_url, session_id = seeded_session
    project = f"Project {uuid.uuid4().hex[:6]}"
    _create_project(base_url, project)

    page.goto(f"{base_url}/c/{session_id}")

    _open_project_settings(page, project)
    expect(page.get_by_test_id("project-collaboration-section")).to_have_count(0)
