import { conversation, conversationPage } from "@/test/sidebarMockHelpers";
import { renderSidebar } from "@/test/sidebarTestHelpers";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/hooks/useScopeCache", () => import("@/test/mockScopeCache"));

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";

import { TooltipProvider } from "@/components/ui/tooltip";
import { SidebarDataProvider } from "@/hooks/useSidebarData";
import type { Conversation } from "@/hooks/useConversations";
import type * as IdentityModule from "@/lib/identity";
import type { SidebarLayout } from "@/lib/sidebarLayout";
import { Sidebar } from "@/shell/Sidebar";

vi.mock("@/hooks/useHosts", () => ({
  useHosts: () => ({ data: [] }),
}));

const { fetchMock, recentRef } = vi.hoisted(() => ({
  fetchMock: vi.fn(),
  recentRef: { current: { status: 200, data: [] as Conversation[] } },
}));

vi.mock("@/lib/identity", async (importOriginal) => {
  const actual = await importOriginal<typeof IdentityModule>();
  return { ...actual, authenticatedFetch: fetchMock };
});

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 404 ? "Not Found" : "OK",
    json: async () => body,
  } as unknown as Response;
}

const { projectsRef, pinnedRef, orderRef, orderStatusRef } = vi.hoisted(() => ({
  projectsRef: {
    current: [] as { id: string | null; name: string; icon?: string | null }[],
  },
  pinnedRef: { current: [] as unknown[] },
  orderRef: {
    current: {
      sort_mode: "alphabetical" as "alphabetical" | "manual",
      ordered_project_ids: [] as string[],
    },
  },
  orderStatusRef: { current: 200 },
}));

vi.mock("@/hooks/useConversations", async () => {
  const { conversationHooksMock } = await import("@/test/sidebarMockHelpers");
  return {
    ...conversationHooksMock(),
    resolveOrCreateProjectId: vi.fn((name: string) => Promise.resolve(`p_${name}`)),
    useProjects: () => ({ data: projectsRef.current }),
    usePinnedConversations: () => ({
      data: { conversations: pinnedRef.current, filterHonored: true },
      isSuccess: true,
    }),
    useProjectSessions: () => ({
      data: undefined,
      isLoading: false,
      hasNextPage: false,
      isFetchingNextPage: false,
      fetchNextPage: vi.fn(),
    }),
  };
});
vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));

import { resolveOrCreateProjectId, useConversations } from "@/hooks/useConversations";

const useConversationsMock = vi.mocked(useConversations);

const PROJECTS = [
  { id: "p_alpha", name: "Alpha", icon: null },
  { id: "p_beta", name: "Beta", icon: null },
  { id: "p_gamma", name: "Gamma", icon: null },
];

const WORK_LAYOUT: SidebarLayout = {
  version: 1,
  sections: [
    { id: "sec_work", kind: "projects", name: "Work", maxRows: 10, projectIds: ["p_alpha"] },
    { id: "default-favorites", kind: "favorites", name: "Pinned", maxRows: null, items: [] },
    { id: "default-other-projects", kind: "other_projects", name: "Projects", maxRows: null },
    { id: "default-other-sessions", kind: "other_sessions", name: "Sessions", maxRows: null },
  ],
};

// A `projects` section holding nothing, so a folder can be dragged into it.
const EMPTY_WORK_LAYOUT: SidebarLayout = {
  version: 1,
  sections: [
    { id: "sec_work", kind: "projects", name: "Work", maxRows: null, projectIds: [] },
    { id: "default-favorites", kind: "favorites", name: "Pinned", maxRows: null, items: [] },
    { id: "default-other-projects", kind: "other_projects", name: "Projects", maxRows: null },
    { id: "default-other-sessions", kind: "other_sessions", name: "Sessions", maxRows: null },
  ],
};

const RECENT_LAYOUT: SidebarLayout = {
  version: 1,
  sections: [
    { id: "sec_recent", kind: "recent", name: "Recent", maxRows: null, count: 5 },
    { id: "default-other-projects", kind: "other_projects", name: "Projects", maxRows: null },
    { id: "default-other-sessions", kind: "other_sessions", name: "Sessions", maxRows: null },
  ],
};

// A recent section above a projects section, so one session renders twice.
const RECENT_FOLDER_LAYOUT: SidebarLayout = {
  version: 1,
  sections: [
    { id: "sec_recent", kind: "recent", name: "Recent", maxRows: null, count: 5 },
    { id: "sec_work", kind: "projects", name: "Work", maxRows: null, projectIds: ["p_alpha"] },
  ],
};

const LAYOUT_STORAGE_KEY = "omnigent:sidebar-layout";
const COLLAPSED_IDS_STORAGE_KEY = "omnigent:collapsed-sidebar-section-ids";
const LEGACY_COLLAPSED_STORAGE_KEY = "omnigent:collapsed-sidebar-sections";

