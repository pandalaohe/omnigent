import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Host } from "@/hooks/useHosts";
import type { HostTerminal } from "@/hooks/useHostTerminals";
import { authenticatedFetch } from "@/lib/identity";
import type * as Identity from "@/lib/identity";
import { HostTerminalsPage } from "./HostTerminalsPage";

const mocks = vi.hoisted(() => ({ hosts: [] as Host[], admin: true, mobile: false }));
vi.mock("@/hooks/useHosts", () => ({ useHosts: () => ({ data: mocks.hosts }) }));
vi.mock("@/hooks/useIsAdmin", () => ({ useIsAdmin: () => mocks.admin }));
vi.mock("@/hooks/useIsMobileViewport", () => ({ useIsMobileViewport: () => mocks.mobile }));
vi.mock("@/lib/identity", async (original) => ({
  ...(await original<typeof Identity>()),
  authenticatedFetch: vi.fn(),
}));
vi.mock("@/components/blocks/TerminalView", () => ({
  TerminalView: ({
    hostId,
    terminalId,
    active,
  }: {
    hostId: string;
    terminalId: string;
    active: boolean;
  }) => (
    <div
      data-testid="attached-terminal"
      data-host={hostId}
      data-terminal={terminalId}
      data-active={active}
    />
  ),
}));

const host: Host = {
  host_id: "host-a",
  name: "Host A",
  owner: "alice",
  status: "online",
  capabilities: ["host_terminals_v1", "terminal_elevated_v1"],
};
function terminal(id: string, elevated = false): HostTerminal {
  return {
    terminal_id: id,
    elevated,
    created_at: 1,
    attach_path: `/v1/hosts/host-a/terminals/${id}/attach`,
  };
}

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/hosts/host-a/terminals"]}>
        <Routes>
          <Route path="/hosts/:hostId/terminals" element={<HostTerminalsPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return {
    ...view,
    dispose: () => {
      view.unmount();
      client.clear();
    },
  };
}

async function newMenu() {
  const button = screen.getByRole("button", { name: "New terminal" });
  fireEvent.pointerDown(button, { button: 0 });
  fireEvent.click(button);
  return screen.findByRole("menu");
}

beforeEach(() => {
  mocks.hosts = [host];
  mocks.admin = true;
  mocks.mobile = false;
  vi.mocked(authenticatedFetch).mockReset();
});
afterEach(cleanup);

