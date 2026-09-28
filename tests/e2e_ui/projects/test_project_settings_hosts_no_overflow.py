"""UI: the Project settings Hosts block never scrolls horizontally.

The Hosts block renders one row per project host entry — a name, a directory
(often a long path), a summary chip, and edit / delete controls — with a detail
pane at >= md and a single-open accordion below md. On a phone-width viewport
the dialog is capped to the screen, so the rows and the dialog body must shrink
with it instead of pushing a horizontal scrollbar.

The e2e runner registers no host, so the project's config, entries, and the
host list are faked; the dialog itself is the real SPA. The host list carries a
third host with no entry so the "Add host" control renders.
"""

from __future__ import annotations

import re
import uuid

import httpx
from playwright.sync_api import Locator, Page, expect

# 200 characters, to break any min-content sizing the row grid relies on.
_LONG_PATH = "/" + "x" * 65 + "/" + "y" * 65 + "/" + "z" * 65 + "ab"

_DESKTOP = {"width": 1280, "height": 800}
_PHONE = {"width": 390, "height": 844}

_HOST_A = "host_e2e_fit_a"
_HOST_B = "host_e2e_fit_b"
_HOST_C = "host_e2e_fit_c"


def _create_project(base_url: str, name: str) -> str:
    """Create an empty first-class project via the API; return its id."""
    resp = httpx.post(f"{base_url}/v1/projects", json={"name": name}, timeout=10.0)
    resp.raise_for_status()
    return resp.json()["id"]


def _stub_project_routes(page: Page, project_id: str, project_name: str) -> None:
    """Answer the project's config / entries and the host list with fixtures."""
    page.route(
        "**/v1/hosts",
        lambda route: route.fulfill(
            json={
                "hosts": [
                    {
                        "host_id": _HOST_A,
                        "name": "fit-host-a",
                        "owner": "e2e",
                        "status": "online",
                    },
                    {
                        "host_id": _HOST_B,
                        "name": "fit-host-b",
                        "owner": "e2e",
                        "status": "online",
                    },
                    {
                        "host_id": _HOST_C,
                        "name": "fit-host-c",
                        "owner": "e2e",
                        "status": "online",
                    },
                ]
            }
        ),
    )
    page.route(
        re.compile(rf"/v1/projects/{re.escape(project_id)}$"),
        lambda route: route.fulfill(
            json={
                "id": project_id,
                "name": project_name,
                "config": {
                    "host_id": _HOST_A,
                    "calling_defaults": {
                        _HOST_A: {
                            "harnesses": {"codex": {"model": "gpt-6-sol", "effort": "high"}}
                        },
                        _HOST_B: {
                            "harnesses": {
                                "claude-native": {"model": "opus-5-5", "effort": "xhigh"}
                            }
                        },
                    },
                },
            }
        ),
    )
    page.route(
        re.compile(rf"/v1/projects/{re.escape(project_id)}/entries$"),
        lambda route: route.fulfill(
            json={
                "entries": [
                    {"host_id": _HOST_A, "workspace": _LONG_PATH, "updated_at": 0},
                    {"host_id": _HOST_B, "workspace": "/work/short-repo", "updated_at": 0},
                ]
            }
        ),
    )


def _open_project_settings(page: Page, project: str) -> None:
    """Open the folder kebab → "Project settings" for *project*."""
    actions = page.get_by_role("button", name=f"Project actions for {project}", exact=True)
    expect(actions).to_be_visible()
    actions.click()
    page.get_by_test_id("project-settings").click()
    # Save enables once the config and entry fetches settled.
    expect(page.get_by_test_id("project-settings-save")).to_be_enabled()


def _assert_fits_width(locator: Locator, label: str) -> None:
    """Assert *locator* has no horizontal overflow (1px subpixel tolerance)."""
    scroll_width, client_width = locator.evaluate("el => [el.scrollWidth, el.clientWidth]")
    assert scroll_width <= client_width + 1, (
        f"{label} overflows horizontally: scrollWidth {scroll_width} > clientWidth {client_width}"
    )


