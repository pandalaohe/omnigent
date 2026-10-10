import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { useWorktreeStatus, type SessionWorktreeStatus } from "@/hooks/useWorktreeStatus";
import { WorktreeStatusMark } from "./WorktreeStatusMark";

vi.mock("@/hooks/useWorktreeStatus", () => ({ useWorktreeStatus: vi.fn() }));
const hook = vi.mocked(useWorktreeStatus);
const base: SessionWorktreeStatus = {
  own: {
    state: "clean",
    reason: null,
    path: "/opt/work/sample-app/worktrees/ui",
    branch: "feature/ui",
    merged: true,
    merge_target: "main",
    files: [],
  },
  aggregate: { state: "clean", reason: null },
  blockers: [],
  session_count: 1,
};

function show(state: string, overrides: Record<string, unknown> = {}) {
  hook.mockReturnValue({
    supported: true,
    isError: false,
    isFetching: false,
    data: { ...base, aggregate: { state, reason: null }, ...overrides },
  } as unknown as ReturnType<typeof useWorktreeStatus>);
  return render(
    <TooltipProvider delayDuration={0}>
      <a href="/sessions/one">
        <WorktreeStatusMark sessionId="one" />
      </a>
    </TooltipProvider>,
  );
}

afterEach(() => {
  cleanup();
  hook.mockReset();
});

describe("WorktreeStatusMark", () => {
  it.each([
    ["clean", "Worktrees clean", "text-success"],
    ["dirty", "Worktree has uncommitted changes", "text-warning"],
    ["unknown", "Worktree status unknown", "text-muted-foreground"],
    ["protected", "Worktree kept by rule", "text-foreground"],
    ["shared", "Worktree shared with another session", "text-foreground"],
    ["removed", "Worktree removed", "opacity-45"],
  ])("shows %s as an accessible 14px tree in a 20px slot", (state, label, color) => {
    show(state);
    const mark = screen.getByRole("img", { name: label });
    expect(mark).toHaveAttribute("data-worktree-state", state);
    expect(mark).toHaveClass("size-5", "shrink-0", color);
    expect(mark.querySelector("svg")).toHaveAttribute("width", "14");
    expect(mark.querySelector("svg")).toHaveAttribute("height", "14");
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("uses dashed branches and hollow terminals for an unknown read", () => {
    show("unknown");
    const svg = screen.getByTestId("worktree-tree-glyph");
    expect(svg.querySelector("path")?.getAttribute("stroke-dasharray")).toBe("2.4 2");
    expect(svg.querySelectorAll('rect[fill="none"]')).toHaveLength(4);
  });

  it("distinguishes rule-kept worktrees with the short bar", () => {
    show("protected");
    expect(
      screen.getByTestId("worktree-tree-glyph").querySelector('path[d="M4.5 20.5h7"]'),
    ).toBeInTheDocument();
  });

  it("hides none, unsupported, and absent sessions", () => {
    const { rerender } = show("none");
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    hook.mockReturnValue({ supported: false } as unknown as ReturnType<typeof useWorktreeStatus>);
    rerender(
      <TooltipProvider>
        <WorktreeStatusMark sessionId="one" />
      </TooltipProvider>,
    );
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    rerender(
      <TooltipProvider>
        <WorktreeStatusMark sessionId={null} />
      </TooltipProvider>,
    );
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });

  it("renders mother's aggregate and names a blocking Past child on hover", async () => {
    show("dirty", {
      blockers: [
        { session_id: "past", title: "Past child", state: "dirty", reason: "Untracked notes" },
      ],
      session_count: 2,
    });
    const mark = screen.getByRole("img", { name: "Worktree has uncommitted changes" });
    fireEvent.focus(mark);
    expect(await screen.findByText("This session: Worktrees clean")).toBeInTheDocument();
    expect(
      await screen.findByText(/Past child: Worktree has uncommitted changes/),
    ).toHaveTextContent("Untracked notes");
    expect(mark.closest("a")).toHaveAttribute("href", "/sessions/one");
  });

  it("does not paint stale green during a failed or pending read", () => {
    hook.mockReturnValue({
      supported: true,
      isError: true,
      isFetching: false,
      data: base,
    } as unknown as ReturnType<typeof useWorktreeStatus>);
    const { rerender } = render(
      <TooltipProvider>
        <WorktreeStatusMark sessionId="one" />
      </TooltipProvider>,
    );
    expect(screen.getByRole("img")).toHaveAttribute("data-worktree-state", "unknown");
    hook.mockReturnValue({
      supported: true,
      isError: false,
      isFetching: true,
      data: base,
    } as unknown as ReturnType<typeof useWorktreeStatus>);
    rerender(
      <TooltipProvider>
        <WorktreeStatusMark sessionId="one" />
      </TooltipProvider>,
    );
    expect(screen.getByRole("img")).toHaveAttribute("data-worktree-state", "unknown");
  });
});
