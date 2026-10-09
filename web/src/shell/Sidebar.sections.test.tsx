import { conversation, conversationPage } from "@/test/sidebarMockHelpers";
import { renderSidebar } from "@/test/sidebarTestHelpers";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/hooks/useScopeCache", () => import("@/test/mockScopeCache"));

import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";

import type { SidebarLayout } from "@/lib/sidebarLayout";

vi.mock("@/hooks/useHosts", () => ({
  useHosts: () => ({ data: [] }),
}));

const { projectsRef, pinnedRef } = vi.hoisted(() => ({
  projectsRef: {
    current: [] as { id: string; name: string; icon?: string | null }[],
  },
  pinnedRef: { current: [] as unknown[] },
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

import { type Conversation, useConversations } from "@/hooks/useConversations";

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

beforeEach(() => {
  vi.clearAllMocks();
  useConversationsMock.mockReset();
  localStorage.clear();
  projectsRef.current = PROJECTS;
  pinnedRef.current = [];
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
});
