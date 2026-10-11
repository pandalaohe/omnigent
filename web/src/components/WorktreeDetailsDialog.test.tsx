import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useWorktreeStatus, type SessionWorktreeStatus } from "@/hooks/useWorktreeStatus";
import { WorktreeDetailsDialog } from "./WorktreeDetailsDialog";

vi.mock("@/hooks/useWorktreeStatus", () => ({ useWorktreeStatus: vi.fn() }));
const hook = vi.mocked(useWorktreeStatus);
afterEach(() => {
  cleanup();
  hook.mockReset();
});

function show(data: SessionWorktreeStatus | undefined, extra = {}) {
  hook.mockReturnValue({
    supported: true,
    isError: false,
    isFetching: false,
    data,
    ...extra,
  } as ReturnType<typeof useWorktreeStatus>);
  return render(<WorktreeDetailsDialog sessionId="one" open onOpenChange={vi.fn()} />);
}

describe("Worktree details", () => {
  it("shows full local file classes, branch retention and child reasons", () => {
    show({
      own: {
        state: "local",
        reason: "local files",
        path: "/opt/work/sample-app/ui",
        branch: "feature/ui",
        merged: false,
        merge_target: "main",
        files: [
          { path: "dist/", status: "!!", kind: "rebuildable" },
          { path: "notes.md", status: "!!", kind: "local" },
        ],
      },
      aggregate: { state: "dirty", reason: "source changes" },
      session_count: 2,
      blockers: [
        {
          session_id: "child",
          title: "Child",
          state: "dirty",
          reason: "uncommitted source changes",
        },
      ],
    });
    expect(screen.getByText("!! dist/")).toBeInTheDocument();
    expect(screen.getByText("Rebuildable local files")).toBeInTheDocument();
    expect(screen.getByText("Local files (Git ignored)")).toBeInTheDocument();
    expect(screen.getByText("feature/ui")).toBeInTheDocument();
    expect(screen.getByText(/Not merged into main/)).toBeInTheDocument();
    expect(screen.getByText("uncommitted source changes")).toBeInTheDocument();
  });

  it("explains that a failed read keeps the tree", () => {
    show(undefined, { isError: true });
    expect(screen.getByRole("alert")).toHaveTextContent("It will be kept");
  });

  it("does not request details while closed", () => {
    hook.mockReturnValue({ supported: true } as ReturnType<typeof useWorktreeStatus>);
    render(<WorktreeDetailsDialog sessionId="one" open={false} onOpenChange={vi.fn()} />);
    expect(hook).toHaveBeenCalledWith(null);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});