function mockConversations(conversations: Conversation[]) {
  useConversationsMock.mockReturnValue(conversationPage(conversations));
}

function isBefore(a: HTMLElement, b: HTMLElement): boolean {
  return Boolean(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
}

function sectionOf(title: string): HTMLElement {
  const header = screen.getByText(title).closest("section");
  if (header === null) throw new Error(`No section for ${title}`);
  return header as HTMLElement;
}

function LocationProbe() {
  return <div data-testid="location">{useLocation().pathname}</div>;
}

/** The sidebar with a live `/c/:id` route so the switch hotkey sees an active row. */
function renderSidebarAt(path: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <SidebarDataProvider>
        <TooltipProvider>
          <MemoryRouter initialEntries={[path]}>
            <Routes>
              <Route
                path="/c/:conversationId"
                element={
                  <>
                    <LocationProbe />
                    <Sidebar open onClose={vi.fn()} />
                  </>
                }
              />
            </Routes>
          </MemoryRouter>
        </TooltipProvider>
      </SidebarDataProvider>
    </QueryClientProvider>,
  );
}

function recentCalls(): unknown[][] {
  return fetchMock.mock.calls.filter((call) => String(call[0]).includes("/v1/me/recent-sessions"));
}

beforeEach(() => {
  vi.clearAllMocks();
  useConversationsMock.mockReset();
  localStorage.clear();
  projectsRef.current = PROJECTS;
  pinnedRef.current = [];
  orderRef.current = { sort_mode: "alphabetical", ordered_project_ids: [] };
  orderStatusRef.current = 200;
  recentRef.current = { status: 200, data: [] };
  fetchMock.mockReset();
  fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/v1/me/recent-sessions")) {
      if (recentRef.current.status === 404) return jsonResponse({}, 404);
      return jsonResponse({
        data: recentRef.current.data,
        first_id: null,
        last_id: null,
        has_more: false,
      });
    }
    if (url.includes("/v1/projects/order")) {
      return jsonResponse(orderRef.current, orderStatusRef.current);
    }
    return jsonResponse({}, 404);
  });
  mockConversations([conversation("plain")]);
});

afterEach(cleanup);

