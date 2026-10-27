import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, getStartupReleasePreview, releaseStartupSlots } from "@/lib/sessionsApi";
import type * as SessionsApi from "@/lib/sessionsApi";
import { StartupReleaseDialog } from "./StartupReleaseDialog";

vi.mock("@/lib/sessionsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof SessionsApi>()),
  getStartupReleasePreview: vi.fn(),
  releaseStartupSlots: vi.fn(),
}));
afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});
const target = {
  startup_id: "a".repeat(32),
  session_id: "b".repeat(32),
  attempt_id: "c".repeat(32),
  input_id: null,
  runtime_id: "runtime",
  host_generation: "old",
  runner_id: "runner",
};
const preview = {
  current_host_generation: "current",
  targets: [{ target, title: "Research", work_hint: "unknown" as const }],
};

describe("StartupReleaseDialog", () => {
  it.each(["working", "unknown"] as const)(
    "confirms exact targets despite %s hints and prevents duplicate release",
    async (work_hint) => {
      vi.mocked(getStartupReleasePreview).mockResolvedValue({
        ...preview,
        targets: [{ ...preview.targets[0], work_hint }],
      });
      let finish!: () => void;
      vi.mocked(releaseStartupSlots).mockImplementation(
        () =>
          new Promise<void>((resolve) => {
            finish = resolve;
          }),
      );
      const onReleased = vi.fn().mockResolvedValue(undefined);
      render(<StartupReleaseDialog hostId="host" onReleased={onReleased} />);
      fireEvent.click(screen.getByRole("button", { name: "Review stuck starts" }));
      const button = await screen.findByRole("button", { name: "Release startup slots" });
      await waitFor(() => expect(button).toBeEnabled());
      const dialog = screen.getByRole("dialog");
      expect(dialog).toHaveTextContent("does not stop old work");
      expect(dialog).toHaveTextContent("more concurrent starts than the limit");
      expect(dialog).toHaveTextContent("does not resend saved input");
      expect(dialog).toHaveTextContent("Research");
      expect(screen.getByText(target.session_id.slice(0, 8), { exact: true })).toHaveAttribute(
        "title",
        target.session_id,
      );
      expect(dialog).not.toHaveTextContent(target.session_id);
      fireEvent.click(button);
      expect(screen.getByRole("button", { name: "Releasing…" })).toBeDisabled();
      fireEvent.click(button);
      expect(releaseStartupSlots).toHaveBeenCalledExactlyOnceWith("host", {
        current_host_generation: "current",
        targets: [target],
      });
      finish();
      await waitFor(() => expect(onReleased).toHaveBeenCalledOnce());
      expect(await screen.findByRole("status")).toHaveTextContent("Startup slots released");
    },
  );
  it("requires a new review after a conflict and never substitutes fresh targets automatically", async () => {
    vi.mocked(getStartupReleasePreview).mockResolvedValue(preview);
    vi.mocked(releaseStartupSlots).mockRejectedValue(new ApiError("changed", 409, null));
    render(<StartupReleaseDialog hostId="host" onReleased={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Review stuck starts" }));
    const button = await screen.findByRole("button", { name: "Release startup slots" });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    expect(await screen.findByRole("alert")).toHaveTextContent("review it again");
    expect(button).toBeDisabled();
    expect(getStartupReleasePreview).toHaveBeenCalledOnce();
    expect(releaseStartupSlots).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole("button", { name: "Refresh list" }));
    await waitFor(() => expect(button).toBeEnabled());
    expect(getStartupReleasePreview).toHaveBeenCalledTimes(2);
  });
  it("hides release authority when the host is owned by somebody else", async () => {
    vi.mocked(getStartupReleasePreview).mockRejectedValue(new ApiError("forbidden", 403, null));
    render(<StartupReleaseDialog hostId="host" onReleased={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Review stuck starts" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Only the host owner");
    expect(screen.getByRole("button", { name: "Release startup slots" })).toBeDisabled();
    expect(releaseStartupSlots).not.toHaveBeenCalled();
  });
});
