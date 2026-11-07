import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { MouseEvent, ReactNode } from "react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type * as UseChildSessionsModule from "@/hooks/useChildSessions";
import type { ChildSessionInfo } from "@/hooks/useChildSessions";
import { writeOpenSubagentsInPanel } from "@/lib/subagentPanelPreferences";
import { SubagentPanelOpenerContext } from "./subagentPanelOpener";
import { SubagentsGraphView } from "./SubagentsGraphView";

// The real canvas exhausts jsdom's memory; this stand-in renders one button per
// node and forwards clicks the way React Flow's `onNodeClick` does.
interface StubNode {
  id: string;
  data: { sessionId: string };
}
const flow = vi.hoisted(() => ({
  zoomIn: vi.fn(),
  zoomOut: vi.fn(),
  setCenter: vi.fn(),
  viewportInitialized: true,
}));
vi.mock("@xyflow/react", () => ({
  ReactFlow: ({
    nodes,
    onNodeClick,
    children,
    minZoom,
  }: {
    nodes: StubNode[];
    onNodeClick: (event: MouseEvent, node: StubNode) => void;
    children?: ReactNode;
    minZoom?: number;
  }) => (
    <div data-testid="graph-canvas" data-min-zoom={minZoom}>
      {nodes.map((node) => (
        <button key={node.id} type="button" onClick={(event) => onNodeClick(event, node)}>
          {node.data.sessionId}
        </button>
      ))}
      {children}
    </div>
  ),
  Background: () => null,
  Handle: () => null,
  Position: { Top: "top", Bottom: "bottom" },
  useReactFlow: () => flow,
}));
vi.mock("@xyflow/react/dist/style.css", () => ({}));

const child: ChildSessionInfo = {
  id: "conv_child",
  title: "researcher:auth",
  task_summary: null,
  tool: "researcher",
  session_name: "auth",
  current_task_status: null,
  busy: false,
  last_message_preview: null,
  pending_elicitations_count: 0,
};
// Stable arrays: the view compares child lists by identity across renders.
const rootChildren = [child];
const noChildren: ChildSessionInfo[] = [];
vi.mock("@/hooks/useChildSessions", async (importOriginal) => ({
  ...(await importOriginal<typeof UseChildSessionsModule>()),
  useChildSessions: (id: string | null) => ({
    children: id === "conv_root" ? rootChildren : noChildren,
    isLoading: false,
    error: null,
  }),
}));
vi.mock("@/hooks/useSession", () => ({
  useSession: () => ({ session: null, isLoading: false, error: null }),
}));

function CurrentPath() {
  const location = useLocation();
  return <output data-testid="current-path">{location.pathname + location.search}</output>;
}

function renderGraph(
  opener: (id: string) => boolean,
  initialEntry = "/c/conv_root",
  conversationId = "conv_root",
) {
  render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <SubagentPanelOpenerContext.Provider value={opener}>
        <SubagentsGraphView conversationId={conversationId} rootSessionId="conv_root" />
      </SubagentPanelOpenerContext.Provider>
      <CurrentPath />
    </MemoryRouter>,
  );
}

afterEach(() => {
  cleanup();
  localStorage.clear();
  rootChildren.splice(1);
  vi.clearAllMocks();
});

describe("SubagentsGraphView readable viewport", () => {
  it("opens a large tree around the current session at normal text size", () => {
    rootChildren.push(...Array.from({ length: 31 }, (_, i) => ({ ...child, id: `sibling-${i}` })));
    renderGraph(() => false);

    expect(flow.setCenter).toHaveBeenCalledWith(0, 30, { zoom: 1, duration: 0 });
    expect(screen.getByTestId("graph-canvas")).toHaveAttribute("data-min-zoom", "0.85");
    expect(screen.getByRole("button", { name: "sibling-30" })).toBeInTheDocument();
  });

  it("resets the viewport around the selected child instead of shrinking the tree", () => {
    renderGraph(() => false, "/c/conv_child", "conv_child");
    fireEvent.click(screen.getByRole("button", { name: "Reset view" }));

    expect(flow.setCenter).toHaveBeenLastCalledWith(0, 120, { zoom: 1, duration: 200 });
  });
});

describe("SubagentsGraphView node clicks", () => {
  it("opens a sub-agent node in the Workspace panel when the setting is on", () => {
    writeOpenSubagentsInPanel(true);
    const opener = vi.fn(() => true);
    renderGraph(opener);

    fireEvent.click(screen.getByRole("button", { name: "conv_child" }));

    expect(opener).toHaveBeenCalledWith("conv_child");
    expect(screen.getByTestId("current-path")).toHaveTextContent("/c/conv_root");
  });

  it("navigates to a sub-agent node when the setting is off", () => {
    const opener = vi.fn(() => true);
    renderGraph(opener);

    fireEvent.click(screen.getByRole("button", { name: "conv_child" }));

    expect(opener).not.toHaveBeenCalled();
    expect(screen.getByTestId("current-path")).toHaveTextContent("/c/conv_child");
  });

  it("navigates when the opener refuses the node, as AppShell's does for the root", () => {
    writeOpenSubagentsInPanel(true);
    const opener = vi.fn((id: string) => id !== "conv_root");
    // Navigating to the root drops the file viewer param, so the URL shows the click landed.
    renderGraph(opener, "/c/conv_root?file=notes.md");

    fireEvent.click(screen.getByRole("button", { name: "conv_root" }));

    expect(opener).toHaveBeenCalledWith("conv_root");
    expect(screen.getByTestId("current-path").textContent).toBe("/c/conv_root");
  });
});