describe("Sidebar sections", () => {
  it("renders the default layout as today's Pinned / Projects / Sessions", () => {
    pinnedRef.current = [conversation("pin_a"), conversation("pin_b")];
    mockConversations([
      conversation("pin_a", { updated_at: 3 }),
      conversation("pin_b", { updated_at: 2 }),
      conversation("plain", { updated_at: 1 }),
    ]);
    renderSidebar();

    const pinned = screen.getByText("Pinned");
    const projects = screen.getByText("Projects");
    const sessions = screen.getByText("Sessions");
    expect(isBefore(pinned, projects)).toBe(true);
    expect(isBefore(projects, sessions)).toBe(true);
    for (const name of ["Alpha", "Beta", "Gamma"]) {
      expect(screen.getByText(name)).toBeInTheDocument();
    }
    expect(screen.getByText("plain")).toBeInTheDocument();
  });

  it("creates a project section and moves a project into it", async () => {
    renderSidebar();

    fireEvent.pointerDown(within(sectionOf("Projects")).getByTestId("section-options"), {
      button: 0,
    });
    fireEvent.click(screen.getByTestId("new-section"));
    fireEvent.click(screen.getByTestId("new-section-kind-projects"));
    fireEvent.click(screen.getByTestId("new-section-continue"));

    const nameInput = screen.getByTestId("new-section-name") as HTMLInputElement;
    expect(nameInput.value).toBe("New section");
    fireEvent.change(nameInput, { target: { value: "Work" } });
    fireEvent.click(screen.getByTestId("new-section-create"));

    await waitFor(() => expect(screen.queryByTestId("new-section-dialog")).toBeNull());
    const stored = JSON.parse(localStorage.getItem(LAYOUT_STORAGE_KEY)!) as SidebarLayout;
    expect(stored.sections.map((section) => section.kind)).toEqual([
      "projects",
      "favorites",
      "other_projects",
      "other_sessions",
    ]);
    const work = stored.sections[0];
    expect(work.name).toBe("Work");
    expect(isBefore(screen.getByText("Work"), screen.getByText("Projects"))).toBe(true);
    expect(within(sectionOf("Work")).getByText("No projects in this section")).toBeInTheDocument();

    fireEvent.pointerDown(screen.getByLabelText("Project actions for Alpha"), { button: 0 });
    fireEvent.click(await screen.findByTestId("move-project-to-section"));
    fireEvent.click(await screen.findByTestId(`move-to-section-${work.id}`));

    await waitFor(() => expect(within(sectionOf("Work")).getByText("Alpha")).toBeInTheDocument());
    expect(within(sectionOf("Projects")).queryByText("Alpha")).toBeNull();
    expect(within(sectionOf("Projects")).getByText("Beta")).toBeInTheDocument();
    expect(within(sectionOf("Projects")).getByText("Gamma")).toBeInTheDocument();

    const storedAfter = JSON.parse(localStorage.getItem(LAYOUT_STORAGE_KEY)!) as SidebarLayout;
    expect(storedAfter.sections[0].id).toBe(work.id);
    expect(storedAfter.sections[0].projectIds).toEqual(["p_alpha"]);
    expect(storedAfter.sections[1].kind).toBe("favorites");
  });

  it("removes a project section and returns its projects to Projects", async () => {
    localStorage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify(WORK_LAYOUT));
    renderSidebar();

    expect(within(sectionOf("Work")).getByText("Alpha")).toBeInTheDocument();
    expect(within(sectionOf("Projects")).queryByText("Alpha")).toBeNull();

    fireEvent.pointerDown(within(sectionOf("Work")).getByTestId("section-options"), { button: 0 });
    fireEvent.click(screen.getByTestId("remove-section"));

    await waitFor(() => expect(screen.queryByText("Work")).toBeNull());
    expect(within(sectionOf("Projects")).getByText("Alpha")).toBeInTheDocument();
    const stored = JSON.parse(localStorage.getItem(LAYOUT_STORAGE_KEY)!) as SidebarLayout;
    expect(stored.sections.some((section) => section.id === "sec_work")).toBe(false);
  });

  it("caps a section body and makes it scroll", () => {
    localStorage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify(WORK_LAYOUT));
    renderSidebar();

    const body = within(sectionOf("Work")).getByTestId("sidebar-section-body");
    expect(body).toHaveStyle({ maxHeight: "290px" });
    expect(body).toHaveClass("overflow-y-auto");
  });

  it("writes collapse state only under the section-id key", () => {
    renderSidebar();

    fireEvent.click(screen.getByRole("button", { name: /Sessions/ }));

    expect(JSON.parse(localStorage.getItem(COLLAPSED_IDS_STORAGE_KEY)!)).toContain(
      "default-other-sessions",
    );
    expect(localStorage.getItem(LEGACY_COLLAPSED_STORAGE_KEY)).toBeNull();
  });

  it("migrates legacy title-keyed collapse state onto the default ids", () => {
    localStorage.setItem(LEGACY_COLLAPSED_STORAGE_KEY, JSON.stringify(["Pinned", "Chats"]));
    renderSidebar();

    expect(JSON.parse(localStorage.getItem(COLLAPSED_IDS_STORAGE_KEY)!)).toEqual([
      "default-favorites",
      "default-other-sessions",
    ]);
    expect(localStorage.getItem(LEGACY_COLLAPSED_STORAGE_KEY)).toBe(
      JSON.stringify(["Pinned", "Chats"]),
    );
    expect(screen.getByRole("button", { name: /Sessions/ })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
  });

  it("offers a fallback New section entry when the layout is empty", async () => {
    localStorage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify({ version: 1, sections: [] }));
    renderSidebar();

    fireEvent.click(screen.getByTestId("sidebar-new-section-fallback"));
    fireEvent.click(screen.getByTestId("new-section-kind-other_sessions"));
    fireEvent.click(screen.getByTestId("new-section-continue"));
    fireEvent.click(screen.getByTestId("new-section-create"));

    await waitFor(() => expect(screen.queryByTestId("new-section-dialog")).toBeNull());
    expect(screen.getByText("Sessions")).toBeInTheDocument();
    const stored = JSON.parse(localStorage.getItem(LAYOUT_STORAGE_KEY)!) as SidebarLayout;
    expect(stored.sections.map((section) => section.kind)).toEqual(["other_sessions"]);
  });

  it("shift-select follows the rendered project order in a projects section", async () => {
    projectsRef.current = [...PROJECTS, { id: "p_delta", name: "Delta", icon: null }];
    localStorage.setItem(
      LAYOUT_STORAGE_KEY,
      JSON.stringify({
        version: 1,
        sections: [
          {
            id: "sec_work",
            kind: "projects",
            name: "Work",
            maxRows: null,
            projectIds: ["p_gamma", "p_alpha", "p_beta"],
          },
          { id: "default-other-projects", kind: "other_projects", name: "Projects", maxRows: null },
        ],
      }),
    );
    localStorage.setItem(
      "omnigent:expanded-project-sections",
      JSON.stringify(["Alpha", "Beta", "Gamma"]),
    );
    mockConversations([
      conversation("a1", { labels: { omni_project: "Alpha" }, updated_at: 4 }),
      conversation("b1", { labels: { omni_project: "Beta" }, updated_at: 3 }),
      conversation("g1", { labels: { omni_project: "Gamma" }, updated_at: 2 }),
      conversation("d1", { labels: { omni_project: "Delta" }, updated_at: 1 }),
    ]);
    renderSidebar();

    fireEvent.pointerDown(screen.getByRole("button", { name: "Project list actions" }), {
      button: 0,
    });
    fireEvent.click(await screen.findByTestId("projects-select-sessions"));

    // Alphabetical render order is Alpha, Beta, Gamma — so the Alpha→Gamma
    // range spans all three even though the section lists them Gamma-first.
    fireEvent.click(await screen.findByRole("link", { name: "a1" }));
    fireEvent.click(screen.getByRole("link", { name: "g1" }), { shiftKey: true });
    await waitFor(() => expect(screen.getByText("3 selected")).toBeInTheDocument());
  });

  it("shows a rolled-up awaiting marker on a collapsed projects section", () => {
    localStorage.setItem(
      LAYOUT_STORAGE_KEY,
      JSON.stringify({
        version: 1,
        sections: [
          {
            id: "sec_work",
            kind: "projects",
            name: "Work",
            maxRows: null,
            projectIds: ["p_alpha"],
          },
        ],
      }),
    );
    localStorage.setItem(COLLAPSED_IDS_STORAGE_KEY, JSON.stringify(["sec_work"]));
    mockConversations([
      conversation("awaiting", {
        labels: { omni_project: "Alpha" },
        pending_elicitations_count: 1,
      }),
    ]);
    renderSidebar();

    const badge = within(sectionOf("Work")).getByTestId("session-state-badge");
    expect(badge).toHaveAttribute("data-state", "awaiting");
  });

  it("renders recent rows in server order with the project label and status marker", async () => {
    localStorage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify(RECENT_LAYOUT));
    recentRef.current.data = [
      conversation("r_new", {
        updated_at: 2,
        project_id: "p_alpha",
        pending_elicitations_count: 1,
      }),
      conversation("r_old", { updated_at: 1, labels: { omni_project: "Beta" } }),
    ];
    renderSidebar();

    const recent = sectionOf("Recent");
    const newRow = await within(recent).findByText("r_new");
    const oldRow = within(recent).getByText("r_old");
    expect(isBefore(newRow, oldRow)).toBe(true);
    expect(
      within(recent)
        .getAllByTestId("conversation-project-label")
        .map((el) => el.textContent),
    ).toEqual(["Alpha", "Beta"]);
    expect(within(recent).getByTestId("session-state-badge")).toHaveAttribute(
      "data-state",
      "awaiting",
    );
  });

  it("keeps the first Recent copy canonical for an unfiled session", async () => {
    localStorage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify(RECENT_LAYOUT));
    const session = conversation("s1");
    mockConversations([session]);
    recentRef.current.data = [session];
    renderSidebar();

    await waitFor(() =>
      expect(document.querySelectorAll('li[data-sidebar-session-id="s1"]')).toHaveLength(2),
    );

    const recentRow = sectionOf("Recent").querySelector('li[data-sidebar-session-id="s1"]');
    const sessionsRow = sectionOf("Sessions").querySelector('li[data-sidebar-session-id="s1"]');
    expect(recentRow).toHaveAttribute("data-sidebar-canonical", "true");
    expect(sessionsRow).not.toHaveAttribute("data-sidebar-canonical");
  });

  it("changes the recent count from the Show submenu and refetches with the new limit", async () => {
    localStorage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify(RECENT_LAYOUT));
    renderSidebar();
    await waitFor(() => expect(recentCalls()).toHaveLength(1));

    fireEvent.pointerDown(within(sectionOf("Recent")).getByTestId("section-options"), {
      button: 0,
    });
    fireEvent.click(await screen.findByTestId("section-show"));
    fireEvent.click(await screen.findByTestId("section-show-8"));

    await waitFor(() => {
      const stored = JSON.parse(localStorage.getItem(LAYOUT_STORAGE_KEY)!) as SidebarLayout;
      expect(stored.sections.find((section) => section.kind === "recent")?.count).toBe(8);
    });
    await waitFor(() =>
      expect(recentCalls().some((call) => String(call[0]).includes("limit=8"))).toBe(true),
    );
  });

  it("shows the old-server note when the recent route 404s and does not retry", async () => {
    localStorage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify(RECENT_LAYOUT));
    recentRef.current.status = 404;
    renderSidebar();

    expect(
      await within(sectionOf("Recent")).findByText("Recent sessions need a newer server."),
    ).toBeInTheDocument();
    expect(recentCalls()).toHaveLength(1);
  });

  it("visits a duplicated session once and drags only the canonical copy", async () => {
    localStorage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify(RECENT_FOLDER_LAYOUT));
    localStorage.setItem("omnigent:expanded-project-sections", JSON.stringify(["Alpha"]));
    projectsRef.current = [{ id: "p_alpha", name: "Alpha", icon: null }];
    const s1 = conversation("s1", { labels: { omni_project: "Alpha" }, updated_at: 2 });
    const s2 = conversation("s2", { labels: { omni_project: "Alpha" }, updated_at: 1 });
    mockConversations([s1, s2]);
    recentRef.current.data = [s1];
    const platform = vi.spyOn(navigator, "platform", "get").mockReturnValue("MacIntel");
    renderSidebarAt("/c/s2");

    await waitFor(() =>
      expect(document.querySelectorAll('li[data-sidebar-session-id="s1"]')).toHaveLength(2),
    );
    expect(
      sectionOf("Work").querySelector(
        'li[data-sidebar-session-id="s1"][data-sidebar-canonical="true"]',
      ),
    ).not.toBeNull();
    expect(
      sectionOf("Recent").querySelector(
        'li[data-sidebar-session-id="s1"][data-sidebar-canonical="true"]',
      ),
    ).toBeNull();
    expect(
      document.querySelectorAll('li[data-sidebar-session-id="s1"][data-sidebar-canonical="true"]'),
    ).toHaveLength(1);

    // Cmd+] steps to s1, then again to s2: s1 is visited once, not twice.
    fireEvent.keyDown(document.body, { code: "BracketRight", metaKey: true, bubbles: true });
    await waitFor(() => expect(screen.getByTestId("location").textContent).toBe("/c/s1"));
    fireEvent.keyDown(document.body, { code: "BracketRight", metaKey: true, bubbles: true });
    await waitFor(() => expect(screen.getByTestId("location").textContent).toBe("/c/s2"));

    const recentRow = document.querySelector(
      'li[data-sidebar-session-id="s1"]:not([data-sidebar-canonical])',
    ) as HTMLElement;
    fireEvent.mouseDown(recentRow, { button: 0, clientX: 50, clientY: 20 });
    fireEvent.mouseMove(document, { clientX: 50, clientY: 40 });
    expect(recentRow).not.toHaveClass("opacity-40");
    fireEvent.mouseUp(document);

    const canonicalRow = document.querySelector(
      'li[data-sidebar-session-id="s1"][data-sidebar-canonical="true"]',
    ) as HTMLElement;
    fireEvent.mouseDown(canonicalRow, { button: 0, clientX: 50, clientY: 20 });
    fireEvent.mouseMove(document, { clientX: 50, clientY: 40 });
    expect(canonicalRow).toHaveClass("opacity-40");
    fireEvent.mouseUp(document);

    platform.mockRestore();
  });
});

