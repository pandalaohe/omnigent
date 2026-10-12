import { conversationPage } from "@/test/sidebarMockHelpers";
import type * as WorktreeStatusMarkModule from "@/components/WorktreeStatusMark";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/hooks/useScopeCache", () => import("@/test/mockScopeCache"));
import { SidebarDataProvider } from "@/hooks/useSidebarData";
// Tests for the archive flow in the sidebar. Archiving sends the archive
// PATCH without a client stop. The row leaves the
// sidebar optimistically (useArchiveConversation flips the cached `archived`
// flag in onMutate; the list filters archived rows out client-side), so there
// is no "Archiving…" status row — the row simply unmounts, like delete's. The
// only synchronous side effect is the Undo pill (which also links to Settings).
// The runner stop is the server's job once the flag commits — a client stop
// would race the server's against the same runner, and put the runner's stop
// timeouts in front of the flag flip. The kebab's user-facing "Stop session"
// action is a separate affordance covered by Sidebar.stop.test.tsx.
// The optimistic cache overlay + error reconcile is covered in
// sessionListCache.test.ts / useConversations.test.ts.
//
// Archived sessions are no longer listed in the sidebar (they moved to the
// Settings page), so unarchiving is covered by SettingsPage.test.tsx; this
// file exercises the archive path from a row's kebab.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { ARCHIVE_SESSION_ACTION_EVENT } from "@/hooks/useSessionPollingHotkeys";
import { TooltipProvider } from "@/components/ui/tooltip";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO, type ServerInfo } from "@/lib/capabilities";

// Controllable archive + stop mutations, declared via vi.hoisted so the
// vi.mock factory can reference them.
const mocks = vi.hoisted(() => ({
  archive: { mutate: vi.fn(), mutateAsync: vi.fn() },
  stop: { mutate: vi.fn() },
  preference: vi.fn(),
  savePreference: vi.fn(),
  status: vi.fn(),
}));

vi.mock("@/hooks/useConversations", async () => {
  const { conversationHooksMock } = await import("@/test/sidebarMockHelpers");
  return {
    ...conversationHooksMock(),
    useArchiveConversation: () => mocks.archive,
    useStopSession: () => mocks.stop,
  };
});

vi.mock("@/components/WorktreeStatusMark", async (original) => ({
  ...(await original<typeof WorktreeStatusMarkModule>()),
  WorktreeStatusMark: () => null,
}));
vi.mock("@/lib/archiveWorktreePreferences", () => ({
  fetchArchiveWorktreePreference: mocks.preference,
  saveArchiveWorktreePreference: mocks.savePreference,
}));
vi.mock("@/hooks/useWorktreeStatus", () => ({
  fetchSessionWorktreeStatus: mocks.status,
  useWorktreeStatus: () => ({ data: null, supported: false, isError: false, isFetching: false }),
}));

vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));

import { type Conversation, useConversations } from "@/hooks/useConversations";
import { Toaster } from "@/components/ui/sonner";
import { Sidebar } from "./Sidebar";

const useConvMock = vi.mocked(useConversations);

// Owner (permission_level null) → archivable.
const CONV: Conversation = {
  id: "conv_1",
  object: "conversation",
  title: "My Session",
  created_at: 1_700_000_000,
  updated_at: 1_700_000_000,
  labels: { "omnigent.wrapper": "claude-code-native-ui" },
  permission_level: null,
  status: "idle",
};

function mockConversations(conversations: Conversation[]) {
  const result = conversationPage(conversations);
  useConvMock.mockImplementation(() => result);
}

const CLEANUP_SERVER: ServerInfo = { ...FALLBACK_SERVER_INFO, worktree_status: true };

function LocationProbe() {
  return <output data-testid="archive-location">{useLocation().pathname}</output>;
}

function renderSidebar(serverInfo: ServerInfo = FALLBACK_SERVER_INFO, route = "/") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <CapabilitiesProvider info={serverInfo}>
        <SidebarDataProvider>
          <TooltipProvider>
            <MemoryRouter initialEntries={[route]}>
              <Routes>
                <Route
                  path="/c/:conversationId"
                  element={<Sidebar open={true} onClose={vi.fn()} />}
                />
                <Route path="*" element={<Sidebar open={true} onClose={vi.fn()} />} />
              </Routes>
              <LocationProbe />
              <Toaster />
            </MemoryRouter>
          </TooltipProvider>
        </SidebarDataProvider>
      </CapabilitiesProvider>
    </QueryClientProvider>,
  );
}

/** Open the row's action dropdown and click the archive/unarchive item. */
function clickArchive() {
  // Radix DropdownMenu opens on pointerdown, not click.
  fireEvent.pointerDown(screen.getByTestId("conversation-actions"), { button: 0 });
  fireEvent.click(screen.getByTestId("archive-conversation"));
}

