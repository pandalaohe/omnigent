// The desktop Workspace rail keeps a second width while it shows a browser soft
// tab or an opened file, so wide content gets more room without disturbing the
// user's usual rail width. This drives the real AppShell + resize hook and
// reads the rendered rail width.

import { SidebarDataProvider } from "@/hooks/useSidebarData";

import type * as UseTerminalsModule from "@/hooks/useTerminals";
import type * as UseChildSessionsModule from "@/hooks/useChildSessions";
import type * as UseSessionModule from "@/hooks/useSession";
import type * as UseConversationsModule from "@/hooks/useConversations";
import type * as UsePullRequestsModule from "@/hooks/usePullRequests";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { readPanelSizePreference } from "@/lib/panelSizePreferences";
import { resetWidthStoreForTesting } from "@/hooks/useResizableInlinePanel";
import {
  writeWidenWorkspaceForContent,
  writeWorkspacePanelDefault,
} from "@/lib/workspacePanelPreferences";

vi.mock("@/hooks/useConversations", async (importOriginal) => ({
  ...(await importOriginal<typeof UseConversationsModule>()),
  useConversations: vi.fn(),
  useStopSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
}));
vi.mock("@/hooks/useTerminals", async (importOriginal) => ({
  ...(await importOriginal<typeof UseTerminalsModule>()),
  useTerminals: vi.fn(() => ({ terminals: [], isLoading: false, error: null })),
}));
vi.mock("@/hooks/useWorkspaceChangedFiles", () => ({
  useWorkspaceEnvironment: vi.fn(() => ({
    data: { available: true, root: null },
    isLoading: false,
  })),
  useWorkspaceChangedFiles: vi.fn(() => ({
    data: { data: [] },
    isSuccess: true,
    isLoading: false,
  })),
}));
vi.mock("@/hooks/usePullRequests", async (importOriginal) => ({
  ...(await importOriginal<typeof UsePullRequestsModule>()),
  usePullRequestInfo: vi.fn(() => ({ data: undefined, isLoading: true })),
}));
vi.mock("@/hooks/useChildSessions", async (importOriginal) => ({
  ...(await importOriginal<typeof UseChildSessionsModule>()),
  useChildSessions: vi.fn(() => ({ children: [], isLoading: false, error: null })),
}));
vi.mock("@/hooks/useSession", async (importOriginal) => ({
  ...(await importOriginal<typeof UseSessionModule>()),
  useSession: vi.fn(() => ({ session: null, isLoading: false, error: null })),
}));
vi.mock("@/hooks/useAgents", () => ({
  useSessionAgent: vi.fn(() => ({ data: undefined })),
  useCreateMcpServer: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useUpdateMcpServer: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useDeleteMcpServer: () => ({ mutate: vi.fn(), isPending: false, error: null }),
}));
vi.mock("./Sidebar", () => ({
  Sidebar: () => <div data-testid="sidebar" />,
  isMobileViewport: vi.fn(() => false),
}));
vi.mock("./PullRequestPanel", () => ({
  PullRequestPanel: () => <div data-testid="github-panel">Pull request details</div>,
}));
vi.mock("./FilesPanel", () => ({
  FilesPanel: () => <div data-testid="files-panel" />,
}));
vi.mock("./FileViewer", () => ({
  FileViewer: () => <div data-testid="file-viewer" />,
}));
vi.mock("./InlineTerminalsSection", () => ({
  InlineTerminalsSection: () => <div data-testid="inline-terminals-section" />,
}));
vi.mock("./FilesPanelDrawer", () => ({
  FilesPanelDrawer: () => <div data-testid="files-panel-drawer" />,
}));
vi.mock("./TerminalsPanel", () => ({
  TerminalsPanel: () => <div data-testid="terminals-panel" />,
}));

import { AppShell } from "./AppShell";
import { useFileViewer } from "./FileViewerContext";
import { isMobileViewport } from "./Sidebar";
import { usePullRequestInfo } from "@/hooks/usePullRequests";
import { useConversations } from "@/hooks/useConversations";

const usePullRequestInfoMock = vi.mocked(usePullRequestInfo);
const originalInnerWidth = window.innerWidth;
const realMatchMedia = window.matchMedia;

function setInnerWidth(px: number): void {
  Object.defineProperty(window, "innerWidth", { configurable: true, writable: true, value: px });
}