// ── Section / project drag ───────────────────────────────────────────────────

const HEADER_ROW = 40;
const SECTION_BAND = 400;
const SECTION_BASE = 1000;

/** jsdom has no layout, so section wrappers enclose their sortable headers. */
function stubRects() {
  return vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (
    this: Element,
  ) {
    if (this.tagName === "NAV") return new DOMRect(0, 0, 400, 5000);
    const sections = [...document.querySelectorAll("[data-section-id]")];
    const sectionIndex = sections.indexOf(this);
    if (sectionIndex >= 0) {
      return new DOMRect(0, SECTION_BASE + sectionIndex * SECTION_BAND, 240, SECTION_BAND - 40);
    }
    const section = this.closest("[data-section-id]");
    if (section !== null && this.matches('button[aria-roledescription="sortable"]')) {
      const sectionHeaders = [
        ...section.querySelectorAll('button[aria-roledescription="sortable"]'),
      ];
      const headerIndex = sectionHeaders.indexOf(this);
      const index = sections.indexOf(section);
      return new DOMRect(
        0,
        SECTION_BASE + index * SECTION_BAND + 10 + headerIndex * HEADER_ROW,
        200,
        HEADER_ROW - 10,
      );
    }
    return new DOMRect(0, 0, 0, 0);
  });
}

