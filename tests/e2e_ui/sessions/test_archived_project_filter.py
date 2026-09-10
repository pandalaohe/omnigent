"""API-backed project filtering and pagination in Settings and Archive Library.

Archived-only facets include projects absent from the active sidebar. Both
custom entry points replace bounded pages and navigate back without losing
the selected project. Synthetic sessions are removed after each test.
"""

from __future__ import annotations

import contextlib
import json
import uuid

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _build_hello_world_bundle, open_right_rail

# Reserved label key that stores project membership (see
# ``sqlalchemy_store.list_projects`` and ``web/src/lib/sessionListCache.ts``).
_PROJECT_LABEL_KEY = "omni_project"


def _seed_archived_session(base_url: str, *, title: str, project: str | None) -> str:
    """Create a session, file it under *project*, and archive it.

    Creation goes through the same multipart ``POST /v1/sessions`` path as
    the ``seeded_session`` fixture; a single ``PATCH`` then sets the title,
    the ``omni_project`` label, and the archived flag.

    :param base_url: The live server base URL.
    :param title: Unique title so the row is easy to spot among other tests'
        sessions on the shared server.
    :param project: Project name to file the session under, or ``None`` to
        leave it unfiled.
    :returns: The new session id.
    """
    create_resp = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", _build_hello_world_bundle(), "application/gzip")},
        timeout=30.0,
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]

    body: dict[str, object] = {"title": title, "archived": True}
    if project is not None:
        body["labels"] = {_PROJECT_LABEL_KEY: project}
    patch_resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json=body,
        timeout=10.0,
    )
    patch_resp.raise_for_status()
    return session_id


def _delete_sessions(base_url: str, session_ids: list[str]) -> None:
    """Best-effort cleanup so seeded sessions don't leak into other tests."""
    for session_id in session_ids:
        with contextlib.suppress(httpx.HTTPError):
            httpx.delete(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)


def _pick_project(page: Page, option_name: str) -> None:
    """Open the Project picker and select an option.

    :param option_name: A project name or the literal ``"All projects"``
        reset option, matched by its accessible name.
    """
    page.get_by_role("combobox", name="Filter archived sessions by project").click()
    option = page.get_by_role("option", name=option_name, exact=True)
    expect(option).to_be_visible()
    option.click()


def test_archived_project_filter_narrows_and_resets(
    page: Page,
    live_server: str,
) -> None:
    """Picking a project narrows the archived list; "All projects" restores it.

    Seeds two projects whose sessions are all archived (two rows in one, one
    in the other), then asserts:

    - both projects appear as picker options (the archived-only scan finds
      them even though ``list_projects`` omits all-archived projects);
    - selecting a project shows exactly its rows and hides the other's;
    - switching projects re-filters;
    - "All projects" restores the full list.
    """
    uniq = uuid.uuid4().hex[:6]
    proj_a = f"E2E Alpha {uniq}"
    proj_b = f"E2E Beta {uniq}"
    titles = {
        "a1": f"e2e-archfilter-a1-{uniq}",
        "a2": f"e2e-archfilter-a2-{uniq}",
        "b1": f"e2e-archfilter-b1-{uniq}",
    }
    session_ids: list[str] = []
    try:
        session_ids.append(_seed_archived_session(live_server, title=titles["a1"], project=proj_a))
        session_ids.append(_seed_archived_session(live_server, title=titles["a2"], project=proj_a))
        session_ids.append(_seed_archived_session(live_server, title=titles["b1"], project=proj_b))

        page.goto(f"{live_server}/settings/archived")
        rows = page.get_by_test_id("archived-row")

        # Unfiltered: all three rows are present (newest by updated_at, so
        # they sort onto the first page even on a shared server).
        for title in titles.values():
            expect(rows.filter(has_text=title)).to_have_count(1)

        # Both all-archived projects are offered as options.
        page.get_by_role("combobox", name="Filter archived sessions by project").click()
        expect(page.get_by_role("option", name=proj_a, exact=True)).to_be_visible()
        expect(page.get_by_role("option", name=proj_b, exact=True)).to_be_visible()
        page.get_by_role("option", name=proj_a, exact=True).click()

        # Filtered to A: exactly A's rows; B's row is gone.
        expect(rows.filter(has_text=titles["a1"])).to_have_count(1)
        expect(rows.filter(has_text=titles["a2"])).to_have_count(1)
        expect(rows.filter(has_text=titles["b1"])).to_have_count(0)
        expect(rows).to_have_count(2)

        # Switch to B: re-filters rather than accumulating.
        _pick_project(page, proj_b)
        expect(rows.filter(has_text=titles["b1"])).to_have_count(1)
        expect(rows.filter(has_text=titles["a1"])).to_have_count(0)
        expect(rows).to_have_count(1)

        # Reset restores the unfiltered list.
        _pick_project(page, "All projects")
        for title in titles.values():
            expect(rows.filter(has_text=title)).to_have_count(1)
    finally:
        _delete_sessions(live_server, session_ids)


@pytest.mark.parametrize("mode", ["settings", "library"])
def test_archived_project_filter_paginates(
    page: Page,
    seeded_session: tuple[str, str],
    mode: str,
) -> None:
    """Settings and the Archive Library page forward and back within a project."""
    live_server, active_session_id = seeded_session
    page_size = 20
    uniq = uuid.uuid4().hex[:6]
    project = f"E2E Paged {uniq}"
    total = page_size + 2
    session_ids: list[str] = []
    try:
        for i in range(total):
            session_ids.append(
                _seed_archived_session(
                    live_server,
                    title=f"e2e-archpage-{i:02d}-{uniq}",
                    project=project,
                )
            )

        if mode == "library":
            page.goto(f"{live_server}/c/{active_session_id}")
            open_right_rail(page)
            page.get_by_role("tab", name="Archive Library", exact=True).click()
            page.get_by_role("combobox", name="Filter archived sessions by project").click()
            page.get_by_role("option", name=project, exact=True).click()
            library = page.get_by_test_id("archive-library-rail")
            rows = library.get_by_role("option").filter(has_text="e2e-archpage-")
            expect(rows).to_have_count(page_size)
            library.get_by_role("button", name="Next", exact=True).click()
            expect(rows).to_have_count(2)
            expect(library.get_by_role("button", name="Next", exact=True)).to_be_disabled()
            library.get_by_role("button", name="Previous", exact=True).click()
            expect(rows).to_have_count(page_size)
        else:
            page.goto(f"{live_server}/settings/archived")
            _pick_project(page, project)
            rows = page.get_by_test_id("archived-row")
            expect(rows).to_have_count(page_size)
            next_page = page.get_by_test_id("archived-page-next")
            next_page.click()
            expect(rows).to_have_count(2)
            expect(next_page).to_be_disabled()
            page.get_by_test_id("archived-page-previous").click()
            expect(rows).to_have_count(page_size)
    finally:
        _delete_sessions(live_server, session_ids)
