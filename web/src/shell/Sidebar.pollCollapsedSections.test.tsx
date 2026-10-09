import { conversation, conversationPage } from "@/test/sidebarMockHelpers";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/hooks/useScopeCache", () => import("@/test/mockScopeCache"));
import { SidebarDataProvider } from "@/hooks/useSidebarData";

// Poll's interaction with the sidebar's own data: candidates come from the
// rendered sections (including each folder's own query) and the loaded pages,
// never from fetching more pages. The plain cycle skips rows hidden inside a
// collapsed section/folder or past the display page (but still jumps to them
// when unread), and the candidate pool stays the rows the active My sessions /
// Shared filter holds.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { TooltipProvider } from "@/components/ui/tooltip";
import { POLL_SESSIONS_ACTION_EVENT } from "@/hooks/useSessionPollingHotkeys";
import { resetReadStateForTests } from "@/hooks/useUnseenConversations";
import {
  COLLAPSED_SIDEBAR_SECTIONS_STORAGE_KEY,
  EXPANDED_PROJECT_SECTIONS_STORAGE_KEY,
} from "@/shell/sidebarNav";

const { projectsRef, pinnedRef, folderRowsRef } = vi.hoisted(() => ({
  projectsRef: { current: [] as { id: string; name: string }[] },
  pinnedRef: { current: [] as unknown[] },
  folderRowsRef: { current: new Map<string, unknown[]>() },
}));

vi.mock("@/hooks/useConversations", async () => {
  const { conversationHooksMock } = await import("@/test/sidebarMockHelpers");
  return {
    ...conversationHooksMock(),
    useProjects: () => ({ data: projectsRef.current }),
    usePinnedConversations: () => ({
      data: { conversations: pinnedRef.current, filterHonored: true },
      isSuccess: true,
    }),
    useProjectSessions: (name: string) => {
      const rows = folderRowsRef.current.get(name) ?? [];
      return {
        data: rows.length ? { pages: [{ data: rows }], pageParams: [undefined] } : undefined,
        isLoading: false,
        hasNextPage: false,
        isFetchingNextPage: false,
        fetchNextPage: vi.fn(),
      };
    },
  };
});
vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));
// The filter menu (My sessions / Shared) only renders on a multi-user server.
vi.mock("@/lib/serverOrigin", () => ({ isCurrentServerLocal: () => false }));

import { useConversations } from "@/hooks/useConversations";
import { sidebarConfig, type SidebarConfig } from "@/lib/sidebarConfig";
import { Sidebar } from "./Sidebar";

const useConvMock = vi.mocked(useConversations);

function mockConversations(convs: Parameters<typeof conversationPage>[0]) {
  useConvMock.mockReturnValue(conversationPage(convs));
}

function mockConversationsPage(
  convs: Parameters<typeof conversationPage>[0],
  extra: { hasNextPage?: boolean; fetchNextPage?: () => unknown },
) {
  useConvMock.mockReturnValue({
    ...conversationPage(convs),
    ...extra,
  } as unknown as ReturnType<typeof useConversations>);
}

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location">{location.pathname}</output>;
}

function sidebarTree(initialEntry: string, config: SidebarConfig, qc: QueryClient) {
  return (
    <QueryClientProvider client={qc}>
      <SidebarDataProvider config={config}>
        <TooltipProvider>
          <MemoryRouter initialEntries={[initialEntry]}>
            <LocationProbe />
            <Routes>
              <Route path="/" element={<Sidebar open onClose={vi.fn()} />} />
              <Route path="/c/:conversationId" element={<Sidebar open onClose={vi.fn()} />} />
            </Routes>
          </MemoryRouter>
        </TooltipProvider>
      </SidebarDataProvider>
    </QueryClientProvider>
  );
}

function renderAt(initialEntry: string, config: SidebarConfig = sidebarConfig) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(sidebarTree(initialEntry, config, qc));
  // Re-render the tree in place (router location and query client kept), so a
  // test can change a module-level mock and see the sidebar react to it.
  return { rerenderTree: () => view.rerender(sidebarTree(initialEntry, config, qc)) };
}