function headerButton(name: string): HTMLElement {
  return screen.getByRole("button", { name });
}

function headerPoint(name: string): { clientX: number; clientY: number } {
  const rect = headerButton(name).getBoundingClientRect();
  return { clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2 };
}

function sectionPoint(sectionId: string): { clientX: number; clientY: number } {
  const sections = [...document.querySelectorAll("[data-section-id]")];
  const section = sections.find((el) => el.getAttribute("data-section-id") === sectionId);
  if (section === undefined) throw new Error(`No section with id ${sectionId}`);
  const rect = section.getBoundingClientRect();
  return { clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2 };
}

async function dragHeaderToPoint(
  sourceEl: HTMLElement,
  source: { clientX: number; clientY: number },
  target: { clientX: number; clientY: number },
  expectProjectInsertion = false,
) {
  fireEvent.mouseDown(sourceEl, { button: 0, ...source });
  fireEvent.mouseMove(document, { clientX: source.clientX, clientY: source.clientY - 10 });
  await act(async () => {
    fireEvent.mouseMove(document, target);
  });
  if (expectProjectInsertion) {
    expect(sourceEl).toHaveClass("opacity-40");
    expect(screen.queryByTestId("project-order-insertion")).not.toBeNull();
  }
  await act(async () => {
    fireEvent.mouseUp(document, target);
  });
}

