import { renderSidebar } from "@/test/sidebarTestHelpers";
import { conversationPage } from "@/test/sidebarMockHelpers";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/hooks/useScopeCache", () => import("@/test/mockScopeCache"));
import { SidebarDataProvider } from "@/hooks/useSidebarData";

// Tests for the sidebar kebab's "Stop session" item (moved here from the
// chat header). Contract: the item renders only for stoppable sessions
// (isSessionStoppable: host-spawned or claude-native) whose runner isn't
// known-offline, is owner-gated (disabled + tooltip for non-owners), and
// confirms through a dialog before firing the stop mutation. See
// ConversationRow in Sidebar.tsx.

import type * as RunnerHealthProviderModule from "@/hooks/RunnerHealthProvider";
import type * as SessionsApiModule from "@/lib/sessionsApi";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { TooltipProvider } from "@/components/ui/tooltip";

// Controllable stop mutation + runner-liveness lookup, declared via
// vi.hoisted so the vi.mock factories can reference them. The dialog reads
// isPending/isError on every render and reset() on open, so the stub
// carries the full mutation shape (not just mutate).
const mocks = vi.hoisted(() => ({
  stop: { mutate: vi.fn(), reset: vi.fn(), isPending: false, isError: false },
  runnerOnline: vi.fn<(id: string | undefined) => boolean | undefined>(() => undefined),
  retry: vi.fn(),
}));

vi.mock("@/lib/sessionsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof SessionsApiModule>()),
  retrySession: mocks.retry,
}));

vi.mock("@/hooks/useConversations", async () => {
  const { conversationHooksMock } = await import("@/test/sidebarMockHelpers");
  return {
    ...conversationHooksMock(),
    useStopSession: () => mocks.stop,
  };
});

vi.mock("@/hooks/RunnerHealthProvider", async (importOriginal) => ({
  ...(await importOriginal<typeof RunnerHealthProviderModule>()),
  useSessionRunnerOnline: (id: string | undefined) => mocks.runnerOnline(id),
}));

vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));
// Force a multi-user (non-local) server so the "Shared with me" tab renders —
// jsdom's default loopback origin would otherwise read as single-user and hide
// the tabs the shared-session row actions rely on.
vi.mock("@/lib/serverOrigin", () => ({ isCurrentServerLocal: () => false }));

import { type Conversation, useConversations } from "@/hooks/useConversations";
import { Sidebar } from "./Sidebar";

const useConvMock = vi.mocked(useConversations);

// Owner (permission_level null) of a host-spawned session → stoppable.
const HOST_SPAWNED: Conversation = {
  id: "conv_1",
  object: "conversation",
  title: "My Session",
  created_at: 1_700_000_000,
  updated_at: 1_700_000_000,
  labels: {},
  permission_level: null,
  host_id: "host_a1b2",
  runner_id: "runner_token_abc",
  status: "idle",
};

function mockConversations(conversations: Conversation[]) {
  const withData = conversationPage(conversations);
  useConvMock.mockImplementation(() => withData);
}

// The resume tests assert the post-resume navigation, so they mount the
// sidebar with a location probe the shared renderSidebar helper doesn't have.
function renderSidebarWithLocation() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <SidebarDataProvider>
        <TooltipProvider>
          <MemoryRouter initialEntries={["/"]}>
            <Sidebar open={true} onClose={vi.fn()} />
            <LocationProbe />
          </MemoryRouter>
        </TooltipProvider>
      </SidebarDataProvider>
    </QueryClientProvider>,
  );
}

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location">{location.pathname + location.search}</output>;
}

/** Open the row's action dropdown (Radix opens on pointerdown, not click). */
function openKebab() {
  fireEvent.pointerDown(screen.getByTestId("conversation-actions"), { button: 0 });
}

beforeEach(() => {
  mocks.stop.mutate.mockReset();
  mocks.stop.reset.mockReset();
  mocks.stop.isPending = false;
  mocks.runnerOnline.mockReset();
  mocks.runnerOnline.mockReturnValue(undefined);
  mocks.retry.mockReset();
  mocks.retry.mockResolvedValue({ recovered: true });
});

afterEach(() => {
  cleanup();
});

