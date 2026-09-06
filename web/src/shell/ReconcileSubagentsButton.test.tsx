import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";
import { authenticatedFetch } from "@/lib/identity";
import { ReconcileSubagentsButton, reconcileSubagents } from "./ReconcileSubagentsButton";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), info: vi.fn(), error: vi.fn() } }));

beforeEach(() => vi.clearAllMocks());
afterEach(cleanup);

function renderButton(client = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  return {
    client,
    ...render(
      <QueryClientProvider client={client}>
        <ReconcileSubagentsButton rootSessionId="parent" childIds={["child"]} />
      </QueryClientProvider>,
    ),
  };
}

describe("ReconcileSubagentsButton", () => {
  it("uses one icon action, prevents repeat clicks, and refreshes status after correction", async () => {
    let finish!: (response: Response) => void;
    vi.mocked(authenticatedFetch).mockReturnValue(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    const { client } = renderButton();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const button = screen.getByRole("button", { name: "Recheck agent status" });
    expect(button.textContent).toBe("");
    fireEvent.click(button);
    await waitFor(() => expect(button).toBeDisabled());
    fireEvent.click(button);
    expect(authenticatedFetch).toHaveBeenCalledTimes(1);
    expect(authenticatedFetch).toHaveBeenCalledWith(
      "/v1/sessions/parent/child_sessions/reconcile",
      { method: "POST" },
    );
    finish(Response.json({ corrected: 3, unchanged: 1, unverified: 0 }));
    await waitFor(() => expect(button).not.toBeDisabled());
    expect(toast.success).toHaveBeenCalledWith("3 corrected · 1 unchanged · 0 not verified");
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["session", "child"] });
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: ["conversation", "parent", "child_sessions"],
    });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["project-sessions"] });
  });

  it("explains unverified agents without claiming that they completed", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValue(
      Response.json({ corrected: 0, unchanged: 2, unverified: 4 }),
    );
    renderButton();
    fireEvent.click(screen.getByRole("button", { name: "Recheck agent status" }));
    await waitFor(() =>
      expect(toast.info).toHaveBeenCalledWith("0 corrected · 2 unchanged · 4 not verified", {
        description: "Unverified agents keep their current state.",
      }),
    );
    expect(toast.success).not.toHaveBeenCalled();
  });

  it("reports an unavailable Host without retrying or showing success", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValue(new Response(null, { status: 503 }));
    renderButton();
    fireEvent.click(screen.getByRole("button", { name: "Recheck agent status" }));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        "Could not verify agent status. Check that the Host is online and updated.",
      ),
    );
    expect(authenticatedFetch).toHaveBeenCalledTimes(1);
    expect(toast.success).not.toHaveBeenCalled();
  });

  it("rejects malformed success bodies instead of reporting fabricated counts", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValue(Response.json({ corrected: -1 }));
    await expect(reconcileSubagents("parent")).rejects.toThrow("invalid status check");
  });
});