function storedLayout(): SidebarLayout {
  return JSON.parse(localStorage.getItem(LAYOUT_STORAGE_KEY)!) as SidebarLayout;
}

function projectIdsOf(sectionId: string): string[] | undefined {
  return storedLayout().sections.find((section) => section.id === sectionId)?.projectIds;
}

async function waitForProjectOrderReady(projectName: string) {
  fireEvent.pointerDown(screen.getByLabelText(`Project actions for ${projectName}`), {
    button: 0,
  });
  fireEvent.keyDown(screen.getByTestId("move-project"), { key: "ArrowRight" });
  await waitFor(() =>
    expect(screen.getByRole("menuitem", { name: "Move down" })).not.toHaveAttribute(
      "aria-disabled",
      "true",
    ),
  );
  fireEvent.pointerDown(document.body, { button: 0 });
  await waitFor(() => expect(screen.queryByTestId("move-project")).toBeNull());
}

async function startKeyboardDrag(source: HTMLElement) {
  fireEvent.keyDown(source, { code: "Space", key: " " });
  await act(
    async () =>
      new Promise<void>((resolve) => {
        setTimeout(resolve, 0);
      }),
  );
}

describe("sidebar section drag", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("reorders a section header with the keyboard in the default layout", async () => {
    stubRects();
    renderSidebar();

    const source = headerButton("Use No Project for new sessions");
    await startKeyboardDrag(source);
    expect(source).toHaveClass("opacity-40");
    await act(async () => {
      fireEvent.keyDown(source, { code: "ArrowUp", key: "ArrowUp" });
    });
    fireEvent.keyDown(source, { code: "Space", key: " " });

    await waitFor(() =>
      expect(storedLayout().sections.map((section) => section.id)).toEqual([
        "default-favorites",
        "default-other-sessions",
        "default-other-projects",
      ]),
    );
  });

  it("reorders a project folder with the keyboard in the default layout", async () => {
    stubRects();
    orderRef.current = {
      sort_mode: "manual",
      ordered_project_ids: ["p_alpha", "p_beta", "p_gamma"],
    };
    renderSidebar();
    await waitForProjectOrderReady("Alpha");

    const source = headerButton("Use Alpha for new sessions");
    await startKeyboardDrag(source);
    expect(source).toHaveClass("opacity-40");
    await act(async () => {
      fireEvent.keyDown(source, { code: "ArrowDown", key: "ArrowDown" });
    });
    fireEvent.keyDown(source, { code: "Space", key: " " });

    await waitFor(() => {
      const saves = fetchMock.mock.calls.filter(
        ([url, init]) => String(url) === "/v1/projects/order" && init?.method === "PUT",
      );
      expect(saves).toHaveLength(1);
      expect(JSON.parse((saves[0]![1] as RequestInit).body as string)).toEqual({
        ordered_project_ids: ["p_beta", "p_alpha", "p_gamma"],
      });
    });
  });

  it("moves a section header above another and saves the new order", async () => {
    stubRects();
    renderSidebar();

    const source = headerButton("Use No Project for new sessions");
    const start = headerPoint("Use No Project for new sessions");
    const target = headerPoint("Projects");
    fireEvent.mouseDown(source, { button: 0, ...start });
    fireEvent.mouseMove(document, { clientX: start.clientX, clientY: start.clientY - 10 });
    await act(async () => {
      fireEvent.mouseMove(document, target);
    });
    expect(screen.queryByTestId("section-order-insertion")).not.toBeNull();
    await act(async () => {
      fireEvent.mouseUp(document, target);
    });

    expect(storedLayout().sections.map((section) => section.id)).toEqual([
      "default-favorites",
      "default-other-sessions",
      "default-other-projects",
    ]);
    expect(isBefore(screen.getByText("Sessions"), screen.getByText("Projects"))).toBe(true);
  });

  it("reorders a default-layout folder when released on the Projects body", async () => {
    stubRects();
    orderRef.current = {
      sort_mode: "manual",
      ordered_project_ids: ["p_alpha", "p_beta", "p_gamma"],
    };
    renderSidebar();
    await waitForProjectOrderReady("Alpha");

    await dragHeaderToPoint(
      headerButton("Use Alpha for new sessions"),
      headerPoint("Use Alpha for new sessions"),
      sectionPoint("default-other-projects"),
      true,
    );

    await waitFor(() => {
      const saves = fetchMock.mock.calls.filter(
        ([url, init]) => String(url) === "/v1/projects/order" && init?.method === "PUT",
      );
      expect(saves).toHaveLength(1);
      expect(JSON.parse((saves[0]![1] as RequestInit).body as string)).toEqual({
        ordered_project_ids: ["p_beta", "p_gamma", "p_alpha"],
      });
    });
  });

  it("moves a project folder into a projects section and back onto Projects", async () => {
    stubRects();
    localStorage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify(EMPTY_WORK_LAYOUT));
    renderSidebar();
    expect(within(sectionOf("Projects")).getByText("Alpha")).toBeInTheDocument();

    await dragHeaderToPoint(
      headerButton("Use Alpha for new sessions"),
      headerPoint("Use Alpha for new sessions"),
      sectionPoint("sec_work"),
    );
    await waitFor(() => expect(within(sectionOf("Work")).getByText("Alpha")).toBeInTheDocument());
    expect(within(sectionOf("Projects")).queryByText("Alpha")).toBeNull();
    expect(projectIdsOf("sec_work")).toEqual(["p_alpha"]);

    await dragHeaderToPoint(
      headerButton("Use Alpha for new sessions"),
      headerPoint("Use Alpha for new sessions"),
      sectionPoint("default-other-projects"),
    );
    await waitFor(() =>
      expect(within(sectionOf("Projects")).getByText("Alpha")).toBeInTheDocument(),
    );
    expect(projectIdsOf("sec_work")).toEqual([]);
  });

  it("lets a folder move into a projects section when project order data is unavailable", async () => {
    stubRects();
    orderStatusRef.current = 404;
    localStorage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify(EMPTY_WORK_LAYOUT));
    renderSidebar();

    await dragHeaderToPoint(
      headerButton("Use Alpha for new sessions"),
      headerPoint("Use Alpha for new sessions"),
      sectionPoint("sec_work"),
    );

    await waitFor(() => expect(projectIdsOf("sec_work")).toEqual(["p_alpha"]));
  });

  it("promotes a label-only project before moving it into a section", async () => {
    stubRects();
    localStorage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify(EMPTY_WORK_LAYOUT));
    projectsRef.current = [{ id: null, name: "Legacy", icon: null }, ...PROJECTS];
    renderSidebar();
    expect(within(sectionOf("Projects")).getByText("Legacy")).toBeInTheDocument();

    await dragHeaderToPoint(
      headerButton("Use Legacy for new sessions"),
      headerPoint("Use Legacy for new sessions"),
      sectionPoint("sec_work"),
    );
    await waitFor(() => expect(projectIdsOf("sec_work")).toEqual(["p_Legacy"]));
  });

  it("leaves the layout unchanged when a label-only promotion fails", async () => {
    stubRects();
    localStorage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify(EMPTY_WORK_LAYOUT));
    projectsRef.current = [{ id: null, name: "Legacy", icon: null }];
    vi.mocked(resolveOrCreateProjectId).mockRejectedValueOnce(new Error("nope"));
    const toasts: unknown[] = [];
    const listener = (event: Event) => toasts.push((event as CustomEvent).detail.content);
    window.addEventListener("omnigent:toast", listener);
    renderSidebar();

    await dragHeaderToPoint(
      headerButton("Use Legacy for new sessions"),
      headerPoint("Use Legacy for new sessions"),
      sectionPoint("sec_work"),
    );
    await waitFor(() => expect(toasts).toContain("Couldn't move the project"));
    expect(projectIdsOf("sec_work")).toEqual([]);
    window.removeEventListener("omnigent:toast", listener);
  });
});