describe("host terminal page", () => {
  it.each([401, 403])(
    "does not attach terminals when the server denies access: %s",
    async (status) => {
      vi.mocked(authenticatedFetch).mockImplementation(async () =>
        Response.json({ error: { message: "Access denied", code: "forbidden" } }, { status }),
      );
      const view = mount();
      expect(await screen.findByRole("alert")).toHaveTextContent("Access denied");
      expect(screen.queryByTestId("attached-terminal")).toBeNull();
      expect(screen.queryByRole("tab")).toBeNull();
      view.dispose();
    },
  );
  it("reattaches live terminals on mount and reload without creating a shell", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async () =>
      Response.json({ terminals: [terminal("aaaaaa"), terminal("bbbbbb", true)] }),
    );
    let view = mount();
    expect(await screen.findByRole("tab", { name: "Shell · aaaaaa" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    fireEvent.mouseDown(screen.getByRole("tab", { name: "root · bbbbbb" }), {
      button: 0,
      ctrlKey: false,
    });
    expect(screen.getByRole("tab", { name: "root · bbbbbb" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getAllByTestId("attached-terminal").map((node) => node.dataset.host)).toEqual([
      "host-a",
      "host-a",
    ]);
    view.dispose();
    view = mount();
    await screen.findByRole("tab", { name: "root · bbbbbb" });
    expect(vi.mocked(authenticatedFetch).mock.calls.every(([, init]) => !init?.method)).toBe(true);
    view.dispose();
  });

  it("creates a root tab and closes only after confirmation", async () => {
    let live: HostTerminal[] = [];
    const requests: { method: string; body?: string | null }[] = [];
    vi.mocked(authenticatedFetch).mockImplementation(async (_, init) => {
      if (init?.method === "POST") {
        requests.push({ method: "POST", body: init.body as string });
        live = [terminal("cccccc", true)];
        return Response.json(live[0], { status: 201 });
      }
      if (init?.method === "DELETE") {
        requests.push({ method: "DELETE" });
        live = [];
        return new Response(null, { status: 204 });
      }
      return Response.json({ terminals: live });
    });
    const view = mount();
    await screen.findByText(/No open terminals/);
    await newMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "Admin (root)" }));
    expect(await screen.findByRole("tab", { name: "root · cccccc" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(requests).toEqual([{ method: "POST", body: '{"elevated":true}' }]);
    fireEvent.click(screen.getByRole("button", { name: "Close root · cccccc" }));
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Cancel" }));
    expect(requests).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Close root · cccccc" }));
    fireEvent.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: "Close terminal" }),
    );
    await waitFor(() => expect(screen.queryByRole("tab")).toBeNull());
    expect(requests).toEqual([{ method: "POST", body: '{"elevated":true}' }, { method: "DELETE" }]);
    view.dispose();
  });

  it.each([false, true])(
    "gates the admin option on capability and administrator: admin=%s",
    async (admin) => {
      mocks.admin = admin;
      mocks.hosts = [{ ...host, capabilities: admin ? ["host_terminals_v1"] : host.capabilities }];
      vi.mocked(authenticatedFetch).mockResolvedValue(Response.json({ terminals: [] }));
      const view = mount();
      await screen.findByText(/No open terminals/);
      await newMenu();
      expect(screen.queryByRole("menuitem", { name: "Admin (root)" })).toBeNull();
      expect(screen.getByRole("menuitem", { name: "Shell" })).toBeVisible();
      view.dispose();
    },
  );

  it("shows reconnecting instead of claiming the terminal list is empty, then recovers", async () => {
    vi.mocked(authenticatedFetch)
      .mockResolvedValueOnce(
        Response.json(
          { error: { code: "runner_unavailable", message: "terminal runner is reconnecting" } },
          { status: 503 },
        ),
      )
      .mockImplementation(async () => Response.json({ terminals: [terminal("dddddd")] }));
    const view = mount();
    expect(await screen.findByRole("alert")).toHaveTextContent("terminal runner is reconnecting");
    expect(screen.queryByText(/No open terminals/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await screen.findByRole("tab", { name: "Shell · dddddd" });
    view.dispose();
  });

  it("creates an ordinary shell with elevated false and reports mutation failure", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (_, init) =>
      init?.method === "POST"
        ? Response.json(
            { error: { message: "terminal runner is reconnecting", code: "runner_unavailable" } },
            { status: 503 },
          )
        : Response.json({ terminals: [] }),
    );
    const view = mount();
    fireEvent.click(await screen.findByRole("button", { name: "Open terminal" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("terminal runner is reconnecting");
    const posts = vi
      .mocked(authenticatedFetch)
      .mock.calls.filter(([, init]) => init?.method === "POST");
    expect(posts).toHaveLength(1);
    expect(posts[0][1]?.body).toBe('{"elevated":false}');
    view.dispose();
  });

  it.each(["missing", "offline", "unsupported"])(
    "makes no terminal request for an unavailable host: %s",
    async (state) => {
      mocks.hosts =
        state === "missing"
          ? []
          : [
              {
                ...host,
                status: state === "offline" ? "offline" : "online",
                capabilities: state === "unsupported" ? [] : host.capabilities,
              },
            ];
      const view = mount();
      expect(screen.getByRole("button", { name: "New terminal" })).toBeDisabled();
      expect(authenticatedFetch).not.toHaveBeenCalled();
      view.dispose();
    },
  );

  it("uses the full viewport on phones", async () => {
    mocks.mobile = true;
    vi.mocked(authenticatedFetch).mockResolvedValue(Response.json({ terminals: [] }));
    const view = mount();
    await screen.findByText(/No open terminals/);
    expect(screen.getByRole("main")).toHaveClass("fixed", "inset-0", "h-dvh");
    view.dispose();
  });
});
