import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { expectSettingsPages } from "@/test/settingsPaging";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { McpRegistryConnections, McpRegistryPicker } from "./McpRegistry";
import { authorizeRegistryService, registryRequest } from "@/lib/mcpRegistry";
import type * as Host from "@/lib/host";

vi.mock("@/lib/mcpRegistry", () => ({
  registryRequest: vi.fn(),
  authorizeRegistryService: vi.fn(),
}));

const scope = vi.hoisted(() => ({
  viewer: "viewer" as string | null,
  server: "one",
  generation: 1,
}));
vi.mock("@/hooks/useViewerId", () => ({ useViewerId: () => scope.viewer }));
vi.mock("@/lib/host", async (importOriginal) => ({
  ...(await importOriginal<typeof Host>()),
  getOmnigentServerIdentity: () => scope.server,
  getOmnigentHostGeneration: () => scope.generation,
}));

function renderConnections(
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } }),
) {
  return render(<McpRegistryConnections />, {
    wrapper: ({ children }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    ),
  });
}

const service = {
  id: "tracker",
  title: "Work tracker",
  description: "Read work tickets",
  auth: "bearer",
  connected: false,
  tools: ["read_ticket"],
  connect_provider: "bearer",
};

beforeEach(() => {
  vi.resetAllMocks();
  Object.assign(scope, { viewer: "viewer", server: "one", generation: 1 });
});