describe("reordering folders inside a projects section", () => {
  const TWO_PROJECT_LAYOUT: SidebarLayout = {
    version: 1,
    sections: [
      {
        id: "sec_work",
        kind: "projects",
        name: "Work",
        maxRows: null,
        projectIds: ["p_beta", "p_alpha"],
      },
      { id: "default-other-projects", kind: "other_projects", name: "Projects", maxRows: null },
      { id: "default-other-sessions", kind: "other_sessions", name: "Sessions", maxRows: null },
    ],
  };

  it("moves a folder within the section's project order in manual mode", async () => {
    orderRef.current = { sort_mode: "manual", ordered_project_ids: ["p_beta", "p_alpha"] };
    localStorage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify(TWO_PROJECT_LAYOUT));
    renderSidebar();

    await waitFor(() =>
      expect(
        isBefore(
          within(sectionOf("Work")).getByText("Beta"),
          within(sectionOf("Work")).getByText("Alpha"),
        ),
      ).toBe(true),
    );

    fireEvent.pointerDown(within(sectionOf("Work")).getAllByTestId("project-actions")[0], {
      button: 0,
    });
    fireEvent.keyDown(screen.getByTestId("move-project"), { key: "ArrowRight" });
    fireEvent.click(screen.getByRole("menuitem", { name: "Move down" }));

    await waitFor(() => expect(projectIdsOf("sec_work")).toEqual(["p_alpha", "p_beta"]));
    expect(
      isBefore(
        within(sectionOf("Work")).getByText("Alpha"),
        within(sectionOf("Work")).getByText("Beta"),
      ),
    ).toBe(true);
  });

  it("reorders a folder by mouse when its own section body is the drop target", async () => {
    stubRects();
    orderRef.current = { sort_mode: "manual", ordered_project_ids: ["p_beta", "p_alpha"] };
    localStorage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify(TWO_PROJECT_LAYOUT));
    renderSidebar();
    await waitForProjectOrderReady("Beta");

    await dragHeaderToPoint(
      headerButton("Use Beta for new sessions"),
      headerPoint("Use Beta for new sessions"),
      sectionPoint("sec_work"),
      true,
    );

    await waitFor(() => expect(projectIdsOf("sec_work")).toEqual(["p_alpha", "p_beta"]));
  });

  it("moves down across an unresolved project reference", async () => {
    orderRef.current = { sort_mode: "manual", ordered_project_ids: ["p_alpha", "p_beta"] };
    localStorage.setItem(
      LAYOUT_STORAGE_KEY,
      JSON.stringify({
        ...TWO_PROJECT_LAYOUT,
        sections: TWO_PROJECT_LAYOUT.sections.map((section) =>
          section.id === "sec_work"
            ? { ...section, projectIds: ["p_alpha", "missing", "p_beta"] }
            : section,
        ),
      }),
    );
    renderSidebar();
    await waitForProjectOrderReady("Alpha");

    fireEvent.pointerDown(within(sectionOf("Work")).getAllByTestId("project-actions")[0], {
      button: 0,
    });
    fireEvent.keyDown(screen.getByTestId("move-project"), { key: "ArrowRight" });
    fireEvent.click(screen.getByRole("menuitem", { name: "Move down" }));

    await waitFor(() => expect(projectIdsOf("sec_work")).toEqual(["p_beta", "missing", "p_alpha"]));
    expect(
      isBefore(
        within(sectionOf("Work")).getByText("Beta"),
        within(sectionOf("Work")).getByText("Alpha"),
      ),
    ).toBe(true);
  });

  it("disables the folder move menu in alphabetical project order", async () => {
    orderRef.current = { sort_mode: "alphabetical", ordered_project_ids: [] };
    localStorage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify(TWO_PROJECT_LAYOUT));
    renderSidebar();

    fireEvent.pointerDown(within(sectionOf("Work")).getAllByTestId("project-actions")[0], {
      button: 0,
    });
    fireEvent.keyDown(screen.getByTestId("move-project"), { key: "ArrowRight" });

    expect(screen.getByRole("menuitem", { name: "Move down" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
  });
});

