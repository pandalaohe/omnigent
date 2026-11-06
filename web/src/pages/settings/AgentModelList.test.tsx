import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { authenticatedFetch } from "@/lib/identity";
import { copyText } from "@/lib/clipboard";
import type { Host } from "@/hooks/useHosts";
import { AgentModelList } from "./AgentModelList";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
vi.mock("@/lib/clipboard", () => ({ copyText: vi.fn().mockResolvedValue(undefined) }));

const host: Host = { host_id: "desktop", name: "Desktop", owner: "local", status: "online" };
let count: number;
let refuse: boolean;

function mount(target = host, harness = "acp:example") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const tree = (nextHost: Host, nextHarness: string) => (
    <QueryClientProvider client={client}>
      <AgentModelList host={nextHost} harness={nextHarness} />
    </QueryClientProvider>
  );
  return { ...render(tree(target, harness)), client, tree };
}

beforeEach(() => {
  count = 2;
  refuse = false;
  vi.mocked(authenticatedFetch).mockReset();
  vi.mocked(copyText).mockClear();
  vi.mocked(authenticatedFetch).mockImplementation(async () =>
    refuse
      ? Response.json({ detail: "synthetic backend failure" }, { status: 502 })
      : Response.json({
          models: Array.from({ length: count }, (_, i) => ({
            id: `model-${i}`,
            source: { kind: "model-source", label: "URL", host: "localhost:11434" },
          })),
        }),
  );
});
afterEach(cleanup);

describe("Model list", () => {
  it("shows its source and count, refreshes now and retains rows after failure", async () => {
    mount();
    await screen.findByText("2 models");
    expect(screen.getByText("URL · localhost:11434")).toBeTruthy();
    count = 3;
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await screen.findByText("3 models");
    expect(await screen.findByRole("status")).toHaveTextContent("Refreshed just now.");
    expect(vi.mocked(authenticatedFetch).mock.calls.at(-1)?.[0]).toContain("?refresh=force");
    refuse = true;
    await waitFor(() => expect(screen.getByRole("button", { name: "Refresh" })).not.toBeDisabled());
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Previous list kept.");
    expect(screen.getByText("3 models")).toBeTruthy();
  });

  it("explains an empty list and copies the same packaged setup rules", async () => {
    count = 0;
    mount();
    await screen.findByText("No list. Type a name in the model menu.");
    const connect = screen.getByRole("button", { name: "Connect your own" });
    connect.focus();
    fireEvent.click(connect);
    expect(await screen.findByRole("dialog")).toHaveTextContent("model_sources");
    fireEvent.click(screen.getByRole("button", { name: "Copy rules" }));
    await screen.findByText("Rules copied.");
    expect(vi.mocked(copyText).mock.calls[0][0]).toContain("omnigent models check");
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(connect).toHaveFocus();
    expect(
      vi
        .mocked(authenticatedFetch)
        .mock.calls.every(([, init]) => !init?.method || init.method === "GET"),
    ).toBe(true);
  });

  it("does not fetch an offline host and clears notices when the target changes", async () => {
    const view = mount();
    await screen.findByText("2 models");
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await screen.findByText("Refreshed just now.");
    view.rerender(view.tree({ ...host, host_id: "offline", status: "offline" }, "acp:other"));
    expect(screen.getByText("Host offline.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Refresh" })).toBeDisabled();
    expect(screen.queryByText("Refreshed just now.")).toBeNull();
    expect(
      vi.mocked(authenticatedFetch).mock.calls.every(([path]) => !String(path).includes("offline")),
    ).toBe(true);
  });
});