describe("MCP account connections", () => {
  it("shows operator-configured services without personal connection actions", async () => {
    vi.mocked(registryRequest).mockResolvedValue({
      data: [
        { ...service, auth: "env", status: "unavailable", connect_provider: null },
        {
          ...service,
          id: "wiki",
          title: "Wiki",
          auth: "env",
          status: "configured",
          connected: true,
          connect_provider: null,
        },
      ],
    });
    renderConnections();
    expect(await screen.findByText("Unavailable")).toBeVisible();
    expect(screen.getByText("Configured by server")).toBeVisible();
    expect(
      screen.queryByRole("button", { name: /^(connect|reconnect|disconnect|update token)$/i }),
    ).toBeNull();
    expect(screen.queryByRole("link", { name: /^(connect|reconnect)$/i })).toBeNull();
  });

  it("waits for viewer identity and drops the cached page on a server switch", async () => {
    scope.viewer = null;
    vi.mocked(registryRequest).mockImplementation(async (path) => ({
      data: [
        {
          ...service,
          title: path?.includes("after") ? "Server one later" : `Server ${scope.server}`,
        },
      ],
      next_cursor: scope.server === "one" && !path?.includes("after") ? "server-one-cursor" : null,
    }));
    const { rerender } = renderConnections(
      new QueryClient({ defaultOptions: { queries: { staleTime: Infinity } } }),
    );
    fireEvent.focus(window);
    expect(registryRequest).not.toHaveBeenCalled();
    scope.viewer = "viewer";
    rerender(<McpRegistryConnections />);
    await screen.findByText("Server one");
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await screen.findByText("Server one later");

    scope.server = "two";
    scope.generation += 1;
    rerender(<McpRegistryConnections />);
    expect(screen.queryByText("Server one later")).not.toBeInTheDocument();
    expect(screen.queryByText("Server one")).not.toBeInTheDocument();
    await screen.findByText("Server two");
    expect(vi.mocked(registryRequest).mock.calls.map(([url]) => url)).toEqual([
      "?limit=20",
      "?limit=20&after=server-one-cursor",
      "?limit=20",
    ]);
  });

  it("requests one bounded page at a time and can go back", async () => {
    const services = Array.from({ length: 45 }, (_, index) => ({
      ...service,
      id: `service-${index + 1}`,
      title: `Service ${index + 1}`,
      auth: "none",
    }));
    const requests: string[] = [];
    vi.mocked(registryRequest).mockImplementation(async (path) => {
      requests.push(path!);
      const after = new URLSearchParams(path).get("after");
      const start = after ? services.findIndex((row) => row.id === after) + 1 : 0;
      const end = start + 20;
      return {
        data: services.slice(start, end),
        next_cursor: services[end] ? services[end - 1].id : null,
      };
    });
    renderConnections();
    await screen.findByText("Service 1");
    expect(requests).toEqual(["?limit=20"]);
    await expectSettingsPages("Managed MCP service pages", (number) => `Service ${number}`);
    expect(requests).toEqual([
      "?limit=20",
      "?limit=20&after=service-20",
      "?limit=20&after=service-40",
    ]);
    fireEvent.focus(window);
    await waitFor(() => expect(requests.slice(3)).toEqual(["?limit=20&after=service-40"]));
    const pager = within(screen.getByRole("navigation", { name: "Managed MCP service pages" }));
    await waitFor(() => expect(pager.getByRole("button", { name: "Previous" })).toBeEnabled());
    fireEvent.click(pager.getByRole("button", { name: "Previous" }));
    await screen.findByText("Service 21");
    expect(screen.queryByText("Service 41")).not.toBeInTheDocument();
  });

  it("refreshes the connected MCP service on its later page after OAuth and can go back", async () => {
    const originalUrl = window.location.href;
    window.history.replaceState(null, "", "/settings/mcp");
    let connected = false;
    vi.mocked(registryRequest).mockImplementation(async (path) => {
      const after = new URLSearchParams(path).get("after");
      return after
        ? { data: [{ ...service, auth: "oauth", connect_provider: "mcp-tracker", connected }] }
        : {
            data: [{ ...service, id: "first-page", title: "First page service", auth: "none" }],
            next_cursor: "first-page",
          };
    });
    try {
      const before = renderConnections();
      fireEvent.click(await screen.findByRole("button", { name: "Next" }));
      expect(await screen.findByText("Not connected")).toBeInTheDocument();
      const connect = await screen.findByRole("link", { name: "Connect" });
      const connectUrl = new URL(connect.getAttribute("href")!, originalUrl);
      expect(connectUrl.pathname).toBe("/v1/connections/mcp-tracker/connect");
      const returnedUrl = new URL(connectUrl.searchParams.get("return_to")!, originalUrl);
      returnedUrl.searchParams.set("mcp-tracker", "connected");
      before.unmount();
      connected = true;
      vi.mocked(registryRequest).mockClear();
      window.history.replaceState(null, "", returnedUrl);

      renderConnections();
      expect(await screen.findByRole("link", { name: "Reconnect" })).toBeInTheDocument();
      expect(screen.getByText("Connected")).toBeInTheDocument();
      expect(screen.getByText("Page 2")).toBeInTheDocument();
      expect(vi.mocked(registryRequest).mock.calls.map(([url]) => url)).toEqual([
        "?limit=20&after=first-page",
      ]);
      fireEvent.click(screen.getByRole("button", { name: "Previous" }));
      expect(await screen.findByText("First page service")).toBeInTheDocument();
      expect(vi.mocked(registryRequest).mock.calls.at(-1)?.[0]).toBe("?limit=20");
    } finally {
      window.history.replaceState(null, "", originalUrl);
    }
  });

  it.each(["server", "viewer"] as const)(
    "consumes the OAuth page cursor before switching %s at the same URL",
    async (switchScope) => {
      const originalUrl = window.location.href;
      const originalState = window.history.state;
      const routeState = { idx: 2 };
      window.history.replaceState(
        routeState,
        "",
        "/settings/mcp?mcp_page_after=first-page&mcp-tracker=connected&tab=kept#services",
      );
      scope.viewer = null;
      vi.mocked(registryRequest).mockImplementation(async (path) => ({
        data: [
          {
            ...service,
            title: path?.includes("after") ? "Later page service" : "First page service",
          },
        ],
      }));

      try {
        const { rerender } = renderConnections();
        expect(registryRequest).not.toHaveBeenCalled();
        scope.viewer = "viewer";
        rerender(<McpRegistryConnections />);
        await screen.findByText("Later page service");
        expect(screen.getByText("Page 2")).toBeInTheDocument();

        if (switchScope === "server") {
          scope.server = "two";
          scope.generation += 1;
        } else {
          scope.viewer = "another-viewer";
        }
        rerender(<McpRegistryConnections />);
        await screen.findByText("First page service");
        expect(vi.mocked(registryRequest).mock.calls.map(([url]) => url)).toEqual([
          "?limit=20&after=first-page",
          "?limit=20",
        ]);
        expect(window.location.search).toBe("?tab=kept");
        expect(window.location.hash).toBe("#services");
        expect(window.history.state).toEqual(routeState);
      } finally {
        window.history.replaceState(originalState, "", originalUrl);
      }
    },
  );

  it.each(["github", "databricks"])(
    "links shared %s accounts to their provider controls",
    async (auth) => {
      vi.mocked(registryRequest).mockResolvedValue({ data: [{ ...service, auth }] });
      renderConnections();
      expect(
        await screen.findByRole("link", { name: "Manage account in Sandbox Integrations" }),
      ).toHaveAttribute("href", "/settings/integrations");
    },
  );

  it("saves a personal token, clears it, and tests the connected service", async () => {
    vi.mocked(registryRequest)
      .mockResolvedValueOnce({ data: [service] })
      .mockResolvedValueOnce({ connected: true })
      .mockResolvedValueOnce({ data: [{ ...service, connected: true }] })
      .mockResolvedValueOnce({ tools: ["read_ticket"] });
    renderConnections();
    fireEvent.click(await screen.findByRole("button", { name: "Connect" }));
    const input = screen.getByLabelText("Token for Work tracker");
    expect(input.getAttribute("type")).toBe("password");
    fireEvent.change(input, { target: { value: "test-personal-token" } });
    fireEvent.click(screen.getByRole("button", { name: "Save token" }));
    await screen.findByText("Connected");
    expect(screen.queryByLabelText("Token for Work tracker")).toBeNull();
    expect(registryRequest).toHaveBeenCalledWith(
      "/tracker/connection",
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify({ token: "test-personal-token" }),
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Test connection" }));
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Tool discovery succeeded. Tools: read_ticket. Individual calls may require additional permissions.",
    );
  });

  it("shows a connection failure and keeps the account controls usable", async () => {
    vi.mocked(registryRequest)
      .mockResolvedValueOnce({ data: [{ ...service, connected: true }] })
      .mockRejectedValueOnce(new Error("Reconnect your account"));
    renderConnections();
    fireEvent.click(await screen.findByRole("button", { name: "Test connection" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Reconnect your account");
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Disconnect" })).not.toBeDisabled(),
    );
  });
});

describe("MCP service selection", () => {
  it("shows selected services and toggles membership without disconnecting the account", async () => {
    vi.mocked(registryRequest).mockResolvedValue({
      data: [
        service,
        { ...service, id: "connected", title: "Connected tracker", connected: true },
        { ...service, id: "attached", title: "Attached tracker", connected: true },
      ],
    });
    const toggle = vi.fn();
    render(<McpRegistryPicker attached={["attached"]} onToggle={toggle} busy={false} />);
    const selected = await screen.findByRole("switch", { name: "Attached tracker" });
    expect(selected).toBeChecked();
    expect(screen.getByRole("switch", { name: "Work tracker" })).toBeDisabled();
    fireEvent.click(screen.getByRole("switch", { name: "Connected tracker" }));
    fireEvent.click(selected);
    expect(toggle.mock.calls).toEqual([
      ["connected", true],
      ["attached", false],
    ]);
    expect(registryRequest).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("link", { name: "Manage MCP accounts" })).toHaveAttribute(
      "href",
      "/settings/mcp",
    );
  });

  it("reports a sign-in starting and finishing to its owner", async () => {
    const oauth = { ...service, auth: "oauth" };
    vi.mocked(registryRequest).mockResolvedValueOnce({ data: [oauth] });
    let complete!: () => void;
    vi.mocked(authorizeRegistryService).mockReturnValue(
      new Promise<void>((resolve) => {
        complete = resolve;
      }),
    );
    const onAuthorizingChange = vi.fn();
    render(
      <McpRegistryPicker
        attached={[]}
        onToggle={vi.fn()}
        busy={false}
        onAuthorizingChange={onAuthorizingChange}
      />,
    );
    fireEvent.click(await screen.findByRole("switch", { name: "Work tracker" }));
    await waitFor(() => expect(onAuthorizingChange).toHaveBeenLastCalledWith(true));

    vi.mocked(registryRequest).mockResolvedValue({ data: [{ ...oauth, connected: true }] });
    complete();
    await waitFor(() => expect(onAuthorizingChange).toHaveBeenLastCalledWith(false));
  });

  it("reports the sign-in as over when its owner unmounts mid-popup", async () => {
    const oauth = { ...service, auth: "oauth" };
    vi.mocked(registryRequest).mockResolvedValue({ data: [oauth] });
    vi.mocked(authorizeRegistryService).mockReturnValue(new Promise<void>(() => {}));
    const onAuthorizingChange = vi.fn();
    const { unmount } = render(
      <McpRegistryPicker
        attached={[]}
        onToggle={vi.fn()}
        busy={false}
        onAuthorizingChange={onAuthorizingChange}
      />,
    );
    fireEvent.click(await screen.findByRole("switch", { name: "Work tracker" }));
    await waitFor(() => expect(onAuthorizingChange).toHaveBeenLastCalledWith(true));

    unmount();
    expect(onAuthorizingChange).toHaveBeenLastCalledWith(false);
  });

  it("finishes sign-in before enabling a service", async () => {
    const oauth = { ...service, auth: "oauth" };
    vi.mocked(registryRequest).mockResolvedValueOnce({ data: [oauth] });
    let complete!: () => void;
    vi.mocked(authorizeRegistryService).mockReturnValue(
      new Promise<void>((resolve) => {
        complete = resolve;
      }),
    );
    const toggle = vi.fn();
    render(<McpRegistryPicker attached={[]} onToggle={toggle} busy={false} />);
    fireEvent.click(await screen.findByRole("switch", { name: "Work tracker" }));
    expect(toggle).not.toHaveBeenCalled();
    expect(screen.getByRole("switch")).not.toBeChecked();
    expect(screen.getByRole("switch")).toBeDisabled();
    vi.mocked(registryRequest).mockResolvedValue({ data: [{ ...oauth, connected: true }] });
    complete();
    await waitFor(() => expect(toggle).toHaveBeenCalledWith("tracker", true));
  });

  it("keeps a service unchecked after OAuth cancellation and allows retry", async () => {
    vi.mocked(registryRequest).mockResolvedValue({ data: [{ ...service, auth: "oauth" }] });
    vi.mocked(authorizeRegistryService).mockRejectedValue(new Error("Sign-in cancelled"));
    const toggle = vi.fn();
    render(<McpRegistryPicker attached={[]} onToggle={toggle} busy={false} />);
    fireEvent.click(await screen.findByRole("switch", { name: "Work tracker" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Sign-in cancelled");
    expect(screen.getByRole("switch")).not.toBeChecked();
    expect(screen.getByRole("switch")).not.toBeDisabled();
    expect(toggle).not.toHaveBeenCalled();
  });

  it("reconnects an attached service without creating a duplicate attachment", async () => {
    const oauth = { ...service, auth: "oauth" };
    vi.mocked(registryRequest)
      .mockResolvedValueOnce({ data: [oauth] })
      .mockResolvedValue({ data: [{ ...oauth, connected: true }] });
    vi.mocked(authorizeRegistryService).mockResolvedValue();
    const toggle = vi.fn();
    render(<McpRegistryPicker attached={["tracker"]} onToggle={toggle} busy={false} />);
    fireEvent.click(await screen.findByRole("button", { name: "Reconnect" }));
    await screen.findByText("Connected");
    expect(toggle).not.toHaveBeenCalled();
  });

  it("allows removing disconnected or unavailable services and protects custom server names", async () => {
    vi.mocked(registryRequest).mockResolvedValue({
      data: [service, { ...service, id: "custom", title: "Custom conflict", connected: true }],
    });
    const toggle = vi.fn();
    render(
      <McpRegistryPicker
        attached={["tracker", "removed"]}
        reservedNames={["custom"]}
        onToggle={toggle}
        busy={false}
      />,
    );
    fireEvent.click(await screen.findByRole("switch", { name: "Work tracker" }));
    fireEvent.click(screen.getByRole("switch", { name: "removed" }));
    expect(toggle.mock.calls).toEqual([
      ["tracker", false],
      ["removed", false],
    ]);
    expect(screen.getByRole("switch", { name: "Custom conflict" })).toBeDisabled();
  });
});

it("asks for a Databricks workspace before connecting and selecting it", async () => {
  const databricks = { ...service, auth: "databricks", connect_provider: "databricks" };
  vi.mocked(registryRequest).mockResolvedValueOnce({ data: [databricks] });
  const toggle = vi.fn();
  render(<McpRegistryPicker attached={[]} onToggle={toggle} busy={false} />);
  fireEvent.click(await screen.findByRole("switch", { name: "Work tracker" }));
  expect(authorizeRegistryService).not.toHaveBeenCalled();
  expect(toggle).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "Connect workspace" })).toBeDisabled();
  fireEvent.change(screen.getByLabelText("Databricks workspace URL"), {
    target: { value: "https://workspace.example.test" },
  });
  vi.mocked(authorizeRegistryService).mockResolvedValue();
  vi.mocked(registryRequest).mockResolvedValue({ data: [{ ...databricks, connected: true }] });
  fireEvent.click(screen.getByRole("button", { name: "Connect workspace" }));
  await waitFor(() => expect(toggle).toHaveBeenCalledWith("tracker", true));
  expect(authorizeRegistryService).toHaveBeenCalledWith(
    databricks,
    expect.any(AbortSignal),
    "https://workspace.example.test",
  );
});

it("follows catalog cursors before showing the complete selection", async () => {
  vi.mocked(registryRequest)
    .mockResolvedValueOnce({ data: [service], next_cursor: "tracker" })
    .mockResolvedValueOnce({
      data: [{ ...service, id: "wiki", title: "Wiki", connected: true }],
      next_cursor: null,
    });
  const toggle = vi.fn();
  render(<McpRegistryPicker attached={[]} onToggle={toggle} busy={false} />);
  fireEvent.click(await screen.findByRole("switch", { name: "Wiki" }));
  expect(toggle).toHaveBeenCalledWith("wiki", true);
  expect(registryRequest).toHaveBeenNthCalledWith(2, "?after=tracker");
  expect(screen.getByRole("switch", { name: "Work tracker" })).toBeInTheDocument();
});