describe("collapsed section marker freshness", () => {
  it("refreshes a collapsed section's badge when a hidden session's state changes", () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const mineKey = ["conversations", "", false, null, "mine"];
    const initialData = conversationPage([
      conversation("run1", { labels: { omni_project: "Alpha" }, status: "running" }),
    ]).data;
    qc.setQueryData(mineKey, initialData);
    const cachedRows = () => {
      const cached = qc.getQueryData(mineKey) as { pages: { data: Conversation[] }[] };
      return cached.pages.flatMap((page) => page.data);
    };
    mockConversations(cachedRows());
    const tree = () => (
      <QueryClientProvider client={qc}>
        <SidebarDataProvider>
          <TooltipProvider>
            <MemoryRouter initialEntries={["/"]}>
              <Sidebar open onClose={vi.fn()} />
            </MemoryRouter>
          </TooltipProvider>
        </SidebarDataProvider>
      </QueryClientProvider>
    );
    localStorage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify(WORK_LAYOUT));
    localStorage.setItem("omnigent:expanded-project-sections", JSON.stringify(["Alpha"]));
    projectsRef.current = [{ id: "p_alpha", name: "Alpha", icon: null }];

    const view = render(tree());
    // The expanded folder mounts and reports its running row.
    expect(within(sectionOf("Work")).getByText("run1")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Work" }));
    expect(within(sectionOf("Work")).getByTestId("session-state-badge")).toHaveAttribute(
      "data-state",
      "running",
    );

    // The folder is unmounted while collapsed; the conversations cache reports awaiting.
    act(() => {
      qc.setQueryData(
        mineKey,
        conversationPage([
          conversation("run1", {
            labels: { omni_project: "Alpha" },
            pending_elicitations_count: 1,
          }),
        ]).data,
      );
    });
    mockConversations(cachedRows());
    view.rerender(tree());

    return waitFor(() =>
      expect(within(sectionOf("Work")).getByTestId("session-state-badge")).toHaveAttribute(
        "data-state",
        "awaiting",
      ),
    );
  });
});
