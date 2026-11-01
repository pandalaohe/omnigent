import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { TerminalAgentAccess } from "./TerminalAgentAccess";
import { terminalsQueryKey, type TerminalInfo } from "@/hooks/useTerminals";
import { authenticatedFetch } from "@/lib/identity";
import type * as Identity from "@/lib/identity";

const state = vi.hoisted(() => ({ owner: true }));
vi.mock("@/hooks/useSessionTerminalAccess", () => ({
  useSessionTerminalAccess: () => ({
    canManageAgentAccess: state.owner,
    host: { host_id: "host-a", name: "Host A" },
    session: { title: "Maintenance", agentName: "Agent A" },
  }),
}));
vi.mock("@/lib/identity", async (original) => ({
  ...(await original<typeof Identity>()),
  authenticatedFetch: vi.fn(),
}));
const root: TerminalInfo = {
  id: "terminal_shell_key",
  name: "shell",
  session: "key",
  running: true,
  elevated: true,
  agentAccess: false,
  instanceId: "instance-a",
};
const resource = (instanceId: string, granted: boolean) => ({
  id: root.id,
  metadata: {
    terminal_name: "shell",
    session_key: "key",
    running: true,
    elevated: true,
    agent_access: granted,
    instance_id: instanceId,
  },
});
function mount(terminal = root) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  client.setQueryData(terminalsQueryKey("session-a"), [terminal]);
  const view = render(
    <QueryClientProvider client={client}>
      <TerminalAgentAccess conversationId="session-a" terminal={terminal} />
    </QueryClientProvider>,
  );
  return {
    ...view,
    client,
    dispose: () => {
      view.unmount();
      client.clear();
    },
  };
}
beforeEach(() => {
  state.owner = true;
  vi.mocked(authenticatedFetch).mockReset();
});
afterEach(cleanup);
it("starts off and sends the exact instance only after explicit confirmation", async () => {
  vi.mocked(authenticatedFetch).mockResolvedValue(Response.json(resource("instance-a", true)));
  const view = mount();
  expect(screen.getByRole("switch")).toHaveAttribute("aria-checked", "false");
  fireEvent.click(screen.getByRole("switch"));
  expect(authenticatedFetch).not.toHaveBeenCalled();
  expect(screen.getByRole("dialog")).toHaveTextContent(/Agent A.*Maintenance.*Host A/);
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(authenticatedFetch).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("switch"));
  fireEvent.click(screen.getByRole("button", { name: "Allow agent access" }));
  await waitFor(() =>
    expect(authenticatedFetch).toHaveBeenCalledWith(
      "/v1/sessions/session-a/resources/terminals/terminal_shell_key",
      expect.objectContaining({
        method: "PATCH",
        body: '{"agent_access":true,"instance_id":"instance-a"}',
      }),
    ),
  );
  await waitFor(() =>
    expect(
      view.client.getQueryData<TerminalInfo[]>(terminalsQueryKey("session-a"))?.[0].agentAccess,
    ).toBe(true),
  );
  view.dispose();
});
it("revokes access without granting or reopening a confirmation", async () => {
  vi.mocked(authenticatedFetch).mockResolvedValue(Response.json(resource("instance-a", false)));
  const view = mount({ ...root, agentAccess: true });
  fireEvent.click(screen.getByRole("switch"));
  await waitFor(() => expect(authenticatedFetch).toHaveBeenCalled());
  expect(vi.mocked(authenticatedFetch).mock.calls[0][1]?.body).toBe(
    '{"agent_access":false,"instance_id":"instance-a"}',
  );
  expect(screen.queryByRole("dialog")).toBeNull();
  view.dispose();
});
it("disables a non-owner's switch", () => {
  state.owner = false;
  const view = mount();
  expect(screen.getByRole("switch")).toBeDisabled();
  expect(authenticatedFetch).not.toHaveBeenCalled();
  view.dispose();
});
it("reports browser proof rejection and keeps access off", async () => {
  vi.mocked(authenticatedFetch).mockResolvedValue(
    Response.json(
      { error: { message: "Sign in to the owner's browser", code: "forbidden" } },
      { status: 403 },
    ),
  );
  const view = mount();
  fireEvent.click(screen.getByRole("switch"));
  fireEvent.click(screen.getByRole("button", { name: "Allow agent access" }));
  await waitFor(() => expect(screen.getAllByRole("alert")[0]).toHaveTextContent("Sign in"));
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(screen.getByRole("switch")).toHaveAttribute("aria-checked", "false");
  view.dispose();
});
it("does not grant a replacement instance from an old response", async () => {
  let finish: (response: Response) => void = () => {};
  vi.mocked(authenticatedFetch).mockImplementation(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  const view = mount();
  fireEvent.click(screen.getByRole("switch"));
  fireEvent.click(screen.getByRole("button", { name: "Allow agent access" }));
  await waitFor(() => expect(authenticatedFetch).toHaveBeenCalled());
  view.client.setQueryData(terminalsQueryKey("session-a"), [{ ...root, instanceId: "instance-b" }]);
  finish(Response.json(resource("instance-a", true)));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(
    view.client.getQueryData<TerminalInfo[]>(terminalsQueryKey("session-a"))?.[0],
  ).toMatchObject({ instanceId: "instance-b", agentAccess: false });
  view.dispose();
});
it("closes a stale confirmation when its displayed instance changes", () => {
  const view = mount();
  fireEvent.click(screen.getByRole("switch"));
  view.rerender(
    <QueryClientProvider client={view.client}>
      <TerminalAgentAccess
        conversationId="session-a"
        terminal={{ ...root, instanceId: "instance-b" }}
      />
    </QueryClientProvider>,
  );
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(authenticatedFetch).not.toHaveBeenCalled();
  view.dispose();
});