beforeEach(() => {
  mocks.archive.mutate.mockReset();
  mocks.archive.mutateAsync.mockReset().mockResolvedValue({});
  mocks.stop.mutate.mockReset();
  mocks.preference.mockReset().mockResolvedValue({
    mode: "green_only",
    ask_before_archive: true,
    rebuildable_paths: ["node_modules/"],
  });
  mocks.savePreference.mockReset().mockResolvedValue(undefined);
  mocks.status.mockReset().mockResolvedValue({
    own: { state: "clean", reason: "clean worktree", path: "/opt/work/sample-app/feature-x" },
    aggregate: { state: "clean", reason: null },
    blockers: [],
    session_count: 1,
  });
});

afterEach(() => {
  cleanup();
  localStorage.clear();
});

describe("archive flow", () => {
  it("archives with a single PATCH and no client-side stop", () => {
    mockConversations([CONV]);
    renderSidebar();
    fireEvent.pointerDown(screen.getByTestId("conversation-actions"), { button: 0 });
    expect(screen.getAllByTestId("archive-conversation")).toHaveLength(1);
    expect(screen.queryByTestId("archive-keep-worktree")).toBeNull();
    fireEvent.click(screen.getByTestId("archive-conversation"));

    expect(mocks.archive.mutate).toHaveBeenCalledTimes(1);
    // Just the flag — the optimistic overlay + error reconcile live in the
    // hook, and the toast fires synchronously (not in a mutate callback, which
    // wouldn't fire once the optimistic overlay unmounts the row).
    expect(mocks.archive.mutate).toHaveBeenCalledWith({
      id: "conv_1",
      archived: true,
      keepWorktree: false,
    });
    // The server owns the stop. A client stop here would race it against
    // the same runner and put its timeouts in front of the flag flip.
    expect(mocks.stop.mutate).not.toHaveBeenCalled();
  });

  it("opens worktree details for a session without a recorded branch", async () => {
    mockConversations([CONV]);
    renderSidebar();
    fireEvent.pointerDown(screen.getByTestId("conversation-actions"), { button: 0 });
    fireEvent.click(screen.getByTestId("worktree-details"));

    expect(await screen.findByTestId("worktree-details-dialog")).toBeInTheDocument();
    expect(
      screen.getByText("Worktree details are unavailable on this server."),
    ).toBeInTheDocument();
    expect(mocks.archive.mutate).not.toHaveBeenCalled();
  });

  it("does not show an 'Archiving…' status row — the row leaves optimistically", () => {
    // No spinner: the cached `archived` flag flips in onMutate and the row
    // unmounts, like delete. (The mocked mutate doesn't model the overlay, so
    // the interactive row is still here — the point is only that no status row
    // replaced it.)
    mockConversations([CONV]);
    renderSidebar();
    clickArchive();

    expect(screen.queryByTestId("conversation-archiving")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /My Session/ })).toBeInTheDocument();
  });

  it("shows an Undo toast (with a View archived action) on archive", async () => {
    mockConversations([CONV]);
    renderSidebar();
    clickArchive();

    // The toast fires synchronously on click (the row is about to unmount, so
    // it can't wait for a mutate callback) — no need to drive onSuccess.
    // Singular copy for one session — never "session(s)".
    expect(await screen.findByText("Archived 1 session")).toBeInTheDocument();
    expect(screen.queryByText(/session\(s\)/)).not.toBeInTheDocument();
    // Undo and the View archived pointer are the toast's two actions.
    expect(screen.getByRole("button", { name: "Undo" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "View archived" })).toBeInTheDocument();
  });

  it("archives from the row's quick-archive hover button", () => {
    mockConversations([CONV]);
    renderSidebar();

    fireEvent.click(screen.getByTestId("quick-archive-conversation"));

    // Same single-PATCH contract as the kebab item, just a different affordance.
    expect(mocks.archive.mutate).toHaveBeenCalledTimes(1);
    expect(mocks.archive.mutate).toHaveBeenCalledWith({
      id: "conv_1",
      archived: true,
      keepWorktree: false,
    });
    expect(mocks.stop.mutate).not.toHaveBeenCalled();
  });

  it("unarchives from the quick button on an archived row", () => {
    // Archived rows render under the "Archived sessions" filter; the quick
    // button flips to its unarchive affordance there.
    mockConversations([{ ...CONV, archived: true }]);
    renderSidebar();
    // Radix menu opens on pointerdown; pick the Archived filter.
    fireEvent.pointerDown(screen.getByTestId("session-filter"), { button: 0 });
    fireEvent.click(screen.getByTestId("session-filter-archived"));

    fireEvent.click(screen.getByTestId("quick-archive-conversation"));

    expect(mocks.archive.mutate).toHaveBeenCalledWith({ id: "conv_1", archived: false });
  });
});