def _assert_no_overflow(page: Page, size: str) -> None:
    """Assert the dialog body, every host row, and the document fit the width."""
    _assert_fits_width(page.locator("#project-settings-defaults-form"), f"{size}: dialog body")
    for host_id in (_HOST_A, _HOST_B):
        _assert_fits_width(
            page.get_by_test_id(f"project-settings-entry-{host_id}"), f"{size}: host row {host_id}"
        )
    _assert_fits_width(page.get_by_test_id("project-settings-all-hosts"), f"{size}: all-hosts row")
    document_width, viewport_width = page.evaluate(
        "() => [document.documentElement.scrollWidth, window.innerWidth]"
    )
    assert document_width <= viewport_width + 1, (
        f"{size}: document overflows horizontally: scrollWidth {document_width} > "
        f"innerWidth {viewport_width}"
    )


def _assert_long_path_truncated(page: Page) -> None:
    """The long directory shows truncated, with the full path in its title."""
    path = page.get_by_test_id(f"project-settings-host-path-{_HOST_A}")
    expect(path).to_have_text(_LONG_PATH)
    expect(path).to_have_attribute("title", _LONG_PATH)
    scroll_width, client_width = path.evaluate("el => [el.scrollWidth, el.clientWidth]")
    assert scroll_width > client_width, (
        f"long path should be clipped by truncation: scrollWidth {scroll_width} <= "
        f"clientWidth {client_width}"
    )
    text_overflow = path.evaluate("el => getComputedStyle(el).textOverflow")
    assert text_overflow == "ellipsis", (
        f"long path should truncate with an ellipsis, got {text_overflow}"
    )


def _assert_row_controls(page: Page) -> None:
    """Every host row exposes edit / delete, and Add host is offered."""
    for host_id in (_HOST_A, _HOST_B):
        expect(page.get_by_test_id(f"project-settings-entry-edit-{host_id}")).to_be_visible()
        expect(page.get_by_test_id(f"project-settings-entry-remove-{host_id}")).to_be_visible()
    add_host = page.get_by_test_id("project-settings-add-host")
    expect(add_host).to_be_visible()
    expect(add_host).to_contain_text("Add host")


def test_project_settings_hosts_no_horizontal_overflow(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    base_url, session_id = seeded_session
    project = f"Fit {uuid.uuid4().hex[:6]}"
    project_id = _create_project(base_url, project)
    _stub_project_routes(page, project_id, project)

    page.set_viewport_size(_DESKTOP)
    page.goto(f"{base_url}/c/{session_id}")
    _open_project_settings(page, project)

    listing = page.get_by_test_id("project-settings-directories")
    detail_a = page.get_by_test_id(f"project-settings-host-detail-{_HOST_A}")
    detail_b = page.get_by_test_id(f"project-settings-host-detail-{_HOST_B}")

    # Desktop: list and the selected row's detail share the pane.
    expect(listing).to_be_visible()
    expect(detail_a).to_be_visible()
    expect(detail_b).to_have_count(0)
    _assert_row_controls(page)
    _assert_no_overflow(page, "desktop")
    _assert_long_path_truncated(page)

    # Phone: the dialog caps to the viewport; the accordion shows one detail.
    page.set_viewport_size(_PHONE)
    _assert_row_controls(page)
    _assert_no_overflow(page, "phone")
    _assert_long_path_truncated(page)

    page.get_by_test_id(f"project-settings-entry-{_HOST_B}").click()
    expect(detail_b).to_be_visible()
    expect(detail_a).to_have_count(0)
    _assert_no_overflow(page, "phone: second row open")

    page.get_by_test_id(f"project-settings-entry-{_HOST_A}").click()
    expect(detail_a).to_be_visible()
    expect(detail_b).to_have_count(0)