function FileOpenProbe() {
  const openFile = useFileViewer();
  return (
    <button type="button" onClick={() => openFile?.("README.md")}>
      Open file
    </button>
  );
}

afterEach(() => {
  cleanup();
  setInnerWidth(originalInnerWidth);
  window.matchMedia = realMatchMedia;
});

beforeEach(() => {
  // The rail persists per-session state (selected tab, width) in localStorage;
  // clear it so one test's writes can't leak into another. resetWidthStore
  // reloads the module-level stores from the cleared storage.
  localStorage.clear();
  resetWidthStoreForTesting();
  writeWorkspacePanelDefault("open");
  sessionStorage.clear();
  setInnerWidth(2000);
  // Open the desktop left sidebar so `reservedPx` is its 320px default: the
  // wide default then lands at max(600, round((2000 - 320) / 2)) = 840.
  window.matchMedia = ((query: string) => ({
    matches: query === "(min-width: 768px)",
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })) as typeof window.matchMedia;
  vi.mocked(isMobileViewport).mockReturnValue(false);
  usePullRequestInfoMock.mockReset();
  usePullRequestInfoMock.mockReturnValue({ data: undefined, isLoading: true } as ReturnType<
    typeof usePullRequestInfo
  >);
  vi.mocked(useConversations).mockReset();
  vi.mocked(useConversations).mockReturnValue({
    data: {
      pages: [
        {
          data: [
            {
              id: "conv_ws",
              object: "conversation" as const,
              title: null,
              created_at: 0,
              updated_at: 0,
              labels: {},
              permission_level: null,
              host_id: null,
              runner_id: null,
            },
          ],
          first_id: null,
          last_id: null,
          has_more: false,
        },
      ],
      pageParams: [undefined],
    },
  } as never);
});

function renderShell() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <SidebarDataProvider>
        <TooltipProvider>
          <MemoryRouter initialEntries={["/c/conv_ws"]}>
            <Routes>
              <Route element={<AppShell />}>
                <Route path="c/:conversationId" element={<FileOpenProbe />} />
              </Route>
            </Routes>
          </MemoryRouter>
        </TooltipProvider>
      </SidebarDataProvider>
    </QueryClientProvider>,
  );
}

const rail = () => screen.getByRole("complementary", { name: "Workspace" });

// Radix tabs activate on the full pointerdown→mousedown→mouseup→click sequence
// in jsdom; a bare click leaves the tab in its prior state.
function selectTab(name: RegExp) {
  const tab = screen.getByRole("tab", { name });
  fireEvent.pointerDown(tab, { button: 0 });
  fireEvent.mouseDown(tab, { button: 0 });
  fireEvent.mouseUp(tab, { button: 0 });
  fireEvent.click(tab);
}

describe("Workspace rail content width", () => {
  it("widens for an opened file and returns to the normal width on another tab", () => {
    renderShell();

    expect(rail().style.width).toBe("600px");

    fireEvent.click(screen.getByRole("button", { name: "Open file" }));
    expect(rail().style.width).toBe("840px");
    expect(readPanelSizePreference("inlinePanelWidthPx")).toBeNull();

    selectTab(/^Agents/);
    expect(rail().style.width).toBe("600px");
    expect(readPanelSizePreference("inlinePanelWidthPx")).toBeNull();
  });

  it("keeps the normal width for an opened file when the setting is off", () => {
    writeWidenWorkspaceForContent(false);

    renderShell();

    fireEvent.click(screen.getByRole("button", { name: "Open file" }));
    expect(rail().style.width).toBe("600px");
  });

  it("leaves the phone layout's rail width alone when a file opens", () => {
    // Phone width: the rail is hidden and the file opens in its own drawer, so
    // the rail width (and the offsets derived from it) must not move.
    window.matchMedia = ((query: string) => ({
      matches: query === "(max-width: 767.98px)",
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    })) as typeof window.matchMedia;
    vi.mocked(isMobileViewport).mockReturnValue(true);

    renderShell();
    const phoneRail = () => screen.getByRole("complementary", { name: "Workspace", hidden: true });
    // The sidebar starts closed on a phone, so an unguarded wide default would
    // be max(600, 2000 / 2) = 1000.
    expect(phoneRail().style.width).toBe("600px");

    fireEvent.click(screen.getByRole("button", { name: "Open file" }));
    expect(phoneRail().style.width).toBe("600px");
  });
});