async function pressPollAndExpect(path: string) {
  act(() => window.dispatchEvent(new Event(POLL_SESSIONS_ACTION_EVENT)));
  await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent(path));
}

beforeEach(() => {
  useConvMock.mockReset();
  projectsRef.current = [];
  pinnedRef.current = [];
  folderRowsRef.current.clear();
  localStorage.clear();
  resetReadStateForTests();
});

afterEach(cleanup);

describe("sidebar Poll vs collapse containers", () => {
  it("plain cycle skips rows hidden by a collapsed Pinned section and folder", async () => {
    // A pinned session now also renders in its folder, so it is only truly
    // hidden while both the Pinned section and its folder are collapsed.
    localStorage.setItem(COLLAPSED_SIDEBAR_SECTIONS_STORAGE_KEY, JSON.stringify(["Pinned"]));
    projectsRef.current = [{ id: "p1", name: "Proj" }];
    pinnedRef.current = [
      conversation("hidden-pin", { updated_at: 3, labels: { omni_project: "Proj" } }),
    ];
    mockConversations([
      conversation("active", { updated_at: 1 }),
      conversation("visible", { updated_at: 2 }),
      conversation("hidden-pin", { updated_at: 3, labels: { omni_project: "Proj" } }),
    ]);
    renderAt("/c/active");

    await pressPollAndExpect("/c/visible");
  });

  it("still jumps to an unread row inside the collapsed Chats section", async () => {
    localStorage.setItem(COLLAPSED_SIDEBAR_SECTIONS_STORAGE_KEY, JSON.stringify(["Chats"]));
    mockConversations([
      conversation("active", { updated_at: 3, viewer_last_seen: 3 }),
      conversation("visible", { updated_at: 2, viewer_last_seen: 2 }),
      conversation("hidden-unread", { updated_at: 1, viewer_last_seen: 0 }),
    ]);
    renderAt("/c/active");

    await pressPollAndExpect("/c/hidden-unread");
  });

  it("plain cycle skips rows inside a collapsed project folder", async () => {
    // Project folders default to collapsed, so no collapse state to seed.
    projectsRef.current = [{ id: "p1", name: "Proj" }];
    mockConversations([
      conversation("active", { updated_at: 1 }),
      conversation("unfiled", { updated_at: 2 }),
      conversation("filed", { updated_at: 3, labels: { omni_project: "Proj" } }),
    ]);
    renderAt("/c/active");

    await pressPollAndExpect("/c/unfiled");
  });
});

