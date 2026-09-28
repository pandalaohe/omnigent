// Selection popup: the "Open in panel" action and its debounced lookup.
//
// The lookup hook is mocked so these tests pin the popup's decision logic:
// direct open for a single settled match, a chooser list for several or for a
// truncated search, and nothing at all for prose, a pending lookup, or a page
// without a FileViewer.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FileViewerContext } from "@/shell/FileViewerContext";

const hookState = vi.hoisted(() => ({
  matches: [] as { path: string; line: number | null }[],
  pending: false,
  truncated: false,
  texts: [] as string[],
}));

vi.mock("@/hooks/useSelectionFileMatch", () => ({
  useSelectionFileMatch: (_conversationId: string | undefined, text: string) => {
    hookState.texts.push(text);
    // A hidden popup passes "" — model the hook's own disable.
    return {
      matches: text ? hookState.matches : [],
      pending: hookState.pending,
      truncated: text ? hookState.truncated : false,
    };
  },
}));

import { SelectionPopup } from "./ChatPage";

const openFile = vi.fn();

// Module scope so the provider value is a constant (jsx-no-constructed-context-values).
const VIEWER = {
  openFile,
  openGithubTab: () => {},
  isChangedPath: () => false,
  conversationId: "conv_1" as string | undefined,
  workspaceRoot: "/home/u/ws" as string | null,
  workspaceHome: "/home/u" as string | null,
};

function Harness() {
  const ref = useRef<HTMLDivElement>(null);
  return (
    <FileViewerContext.Provider value={VIEWER}>
      <div ref={ref} data-testid="selection-surface">
        report/page2.html
      </div>
      <SelectionPopup containerRef={ref} onReply={() => {}} />
    </FileViewerContext.Provider>
  );
}

function BareHarness() {
  const ref = useRef<HTMLDivElement>(null);
  return (
    <>
      <div ref={ref} data-testid="selection-surface">
        report/page2.html
      </div>
      <SelectionPopup containerRef={ref} onReply={() => {}} />
    </>
  );
}

function selectText(container: HTMLElement, text: string) {
  const selection = {
    isCollapsed: false,
    rangeCount: 1,
    anchorNode: container,
    toString: () => text,
    getRangeAt: () => ({
      getBoundingClientRect: () => ({ left: 10, top: 20, width: 30, height: 10 }),
    }),
    removeAllRanges: vi.fn(),
  };
  vi.spyOn(window, "getSelection").mockReturnValue(selection as unknown as Selection);
  fireEvent.mouseUp(document);
  return selection;
}

const openButton = () => screen.queryByRole("button", { name: /open in panel/i });

beforeEach(() => {
  openFile.mockClear();
  hookState.matches = [];
  hookState.pending = false;
  hookState.truncated = false;
  hookState.texts = [];
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("selection Open in panel", () => {
  it("debounces the lookup, then opens a single exact match", async () => {
    hookState.matches = [{ path: "report/page2.html", line: null }];
    render(<Harness />);

    const selection = selectText(screen.getByTestId("selection-surface"), "report/page2.html");

    // The popup appears immediately, but the lookup text waits for the debounce.
    expect(hookState.texts).not.toContain("report/page2.html");
    const button = await screen.findByRole("button", { name: /open in panel/i });
    expect(hookState.texts).toContain("report/page2.html");

    fireEvent.click(button);
    expect(openFile).toHaveBeenCalledWith("report/page2.html", undefined);
    expect(selection.removeAllRanges).toHaveBeenCalled();
    expect(openButton()).toBeNull();
  });

  it("opens a unique basename match", async () => {
    hookState.matches = [{ path: "report/sub/page3.html", line: null }];
    render(<Harness />);

    selectText(screen.getByTestId("selection-surface"), "page3.html");
    fireEvent.click(await screen.findByRole("button", { name: /open in panel/i }));

    expect(openFile).toHaveBeenCalledWith("report/sub/page3.html", undefined);
  });

  it("opens a partial basename at its full path", async () => {
    hookState.matches = [{ path: "weekly_chat-link-panel_report.html", line: null }];
    render(<Harness />);

    selectText(screen.getByTestId("selection-surface"), "link-panel_report.html");
    fireEvent.click(await screen.findByRole("button", { name: /open in panel/i }));

    expect(openFile).toHaveBeenCalledWith("weekly_chat-link-panel_report.html", undefined);
  });

  it("offers a chooser for several matches", async () => {
    hookState.matches = [
      { path: "report/a/index.html", line: null },
      { path: "report/b/index.html", line: null },
    ];
    render(<Harness />);

    selectText(screen.getByTestId("selection-surface"), "index.html");
    fireEvent.click(await screen.findByRole("button", { name: /open in panel/i }));

    expect(openFile).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "report/b/index.html" }));
    expect(openFile).toHaveBeenCalledWith("report/b/index.html", undefined);
    expect(openButton()).toBeNull();
  });

  it("offers a chooser for a truncated single hit and says it may be incomplete", async () => {
    hookState.matches = [{ path: "report/only/index.html", line: null }];
    hookState.truncated = true;
    render(<Harness />);

    selectText(screen.getByTestId("selection-surface"), "index.html");
    fireEvent.click(await screen.findByRole("button", { name: /open in panel/i }));

    expect(openFile).not.toHaveBeenCalled();
    expect(screen.getByText("Results may be incomplete")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "report/only/index.html" }));
    expect(openFile).toHaveBeenCalledWith("report/only/index.html", undefined);
  });

  it("hides the button for prose", async () => {
    render(<Harness />);

    selectText(screen.getByTestId("selection-surface"), "the quick fix");

    await waitFor(() => expect(hookState.texts).toContain("the quick fix"));
    expect(openButton()).toBeNull();
  });

  it("hides the button while the lookup is pending", async () => {
    hookState.matches = [{ path: "report/page2.html", line: null }];
    hookState.pending = true;
    render(<Harness />);

    selectText(screen.getByTestId("selection-surface"), "report/page2.html");

    await waitFor(() => expect(hookState.texts).toContain("report/page2.html"));
    expect(openButton()).toBeNull();
  });

  it("hides the button without a FileViewer", async () => {
    hookState.matches = [{ path: "report/page2.html", line: null }];
    render(<BareHarness />);

    selectText(screen.getByTestId("selection-surface"), "report/page2.html");

    await waitFor(() => expect(hookState.texts).toContain("report/page2.html"));
    expect(openButton()).toBeNull();
  });
});