describe("worktree archive choice", () => {
  const WORKTREE_CONV: Conversation = {
    ...CONV,
    git_branch: "feature/x",
    worktree: "/opt/work/sample-app/feature-x",
  };

  it("routes keyboard archive through cancellation and a one-time tier", async () => {
    mockConversations([WORKTREE_CONV]);
    renderSidebar(CLEANUP_SERVER, "/c/conv_1");
    act(() => window.dispatchEvent(new Event(ARCHIVE_SESSION_ACTION_EVENT)));
    await screen.findByTestId("archive-worktree-dialog");
    expect(mocks.archive.mutateAsync).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() =>
      expect(screen.queryByTestId("archive-worktree-dialog")).not.toBeInTheDocument(),
    );
    expect(screen.getByTestId("archive-location")).toHaveTextContent("/c/conv_1");
    act(() => window.dispatchEvent(new Event(ARCHIVE_SESSION_ACTION_EVENT)));
    await screen.findByTestId("archive-worktree-dialog");
    fireEvent.click(screen.getByRole("radio", { name: /Delete green and yellow worktrees/ }));
    fireEvent.click(screen.getByTestId("archive-worktree-confirm"));
    await waitFor(() =>
      expect(mocks.archive.mutateAsync).toHaveBeenCalledWith({
        id: "conv_1",
        archived: true,
        keepWorktree: false,
        worktreeArchiveMode: "yellow_too",
      }),
    );
    expect(mocks.savePreference).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByTestId("archive-location")).toHaveTextContent(/^\/$/));
  });

  it("offers the first prompt even when the global default keeps every worktree", async () => {
    mocks.preference.mockResolvedValue({
      mode: "never",
      ask_before_archive: true,
      rebuildable_paths: ["node_modules/"],
    });
    mockConversations([WORKTREE_CONV]);
    renderSidebar(CLEANUP_SERVER);
    clickArchive();
    await screen.findByTestId("archive-worktree-dialog");
    expect(screen.getByRole("radio", { name: /Never delete worktrees/ })).toBeChecked();
    expect(mocks.archive.mutate).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId("archive-worktree-confirm"));
    await waitFor(() =>
      expect(mocks.archive.mutate).toHaveBeenCalledWith({
        id: "conv_1",
        archived: true,
        keepWorktree: false,
        worktreeArchiveMode: "never",
      }),
    );
  });

  it("saves the selected tier only when Don't ask again is checked", async () => {
    mockConversations([WORKTREE_CONV]);
    renderSidebar(CLEANUP_SERVER);
    clickArchive();
    await screen.findByTestId("archive-worktree-dialog");
    fireEvent.click(screen.getByRole("radio", { name: /Delete green and yellow worktrees/ }));
    fireEvent.click(screen.getByTestId("archive-worktree-remember"));
    fireEvent.click(screen.getByTestId("archive-worktree-confirm"));
    await waitFor(() =>
      expect(mocks.savePreference).toHaveBeenCalledWith({
        mode: "yellow_too",
        ask_before_archive: false,
        rebuildable_paths: ["node_modules/"],
      }),
    );
    await waitFor(() =>
      expect(mocks.archive.mutate).toHaveBeenCalledWith({
        id: "conv_1",
        archived: true,
        keepWorktree: false,
        worktreeArchiveMode: "yellow_too",
      }),
    );
  });

  it("cancels without archiving when the prompt is dismissed", async () => {
    mockConversations([WORKTREE_CONV]);
    renderSidebar(CLEANUP_SERVER);
    clickArchive();
    const dialog = await screen.findByTestId("archive-worktree-dialog");
    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(mocks.archive.mutate).not.toHaveBeenCalled();
  });

  it("archives without a prompt when Ask before archiving is off", async () => {
    mocks.preference.mockResolvedValue({
      mode: "never",
      ask_before_archive: false,
      rebuildable_paths: ["node_modules/"],
    });
    mockConversations([WORKTREE_CONV]);
    renderSidebar(CLEANUP_SERVER);
    clickArchive();
    await waitFor(() =>
      expect(mocks.archive.mutate).toHaveBeenCalledWith({
        id: "conv_1",
        archived: true,
        keepWorktree: false,
      }),
    );
    expect(screen.queryByTestId("archive-worktree-dialog")).not.toBeInTheDocument();
  });

  it("retains trees if preferences cannot be read", async () => {
    mocks.preference.mockRejectedValue(new Error("offline"));
    mockConversations([WORKTREE_CONV]);
    renderSidebar(CLEANUP_SERVER);
    clickArchive();
    await waitFor(() =>
      expect(mocks.archive.mutate).toHaveBeenCalledWith({
        id: "conv_1",
        archived: true,
        keepWorktree: true,
      }),
    );
  });

  it("offers the prompt for recorded worktrees even when the branch is absent", async () => {
    mockConversations([{ ...WORKTREE_CONV, git_branch: null }]);
    renderSidebar(CLEANUP_SERVER);
    clickArchive();
    await screen.findByTestId("archive-worktree-dialog");
    expect(mocks.archive.mutate).not.toHaveBeenCalled();
  });

  it("skips the prompt for old sessions without a bound worktree", async () => {
    mocks.status.mockResolvedValue({ own: { state: "none" }, worktrees: [] });
    mockConversations([CONV]);
    renderSidebar(CLEANUP_SERVER);
    clickArchive();
    await waitFor(() =>
      expect(mocks.archive.mutate).toHaveBeenCalledWith({
        id: "conv_1",
        archived: true,
        keepWorktree: false,
      }),
    );
    expect(mocks.status).toHaveBeenCalledWith("conv_1", true);
  });
});