describe("sidebar Poll vs the sidebar's own rows", () => {
  it("polls a loaded next-page row without fetching the page", async () => {
    const fetchNextPage = vi.fn();
    mockConversationsPage(
      [conversation("active", { updated_at: 2 }), conversation("next", { updated_at: 1 })],
      { hasNextPage: true, fetchNextPage },
    );
    renderAt("/c/active");

    await pressPollAndExpect("/c/next");
    expect(fetchNextPage).not.toHaveBeenCalled();
  });

  it("plain cycle skips a loaded row past the display page", async () => {
    mockConversations([
      conversation("newest", { updated_at: 4 }),
      conversation("middle", { updated_at: 3 }),
      conversation("active", { updated_at: 2 }),
      conversation("beyond", { updated_at: 1 }),
    ]);
    renderAt("/c/active", { ...sidebarConfig, displayPageSize: 3 });

    await pressPollAndExpect("/c/newest");
  });

  it("still jumps to a row past the display page when it is unread", async () => {
    mockConversations([
      conversation("newest", { updated_at: 4, viewer_last_seen: 4 }),
      conversation("middle", { updated_at: 3, viewer_last_seen: 3 }),
      conversation("active", { updated_at: 2, viewer_last_seen: 2 }),
      conversation("beyond", { updated_at: 1, viewer_last_seen: 0 }),
    ]);
    renderAt("/c/active", { ...sidebarConfig, displayPageSize: 3 });

    await pressPollAndExpect("/c/beyond");
  });

  it("polls a row that only an expanded folder's own query holds", async () => {
    localStorage.setItem(EXPANDED_PROJECT_SECTIONS_STORAGE_KEY, JSON.stringify(["Proj"]));
    projectsRef.current = [{ id: "p1", name: "Proj" }];
    folderRowsRef.current.set("Proj", [conversation("folder-only", { updated_at: 3 })]);
    mockConversations([
      conversation("tail", { updated_at: 2 }),
      conversation("active", { updated_at: 1 }),
    ]);
    renderAt("/c/active");

    await pressPollAndExpect("/c/folder-only");
  });

  it("prioritises an expanded folder's row when a card lands without an id or order change", async () => {
    localStorage.setItem(EXPANDED_PROJECT_SECTIONS_STORAGE_KEY, JSON.stringify(["Proj"]));
    projectsRef.current = [{ id: "p1", name: "Proj" }];
    folderRowsRef.current.set("Proj", [
      conversation("folder-card", { updated_at: 5, pending_elicitations_count: 0 }),
    ]);
    mockConversations([
      conversation("active", { updated_at: 3 }),
      conversation("next", { updated_at: 2 }),
      conversation("after", { updated_at: 1 }),
    ]);
    const { rerenderTree } = renderAt("/c/active");

    // No card yet: Poll moves through the plain cycle.
    await pressPollAndExpect("/c/next");

    // A card arrives on the folder row in place: same id, same position.
    folderRowsRef.current.set("Proj", [
      conversation("folder-card", { updated_at: 5, pending_elicitations_count: 1 }),
    ]);
    rerenderTree();

    await pressPollAndExpect("/c/folder-card");
  });

  it("plain cycle skips a collapsed folder's own row", async () => {
    projectsRef.current = [{ id: "p1", name: "Proj" }];
    folderRowsRef.current.set("Proj", [conversation("folder-only", { updated_at: 3 })]);
    mockConversations([
      conversation("tail", { updated_at: 2 }),
      conversation("active", { updated_at: 1 }),
    ]);
    renderAt("/c/active");

    await pressPollAndExpect("/c/tail");
  });

  it("still jumps to a collapsed folder's own row when it is unread", async () => {
    projectsRef.current = [{ id: "p1", name: "Proj" }];
    folderRowsRef.current.set("Proj", [
      conversation("folder-unread", { updated_at: 3, viewer_last_seen: 0 }),
    ]);
    mockConversations([
      conversation("tail", { updated_at: 2, viewer_last_seen: 2 }),
      conversation("active", { updated_at: 1, viewer_last_seen: 1 }),
    ]);
    renderAt("/c/active");

    await pressPollAndExpect("/c/folder-unread");
  });
});

describe("sidebar Poll vs the session filter", () => {
  it("does not poll into a shared-only row while the My sessions filter is active", async () => {
    mockConversations([
      conversation("active", { updated_at: 1 }),
      conversation("owned-unfiled", { updated_at: 2 }),
      conversation("shared-unfiled", { updated_at: 3, owner: "other@example.com" }),
    ]);
    renderAt("/c/active");

    await pressPollAndExpect("/c/owned-unfiled");
  });

  it("does not poll into an owned-only flat row while the Shared filter is active", async () => {
    mockConversations([
      conversation("active", { updated_at: 1 }),
      conversation("shared-unfiled", { updated_at: 2, owner: "other@example.com" }),
      conversation("owned-unfiled", { updated_at: 3 }),
    ]);
    renderAt("/c/active");
    // Radix Tabs triggers activate on mousedown (primary button), not click.
    fireEvent.pointerDown(screen.getByTestId("session-filter"), {
      button: 0,
      ctrlKey: false,
      pointerType: "mouse",
    });
    fireEvent.click(screen.getByTestId("session-filter-shared"));

    await pressPollAndExpect("/c/shared-unfiled");
  });
});
