// Tests the sidebar row menu's "Copy session ID" action: selecting it copies
// the session's id and toasts on success or failure.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { SidebarDataProvider } from "@/hooks/useSidebarData";

vi.mock("@/hooks/useScopeCache", () => import("@/test/mockScopeCache"));

vi.mock("@/hooks/useConversations", () => ({
  useConversations: vi.fn(),
  useConnectedConversations: () => [],
  useStopAndDeleteConversation: () => ({
    mutate: vi.fn(),
    reset: vi.fn(),
    isPending: false,
    isError: false,
  }),
  usePinnedConversations: () => ({
    data: { conversations: [], filterHonored: true },
    isSuccess: true,
  }),
  useTogglePinnedConversation: () => ({ mutate: vi.fn() }),
  setConversationPinned: vi.fn(() => Promise.resolve({})),
  PINNED_CONVERSATIONS_KEY: ["pinned-conversations"],
  useRenameConversation: () => ({ mutate: vi.fn() }),
  useLeaveSession: () => ({ mutate: vi.fn(), isPending: false }),
  useArchiveConversation: () => ({ mutate: vi.fn() }),
  useBulkArchiveConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkDeleteConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkMoveToProject: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkStopSessions: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useStopSession: () => ({ mutate: vi.fn() }),
  useProjects: () => ({ data: [] }),
  useProjectSessions: () => ({
    data: undefined,
    isLoading: false,
    hasNextPage: false,
    isFetchingNextPage: false,
    fetchNextPage: vi.fn(),
  }),
  useMoveToProject: () => ({ mutate: vi.fn() }),
  useDeleteProject: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useRenameProject: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useCreateProject: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useProjectConfig: () => ({ data: undefined, isLoading: false }),
  useUpdateProjectConfig: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  fetchProjectSessionIds: () => Promise.resolve([]),
  PROJECT_LABEL_KEY: "omni_project",
}));

vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));
vi.mock("@/lib/clipboard", () => ({ copyText: vi.fn() }));
vi.mock("@/components/ui/toast", () => ({ showToast: vi.fn() }));

import { showToast } from "@/components/ui/toast";
import { type Conversation, useConversations } from "@/hooks/useConversations";
import { copyText } from "@/lib/clipboard";
import { Sidebar } from "./Sidebar";

const useConvMock = vi.mocked(useConversations);
const copyTextMock = vi.mocked(copyText);
const showToastMock = vi.mocked(showToast);

function conv(id: string, title: string): Conversation {
  return {
    id,
    object: "conversation",
    title,
    created_at: 1_700_000_000,
    updated_at: 1_700_000_000,
    labels: {},
    // owner absent → the viewer owns it; idle → the unread dot can show.
    permission_level: null,
    status: "idle",
  };
}

const CONV_A = conv("conv_a", "Session Alpha");
const CONV_B = conv("conv_b", "Session Beta");

function mockConversations(conversations: Conversation[]) {
  const withData = {
    data: {
      pages: [
        {
          data: conversations,
          first_id: conversations[0]?.id ?? null,
          last_id: conversations.at(-1)?.id ?? null,
          has_more: false,
        },
      ],
      pageParams: [undefined],
    },
    isLoading: false,
    isError: false,
    error: null,
    fetchNextPage: vi.fn(),
    hasNextPage: false,
    isFetchingNextPage: false,
  } as unknown as ReturnType<typeof useConversations>;
  useConvMock.mockImplementation(() => withData);
}

function renderSidebar() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <SidebarDataProvider>
        <TooltipProvider>
          <MemoryRouter initialEntries={["/"]}>
            <Sidebar open={true} onClose={vi.fn()} />
          </MemoryRouter>
        </TooltipProvider>
      </SidebarDataProvider>
    </QueryClientProvider>,
  );
}

function rowFor(title: string): HTMLElement {
  return screen.getByRole("link", { name: new RegExp(title) }).closest("li") as HTMLElement;
}

function openKebab(row: HTMLElement) {
  // Radix DropdownMenu opens on pointerdown, not click.
  fireEvent.pointerDown(within(row).getByTestId("conversation-actions"), { button: 0 });
}

beforeEach(() => {
  mockConversations([CONV_A, CONV_B]);
  copyTextMock.mockReset();
  copyTextMock.mockResolvedValue(undefined);
  showToastMock.mockReset();
});

afterEach(() => {
  cleanup();
});

describe("copy session id menu action", () => {
  it("copies the row's session id and toasts on success", async () => {
    renderSidebar();

    openKebab(rowFor("Session Alpha"));
    expect(screen.getByText("Copy session ID")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("copy-session-id"));

    expect(copyTextMock).toHaveBeenCalledWith("conv_a");
    await vi.waitFor(() => expect(showToastMock).toHaveBeenCalledWith("Session ID copied"));
  });

  it("toasts a failure message when the copy rejects", async () => {
    copyTextMock.mockRejectedValue(new Error("Clipboard API not available"));
    renderSidebar();

    openKebab(rowFor("Session Beta"));
    fireEvent.click(screen.getByTestId("copy-session-id"));

    expect(copyTextMock).toHaveBeenCalledWith("conv_b");
    await vi.waitFor(() =>
      expect(showToastMock).toHaveBeenCalledWith("Couldn't copy the session ID"),
    );
  });
});