describe("sidebar Stop session item", () => {
  it("stops a host-spawned session after dialog confirm", () => {
    mockConversations([HOST_SPAWNED]);
    renderSidebar();
    openKebab();
    fireEvent.click(screen.getByTestId("stop-conversation"));

    // The confirm dialog gates the mutation — nothing fires on item click.
    expect(mocks.stop.mutate).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Stop session" }));
    expect(mocks.stop.mutate).toHaveBeenCalledTimes(1);
    // Failure: the dialog stopped a different row's session.
    expect(mocks.stop.mutate.mock.calls[0][0]).toBe("conv_1");
    // A failed stop reports via toast, not an in-dialog message, so the
    // mutation is given an onError handler.
    expect(typeof mocks.stop.mutate.mock.calls[0][1]?.onError).toBe("function");
    // The dialog closes immediately rather than blocking on the kill.
    expect(screen.queryByTestId("stop-session-confirm")).toBeNull();
  });

  it("closes the dialog immediately on confirm even while the stop is pending", () => {
    // The stop can take seconds. The dialog must not stay open (blocking the
    // rest of the sidebar) while it runs — it closes right away and the stop
    // continues in the background.
    mocks.stop.isPending = true;
    mockConversations([HOST_SPAWNED]);
    renderSidebar();
    openKebab();
    fireEvent.click(screen.getByTestId("stop-conversation"));

    fireEvent.click(screen.getByRole("button", { name: "Stop session" }));
    expect(mocks.stop.mutate).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId("stop-session-confirm")).toBeNull();
  });

  it("shows for a CLI-launched claude-native session (no host)", () => {
    // Regression guard: the wrapper label keeps the item without a host.
    mockConversations([
      {
        ...HOST_SPAWNED,
        host_id: undefined,
        runner_id: undefined,
        labels: { "omnigent.wrapper": "claude-code-native-ui" },
      },
    ]);
    renderSidebar();
    openKebab();
    expect(screen.getByTestId("stop-conversation")).toBeInTheDocument();
  });

  it("is hidden for a local in-process runner (runner_id, no host_id)", () => {
    // runner_id but no host_id → no kill path → hidden.
    mockConversations([{ ...HOST_SPAWNED, host_id: undefined }]);
    renderSidebar();
    openKebab();
    expect(screen.queryByTestId("stop-conversation")).toBeNull();
  });

  it("is hidden when the runner is known offline", () => {
    // The session is already stopped — no destructive control to offer.
    mocks.runnerOnline.mockReturnValue(false);
    mockConversations([HOST_SPAWNED]);
    renderSidebar();
    openKebab();
    expect(screen.queryByTestId("stop-conversation")).toBeNull();
    expect(screen.getByTestId("resume-conversation")).toBeInTheDocument();
  });

  it("resumes from the same menu without sending a message", async () => {
    mocks.runnerOnline.mockReturnValue(false);
    mockConversations([HOST_SPAWNED]);
    renderSidebarWithLocation();
    openKebab();
    fireEvent.click(screen.getByTestId("resume-conversation"));
    await waitFor(() => expect(mocks.retry).toHaveBeenCalledWith("conv_1"));
    await waitFor(() =>
      expect(screen.getByTestId("location")).toHaveTextContent("/c/conv_1?view=terminal"),
    );
    expect(mocks.retry).toHaveBeenCalledTimes(1);
    expect(mocks.stop.mutate).not.toHaveBeenCalled();
  });

  it("reports failed recovery and keeps the session available for retry", async () => {
    mocks.runnerOnline.mockReturnValue(false);
    mocks.retry.mockRejectedValue(new Error("Host is offline"));
    const notices: string[] = [];
    const listener = (event: Event) => notices.push((event as CustomEvent).detail.content);
    window.addEventListener("omnigent:toast", listener);
    try {
      mockConversations([HOST_SPAWNED]);
      renderSidebarWithLocation();
      openKebab();
      fireEvent.click(screen.getByTestId("resume-conversation"));
      await waitFor(() =>
        expect(notices).toContain("Couldn't resume the session: Host is offline"),
      );
      expect(screen.getByTestId("location")).toHaveTextContent("/");
      openKebab();
      expect(screen.getByTestId("resume-conversation")).not.toHaveAttribute("data-disabled");
    } finally {
      window.removeEventListener("omnigent:toast", listener);
    }
  });

  it("disables duplicate resume requests while recovery is pending", async () => {
    mocks.runnerOnline.mockReturnValue(false);
    mocks.retry.mockReturnValue(new Promise(() => {}));
    mockConversations([HOST_SPAWNED]);
    renderSidebar();
    openKebab();
    fireEvent.click(screen.getByTestId("resume-conversation"));
    await waitFor(() => expect(mocks.retry).toHaveBeenCalledTimes(1));
    openKebab();
    expect(screen.getByTestId("resume-conversation")).toHaveAttribute("data-disabled");
    expect(screen.getByTestId("resume-conversation")).toHaveTextContent("Resuming…");
  });

  it("does not offer resume without a host", () => {
    mocks.runnerOnline.mockReturnValue(false);
    mockConversations([{ ...HOST_SPAWNED, host_id: undefined }]);
    renderSidebar();
    openKebab();
    expect(screen.queryByTestId("resume-conversation")).toBeNull();
  });

  it("disables resume for non-owners", () => {
    mocks.runnerOnline.mockReturnValue(false);
    mockConversations([{ ...HOST_SPAWNED, owner: "other@example.com" }]);
    renderSidebar();
    fireEvent.pointerDown(screen.getByTestId("session-filter"), {
      button: 0,
      ctrlKey: false,
      pointerType: "mouse",
    });
    fireEvent.click(screen.getByTestId("session-filter-shared"));
    openKebab();
    expect(screen.getByTestId("resume-conversation")).toHaveAttribute("data-disabled");
    expect(mocks.retry).not.toHaveBeenCalled();
  });

  it("is disabled for non-owners even on a stoppable session", () => {
    // Owner-gated server-side; a shared viewer (another user owns it) sees it
    // disabled. A non-owner session lives on the "Shared with me" tab, so
    // switch there before opening its kebab.
    mockConversations([{ ...HOST_SPAWNED, owner: "other@example.com" }]);
    renderSidebar();
    // Radix Tabs triggers activate on mousedown (primary button), not click.
    fireEvent.pointerDown(screen.getByTestId("session-filter"), {
      button: 0,
      ctrlKey: false,
      pointerType: "mouse",
    });
    fireEvent.click(screen.getByTestId("session-filter-shared"));
    openKebab();
    const item = screen.getByTestId("stop-conversation");
    expect(item).toHaveAttribute("data-disabled");
  });
});
