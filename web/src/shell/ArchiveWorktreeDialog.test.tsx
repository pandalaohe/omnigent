import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Conversation } from "@/hooks/useConversations";
import type * as ArchivePreferences from "@/lib/archiveWorktreePreferences";
import type * as HostModule from "@/lib/host";
import {
  fetchArchiveWorktreePreference,
  saveArchiveWorktreePreference,
} from "@/lib/archiveWorktreePreferences";
import { getOmnigentHostGeneration } from "@/lib/host";
import { useArchiveWorktreePrompt } from "./ArchiveWorktreeDialog";

vi.mock("@/lib/CapabilitiesContext", () => ({ useServerInfo: () => ({ worktree_status: true }) }));
vi.mock("@/lib/archiveWorktreePreferences", async (original) => ({
  ...(await original<typeof ArchivePreferences>()),
  fetchArchiveWorktreePreference: vi.fn(),
  saveArchiveWorktreePreference: vi.fn(),
}));
vi.mock("@/lib/host", async (original) => ({
  ...(await original<typeof HostModule>()),
  getOmnigentHostGeneration: vi.fn(() => 1),
}));
const preference = {
  mode: "never" as const,
  ask_before_archive: true,
  rebuildable_paths: ["dist/"],
};
const conversation = {
  id: "one",
  git_branch: "feature/ui",
  workspace: "/opt/work/sample-app/ui",
} as Conversation;
const proceed = vi.fn();
const cancel = vi.fn();

function Harness({ scope = "one", bound = true }: { scope?: string; bound?: boolean }) {
  const prompt = useArchiveWorktreePrompt(scope);
  return (
    <>
      <button
        type="button"
        onClick={() =>
          prompt.requestArchive(
            [bound ? conversation : { ...conversation, git_branch: null }],
            proceed,
            cancel,
          )
        }
      >
        Start archive
      </button>
      {prompt.dialog}
    </>
  );
}

beforeEach(() => {
  proceed.mockReset();
  cancel.mockReset();
  vi.mocked(getOmnigentHostGeneration).mockReturnValue(1);
  vi.mocked(fetchArchiveWorktreePreference).mockReset().mockResolvedValue(preference);
  vi.mocked(saveArchiveWorktreePreference).mockReset().mockResolvedValue(undefined);
});
afterEach(() => cleanup());

describe("archive policy prompt", () => {
  it("keeps the first-archive prompt with default never and cancels without archiving", async () => {
    render(<Harness />);
    fireEvent.click(screen.getByText("Start archive"));
    expect(await screen.findByRole("radio", { name: /Never delete worktrees/ })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Don't ask again" })).not.toBeChecked();
    fireEvent.click(screen.getByText("Cancel"));
    expect(proceed).not.toHaveBeenCalled();
    expect(cancel).toHaveBeenCalledOnce();
  });

  it.each(["never", "green_only", "yellow_too"] as const)(
    "applies %s only to this archive without remembering",
    async (mode) => {
      render(<Harness />);
      fireEvent.click(screen.getByText("Start archive"));
      await screen.findByRole("dialog");
      fireEvent.click(document.querySelector(`input[value="${mode}"]`)!);
      fireEvent.click(screen.getByTestId("archive-worktree-confirm"));
      await waitFor(() => expect(proceed).toHaveBeenCalledWith(new Set(), mode));
      expect(saveArchiveWorktreePreference).not.toHaveBeenCalled();
    },
  );

  it("remembers the tier and disables future asks before proceeding", async () => {
    render(<Harness />);
    fireEvent.click(screen.getByText("Start archive"));
    await screen.findByRole("dialog");
    fireEvent.click(screen.getByRole("radio", { name: /Delete green and yellow worktrees/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Don't ask again" }));
    fireEvent.click(screen.getByTestId("archive-worktree-confirm"));
    await waitFor(() => expect(proceed).toHaveBeenCalledWith(new Set(), "yellow_too"));
    expect(saveArchiveWorktreePreference).toHaveBeenCalledWith({
      ...preference,
      mode: "yellow_too",
      ask_before_archive: false,
    });
  });

  it("uses the server default silently after asks are disabled", async () => {
    vi.mocked(fetchArchiveWorktreePreference).mockResolvedValue({
      ...preference,
      mode: "green_only",
      ask_before_archive: false,
    });
    render(<Harness />);
    fireEvent.click(screen.getByText("Start archive"));
    await waitFor(() => expect(proceed).toHaveBeenCalledWith(new Set()));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("retains every worktree if preferences cannot be read", async () => {
    vi.mocked(fetchArchiveWorktreePreference).mockRejectedValue(new Error("offline"));
    render(<Harness />);
    fireEvent.click(screen.getByText("Start archive"));
    await waitFor(() => expect(proceed).toHaveBeenCalledWith(new Set(["one"])));
  });

  it("keeps a failed save open instead of archiving", async () => {
    vi.mocked(saveArchiveWorktreePreference).mockRejectedValue(new Error("save failed"));
    render(<Harness />);
    fireEvent.click(screen.getByText("Start archive"));
    await screen.findByRole("dialog");
    fireEvent.click(screen.getByRole("checkbox", { name: "Don't ask again" }));
    fireEvent.click(screen.getByTestId("archive-worktree-confirm"));
    expect(await screen.findByRole("alert")).toHaveTextContent("save failed");
    expect(proceed).not.toHaveBeenCalled();
  });

  it("cancels an old scope once while a preference read finishes", async () => {
    let resolve!: (value: typeof preference) => void;
    vi.mocked(fetchArchiveWorktreePreference).mockReturnValue(
      new Promise((done) => {
        resolve = done;
      }),
    );
    const { rerender } = render(<Harness />);
    fireEvent.click(screen.getByText("Start archive"));
    rerender(<Harness scope="two" />);
    await act(async () => resolve(preference));
    expect(cancel).toHaveBeenCalledOnce();
    expect(proceed).not.toHaveBeenCalled();
  });
});
