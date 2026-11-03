import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { StandingApprovalChip } from "./StandingApprovalChip";

const { fetchMock } = vi.hoisted(() => ({ fetchMock: vi.fn() }));
vi.mock("@/lib/identity", () => ({ authenticatedFetch: fetchMock }));
const initial = {
  root_id: "parent",
  record: {
    kinds: ["install", "merge"],
    exceptions: ["server release"],
    since: 1792972800,
    events: [],
  },
};
const clients: QueryClient[] = [];
function mount(sessionId = "parent", readOnly = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  render(
    <QueryClientProvider client={client}>
      <StandingApprovalChip sessionId={sessionId} readOnly={readOnly} />
    </QueryClientProvider>,
  );
  return client;
}
beforeEach(() => fetchMock.mockResolvedValue({ ok: true, json: async () => initial }));
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
  vi.resetAllMocks();
});

describe("standing approval chip", () => {
  it("labels child approval delegation and keeps the wire kind when revoking", async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        ...initial,
        record: { ...initial.record, kinds: ["answer-child-approvals"] },
      }),
    });
    mount();
    fireEvent.click(await screen.findByRole("button", { name: "Standing approval: 1 active" }));
    expect(screen.getByText("Answer child approvals", { exact: true })).toBeVisible();
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ ...initial, record: { ...initial.record, kinds: [] } }),
    });
    fireEvent.click(screen.getByRole("button", { name: "Revoke Answer child approvals" }));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenLastCalledWith(
        expect.any(String),
        expect.objectContaining({
          body: JSON.stringify({ op: "revoke", kinds: ["answer-child-approvals"] }),
        }),
      ),
    );
  });
  it.each(["before", "during"])(
    "does not let a GET started %s revoke overwrite the successful change",
    async (when) => {
      const client = mount();
      fireEvent.click(await screen.findByRole("button", { name: "Standing approval: 2 active" }));
      let finishRead!: (value: unknown) => void;
      let finishWrite!: (value: unknown) => void;
      const staleRead = new Promise((done) => {
        finishRead = done;
      });
      const mutation = new Promise((done) => {
        finishWrite = done;
      });
      fetchMock.mockImplementation((_url: string, init?: RequestInit) =>
        init?.method === "POST" ? mutation : staleRead,
      );
      if (when === "before")
        void client.refetchQueries({ queryKey: ["standing-approval", "parent"] });
      fireEvent.click(screen.getByRole("button", { name: "Revoke install" }));
      await screen.findByText("Revoking…");
      if (when === "during")
        void client.refetchQueries({ queryKey: ["standing-approval", "parent"] });
      finishWrite({
        ok: true,
        json: async () => ({
          ...initial,
          record: {
            ...initial.record,
            kinds: ["merge"],
            events: [{ op: "revoke", kinds: ["install"], source: "web", at: 1 }],
          },
        }),
      });
      await screen.findByRole("button", { name: "Grant back install" });
      await act(async () => {
        finishRead({ ok: true, json: async () => initial });
        await staleRead;
      });
      expect(screen.getByRole("button", { name: "Grant back install" })).toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: "Standing approval: 1 active" }),
      ).toBeInTheDocument();
    },
  );
  it("revokes a kind and keeps grant pending until the server approval finishes", async () => {
    mount();
    fireEvent.click(await screen.findByRole("button", { name: "Standing approval: 2 active" }));
    expect(screen.getByText("server release")).toBeInTheDocument();
    const revoked = {
      ...initial,
      record: {
        ...initial.record,
        kinds: ["merge"],
        events: [{ op: "revoke", kinds: ["install"], source: "web", at: 1 }],
      },
    };
    fetchMock.mockResolvedValueOnce({ ok: true, json: async () => revoked });
    fireEvent.click(screen.getByRole("button", { name: "Revoke install" }));
    await screen.findByRole("button", { name: "Grant back install" });
    expect(fetchMock).toHaveBeenLastCalledWith(
      "/v1/sessions/parent/standing-approval",
      expect.objectContaining({ body: JSON.stringify({ op: "revoke", kinds: ["install"] }) }),
    );
    let resolve!: (value: unknown) => void;
    fetchMock.mockReturnValueOnce(
      new Promise((done) => {
        resolve = done;
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Grant back install" }));
    await screen.findByText("Waiting for approval in this conversation…");
    expect(screen.getByRole("button", { name: "Grant back install" })).toBeDisabled();
    resolve({ ok: true, json: async () => initial });
    await screen.findByRole("button", { name: "Revoke install" });
  });

  it("shows children and read-only viewers no mutation controls, and refreshes on invalidation", async () => {
    const client = mount("child");
    fireEvent.click(await screen.findByRole("button", { name: "Standing approval: 2 active" }));
    expect(
      screen.getByText("Inherited from the parent session. Read-only here."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Revoke install" })).toBeNull();
    fetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ ...initial, record: { ...initial.record, kinds: [] } }),
    });
    await client.invalidateQueries({ queryKey: ["standing-approval", "child"] });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Standing approval: none" })).toBeInTheDocument(),
    );
  });

  it("shows read-only viewers no mutation controls", async () => {
    mount("parent", true);
    fireEvent.click(await screen.findByRole("button", { name: "Standing approval: 2 active" }));
    expect(screen.queryByRole("button", { name: "Revoke install" })).toBeNull();
  });

  it("reports a failed load and retries", async () => {
    fetchMock.mockRejectedValueOnce(new Error("offline"));
    mount();
    fireEvent.click(await screen.findByRole("button", { name: "Standing approval: unavailable" }));
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await screen.findByRole("button", { name: "Revoke install" });
  });

  it("keeps the current approval when a mutation fails", async () => {
    mount();
    fireEvent.click(await screen.findByRole("button", { name: "Standing approval: 2 active" }));
    fetchMock.mockRejectedValueOnce(new Error("Standing approval grant was not approved"));
    fireEvent.click(screen.getByRole("button", { name: "Revoke install" }));
    await screen.findByRole("alert");
    expect(screen.getByRole("button", { name: "Revoke install" })).not.toBeDisabled();
    expect(screen.getByRole("button", { name: "Standing approval: 2 active" })).toBeInTheDocument();
  });
});
