import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";
import { TooltipProvider } from "@/components/ui/tooltip";
import { SidebarServerPicker } from "./SidebarServerPicker";

// The picker reaches the Electron shell only through nativeBridge, so the
// bridge is the seam: mocking it covers "inside the shell" (info resolves) and
// "plain browser" (resolves null) without having to fake a preload object.
const getServerPicker = vi.fn();
const switchServer = vi.fn();
const openServerSetup = vi.fn();
const signOutOfServer = vi.fn();

vi.mock("@/lib/nativeBridge", () => ({
  getServerPicker: () => getServerPicker(),
  switchServer: (url: string) => switchServer(url),
  openServerSetup: () => openServerSetup(),
  signOutOfServer: () => signOutOfServer(),
}));

function renderPicker() {
  return render(
    <TooltipProvider>
      <SidebarServerPicker />
    </TooltipProvider>,
  );
}

/**
 * Open the menu. Radix opens its dropdown on pointerdown (not click), which is
 * how the rest of the suite drives these triggers.
 */
async function openMenu() {
  const trigger = await screen.findByTestId("sidebar-server-picker");
  fireEvent.pointerDown(trigger, { button: 0, ctrlKey: false });
  return trigger;
}

beforeEach(() => {
  getServerPicker.mockReset();
  switchServer.mockReset();
  openServerSetup.mockReset();
  signOutOfServer.mockReset();
});

afterEach(cleanup);

describe("SidebarServerPicker", () => {
  it("renders nothing in a plain browser (bridge resolves null)", async () => {
    getServerPicker.mockResolvedValue(null);
    const { container } = renderPicker();

    // Wait out the resolve so this can't pass merely by asserting too early.
    await waitFor(() => expect(getServerPicker).toHaveBeenCalled());
    expect(screen.queryByTestId("sidebar-server-picker")).toBeNull();
    expect(container).toBeEmptyDOMElement();
  });

  it("shows a generic Server label on the collapsed row and lists the host and recents in the menu", async () => {
    getServerPicker.mockResolvedValue({
      currentOrigin: "http://server.example:8000",
      recentServers: [
        "http://server.example:8000/",
        "https://omnigents-3272836215725701.aws.databricksapps.com/",
        "https://omnigents-9147263058412098.aws.databricksapps.com/",
      ],
    });
    renderPicker();

    const trigger = await screen.findByTestId("sidebar-server-picker");
    expect(trigger).toHaveTextContent("Server");
    expect(trigger).toHaveAttribute("aria-label", "Server. Switch server");
    expect(trigger).not.toHaveAttribute("title");
    expect(screen.queryByText("server.example:8000")).toBeNull();

    await openMenu();

    expect(await screen.findByText("Recents")).toBeInTheDocument();
    // server.example:8000 shows once — as the menu's leading checked entry —
    // and NOT on the collapsed row or a second time from recents.
    expect(screen.getAllByText("server.example:8000")).toHaveLength(1);
    expect(
      screen.getByText("omnigents-3272836215725701.aws.databricksapps.com"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("omnigents-9147263058412098.aws.databricksapps.com"),
    ).toBeInTheDocument();
  });

  it("shows managed servers before recents and switches to one", async () => {
    getServerPicker.mockResolvedValue({
      currentOrigin: "https://personal.example.com",
      managedServers: ["https://managed.example.com/ml/omnigents"],
      recentServers: ["https://managed.example.com/old-mount", "https://recent.example.com/"],
    });
    renderPicker();

    await openMenu();
    expect(await screen.findByText("Provided by your organization")).toBeInTheDocument();
    expect(screen.getAllByText("managed.example.com")).toHaveLength(1);
    expect(screen.getByText("Recents")).toBeInTheDocument();
    expect(screen.getByText("recent.example.com")).toBeInTheDocument();

    fireEvent.click(screen.getByText("managed.example.com"));
    await waitFor(() =>
      expect(switchServer).toHaveBeenCalledWith("https://managed.example.com/ml/omnigents"),
    );
  });

  it("shows a managed current server only in the organization section", async () => {
    getServerPicker.mockResolvedValue({
      currentOrigin: "https://managed.example.com",
      managedServers: ["https://managed.example.com/ml/omnigents"],
      recentServers: [],
    });
    renderPicker();

    const trigger = await screen.findByTestId("sidebar-server-picker");
    expect(trigger).toHaveTextContent("Server");
    expect(screen.queryByText("managed.example.com")).toBeNull();

    await openMenu();
    expect(await screen.findByText("Provided by your organization")).toBeInTheDocument();
    // Shown once, as the checked managed menu item (not on the collapsed row).
    expect(screen.getAllByText("managed.example.com")).toHaveLength(1);
    expect(screen.queryByText("Recents")).toBeNull();
  });

  it("switches to a recent server on select", async () => {
    getServerPicker.mockResolvedValue({
      currentOrigin: "http://localhost:8000",
      recentServers: ["https://other.example.com/"],
    });
    renderPicker();

    await openMenu();
    fireEvent.click(await screen.findByText("other.example.com"));

    await waitFor(() => expect(switchServer).toHaveBeenCalledWith("https://other.example.com/"));
  });

  it("opens the shell's setup page from 'Connect to new server…'", async () => {
    getServerPicker.mockResolvedValue({
      currentOrigin: "http://localhost:8000",
      recentServers: [],
    });
    renderPicker();

    await openMenu();
    fireEvent.click(await screen.findByText("Connect to new server…"));

    await waitFor(() => expect(openServerSetup).toHaveBeenCalled());
  });

  it("still offers 'Connect to new server…' with no recents", async () => {
    getServerPicker.mockResolvedValue({
      currentOrigin: "http://localhost:8000",
      recentServers: [],
    });
    renderPicker();

    await openMenu();

    // Only the current server is listed — but the menu is never a dead end.
    expect(await screen.findByText("Connect to new server…")).toBeInTheDocument();
  });

  it("falls back to the raw string in the menu when an origin won't parse as a URL", async () => {
    getServerPicker.mockResolvedValue({
      currentOrigin: "not-a-url",
      recentServers: ["also-not-a-url"],
    });
    renderPicker();

    const trigger = await openMenu();
    expect(trigger).toHaveAttribute("aria-label", "Server. Switch server");

    // Unparseable origins and recents survive as-is in the menu rather than
    // being dropped, so a hand-edited settings file stays switchable.
    expect(await screen.findByText("not-a-url")).toBeInTheDocument();
    expect(screen.getByText("also-not-a-url")).toBeInTheDocument();
  });

  it("tells apart two workspaces behind one account host", async () => {
    // Sign-in moved this window to workspace 1's own host; the shell names the pick.
    getServerPicker.mockResolvedValue({
      currentOrigin: "https://dbc-1.cloud.databricks.com",
      currentServer: "https://accounts.example.com/omnigent?o=1",
      recentServers: ["https://accounts.example.com/?o=1", "https://accounts.example.com/?o=2"],
    });
    renderPicker();
    const trigger = await screen.findByTestId("sidebar-server-picker");
    expect(trigger).toHaveTextContent("Server");
    expect(screen.queryByText("accounts.example.com")).toBeNull();
    await openMenu();
    // The menu names the picked host, not the workspace host sign-in moved to.
    expect(await screen.findAllByText("accounts.example.com")).toHaveLength(2);
    // Workspace 2 stays a switch target even though it shares the account origin.
    const items = screen.getAllByRole("menuitem");
    const other = items.find(
      (item) =>
        !item.hasAttribute("data-disabled") &&
        /accounts\.example\.com/.test(item.textContent ?? ""),
    );
    expect(other).toBeDefined();
    fireEvent.click(other!);
    await waitFor(() =>
      expect(switchServer).toHaveBeenCalledWith("https://accounts.example.com/?o=2"),
    );
  });

  it("names a recent by the server the user picked for it", async () => {
    getServerPicker.mockResolvedValue({
      currentOrigin: "http://localhost:8000",
      recentServers: ["http://localhost:8000/", "https://dbc-1.cloud.databricks.com/omnigent"],
      recentLabels: {
        "https://dbc-1.cloud.databricks.com/omnigent": "https://accounts.example.com/omnigent?o=1",
      },
    });
    renderPicker();
    await openMenu();
    expect(screen.queryByText("dbc-1.cloud.databricks.com")).toBeNull();
    fireEvent.click(await screen.findByText("accounts.example.com"));
    // The switch still goes to the workspace host, where the sign-in is.
    await waitFor(() =>
      expect(switchServer).toHaveBeenCalledWith("https://dbc-1.cloud.databricks.com/omnigent"),
    );
  });

  it("folds a recent into the managed server it was reached through", async () => {
    getServerPicker.mockResolvedValue({
      currentOrigin: "http://localhost:8000",
      managedServers: ["https://accounts.example.com/omnigent?o=1"],
      recentServers: ["http://localhost:8000/", "https://dbc-1.cloud.databricks.com/omnigent"],
      recentLabels: {
        "https://dbc-1.cloud.databricks.com/omnigent": "https://accounts.example.com/omnigent?o=1",
      },
    });
    renderPicker();
    await openMenu();
    // Listed once, as managed; selecting it switches through the recent's host.
    expect(screen.getAllByText("accounts.example.com")).toHaveLength(1);
    fireEvent.click(screen.getByText("accounts.example.com"));
    await waitFor(() =>
      expect(switchServer).toHaveBeenCalledWith("https://dbc-1.cloud.databricks.com/omnigent"),
    );
  });

  it("names managed servers from the organization's server names and shows their hosts in the menu", async () => {
    getServerPicker.mockResolvedValue({
      currentOrigin: "https://dbc-1.cloud.databricks.com",
      managedServers: [
        "https://dbc-1.cloud.databricks.com/?o=1",
        "https://staging.example.com:8443/",
        "https://two.example.com/",
      ],
      managedServerNames: {
        "https://dbc-1.cloud.databricks.com/?o=1": "Engineering",
        "https://staging.example.com:8443/": "Staging Hub",
      },
      recentServers: [],
    });
    renderPicker();
    // The row names the current, managed server by its name without leaking its host.
    expect(await screen.findByText("Engineering")).toBeInTheDocument();
    expect(screen.queryByText("dbc-1.cloud.databricks.com")).toBeNull();
    await openMenu();
    expect(screen.getAllByText("Engineering").length).toBeGreaterThan(1);
    // Both the current managed server and other named managed entries show their
    // address as a secondary muted line under the name in the menu.
    expect(screen.getByText("dbc-1.cloud.databricks.com")).toHaveClass("text-muted-foreground");
    expect(screen.getByText("Staging Hub")).toBeInTheDocument();
    expect(screen.getByText("staging.example.com:8443")).toHaveClass("text-muted-foreground");
    // An unnamed one keeps its host.
    expect(screen.getByText("two.example.com")).toBeInTheDocument();
  });

  it("shows names servers gave themselves on the row and keeps hosts inside the menu", async () => {
    getServerPicker.mockResolvedValue({
      currentOrigin: "https://omni.example",
      recentServers: [
        "https://omni.example/",
        "https://staging.example/",
        "https://plain.example/",
      ],
      serverNames: {
        "https://omni.example": "Acme Engineering",
        "https://staging.example": "Acme Staging",
      },
    });
    renderPicker();
    const trigger = await screen.findByTestId("sidebar-server-picker");
    expect(trigger).toHaveTextContent("Acme Engineering");
    expect(trigger).toHaveAttribute("aria-label", "Server: Acme Engineering. Switch server");
    expect(trigger).not.toHaveAttribute("title");
    expect(screen.queryByText("omni.example")).toBeNull();

    await openMenu();
    // The current entry and the other named recent both keep their host visible in the menu.
    expect(screen.getAllByText("Acme Engineering").length).toBeGreaterThan(1);
    expect(screen.getByText("omni.example")).toBeInTheDocument();
    expect(screen.getByText("Acme Staging")).toBeInTheDocument();
    expect(screen.getByText("staging.example")).toBeInTheDocument();
    expect(screen.getByText("plain.example")).toBeInTheDocument();
    fireEvent.click(screen.getByText("Acme Staging"));
    await waitFor(() => expect(switchServer).toHaveBeenCalledWith("https://staging.example/"));
  });

  it("doesn't repeat a name that is just the host", async () => {
    getServerPicker.mockResolvedValue({
      currentOrigin: "https://omni.example",
      recentServers: [],
      serverNames: { "https://omni.example": "omni.example" },
    });
    renderPicker();
    const trigger = await openMenu();
    expect(trigger).toHaveTextContent("Server");
    expect(trigger).toHaveAttribute("aria-label", "Server. Switch server");
    expect(await screen.findAllByText("omni.example")).toHaveLength(1);
  });

  it("falls back to Server when a name repeats the bare hostname or origin of a port URL", async () => {
    getServerPicker.mockResolvedValue({
      currentOrigin: "http://server.example:8000",
      managedServers: ["http://server.example:8000/"],
      managedServerNames: { "http://server.example:8000/": "server.example" },
      serverNames: { "http://server.example:8000": "http://server.example:8000" },
      recentServers: [],
    });
    renderPicker();
    const trigger = await screen.findByTestId("sidebar-server-picker");
    expect(trigger).toHaveTextContent("Server");
    expect(trigger).toHaveAttribute("aria-label", "Server. Switch server");
    expect(trigger).not.toHaveAttribute("title");
    expect(screen.queryByText("server.example")).toBeNull();
    expect(screen.queryByText("server.example:8000")).toBeNull();
  });

  it.each([
    [
      "contained host (case-insensitive)",
      "http://server.example:8000",
      "Production (SERVER.EXAMPLE)",
    ],
    ["contained host:port", "http://server.example:8000", "Production (server.example:8000)"],
    ["URL in name", "http://server.example:8000", "Production - http://server.example:8000/"],
    [
      "trailing-dot origin vs name without dot",
      "http://server.example.:8000",
      "Production (server.example)",
    ],
    [
      "name with trailing dot vs origin without dot",
      "http://server.example:8000",
      "Production (server.example.)",
    ],
    ["short hostname token in parentheses", "http://dev:8000", "Dev box (dev)"],
    ["short hostname:port token", "http://dev:8000", "dev:8000 staging"],
    ["IPv4 host in parentheses", "http://192.0.2.5:8000", "Lab (192.0.2.5)"],
    ["bracketed IPv6 host in parentheses", "http://[2001:db8::1]:8000", "Lab ([2001:db8::1])"],
  ])(
    "rejects a manifest or managed name with %s on the collapsed row",
    async (_case, origin, candidateName) => {
      getServerPicker.mockResolvedValue({
        currentOrigin: origin,
        managedServers: [`${origin}/`],
        managedServerNames: { [`${origin}/`]: candidateName },
        serverNames: { [origin]: candidateName },
        recentServers: [],
      });
      renderPicker();
      const trigger = await screen.findByTestId("sidebar-server-picker");
      expect(trigger).toHaveTextContent("Server");
      expect(trigger).toHaveAttribute("aria-label", "Server. Switch server");
      expect(trigger).not.toHaveAttribute("title");
      expect(screen.queryByText(candidateName)).toBeNull();
    },
  );

  it.each([
    ["short hostname inside a longer word", "http://dev:8000", "dev:8000", "Developer Tools"],
    [
      "IPv4 prefix of a longer address token",
      "http://192.0.2.5:8000",
      "192.0.2.5:8000",
      "Lab 192.0.2.50",
    ],
  ])("keeps a server name when %s", async (_case, origin, expectedHost, expectedName) => {
    getServerPicker.mockResolvedValue({
      currentOrigin: origin,
      recentServers: [`${origin}/`, "https://recent.example/"],
      serverNames: {
        [origin]: expectedName,
        "https://recent.example": "recent.example",
      },
    });
    renderPicker();
    const trigger = await screen.findByTestId("sidebar-server-picker");
    expect(trigger).toHaveTextContent(expectedName);
    expect(trigger).toHaveAttribute("aria-label", `Server: ${expectedName}. Switch server`);
    expect(trigger).not.toHaveAttribute("title");
    expect(screen.queryByText(expectedHost)).toBeNull();

    await openMenu();
    expect(screen.getByText(expectedHost)).toHaveClass("text-muted-foreground");
    // A recent whose name equals its host is deduplicated to a single line.
    expect(screen.getAllByText("recent.example")).toHaveLength(1);
  });

  it("names an unnamed managed server from its manifest, with its host", async () => {
    getServerPicker.mockResolvedValue({
      currentOrigin: "http://localhost:8000",
      managedServers: ["https://omni.example/"],
      serverNames: { "https://omni.example": "Acme Eng" },
      recentServers: [],
    });
    renderPicker();
    await openMenu();
    expect(screen.getByText("Acme Eng")).toBeInTheDocument();
    expect(screen.getByText("omni.example")).toBeInTheDocument();
  });

  it("prefers the organization's name over the server's own", async () => {
    getServerPicker.mockResolvedValue({
      currentOrigin: "https://omni.example",
      managedServers: ["https://omni.example/"],
      managedServerNames: { "https://omni.example/": "Engineering" },
      serverNames: { "https://omni.example": "Self-chosen" },
      recentServers: [],
    });
    renderPicker();
    expect(await screen.findByText("Engineering")).toBeInTheDocument();
    expect(screen.queryByText("Self-chosen")).toBeNull();
  });

  it("offers Sign out when the shell owns the server's sign-in", async () => {
    signOutOfServer.mockResolvedValue(true);
    getServerPicker.mockResolvedValue({
      currentOrigin: "https://omni.example",
      recentServers: [],
      canSignOut: true,
    });
    renderPicker();
    await openMenu();
    fireEvent.click(await screen.findByText("Sign out of omni.example"));
    await waitFor(() => expect(signOutOfServer).toHaveBeenCalledOnce());
  });

  it("reports a sign-out the shell couldn't complete", async () => {
    const errorToast = vi.spyOn(toast, "error").mockImplementation(() => "id");
    signOutOfServer.mockResolvedValue(false);
    getServerPicker.mockResolvedValue({
      currentOrigin: "https://omni.example",
      recentServers: [],
      canSignOut: true,
    });
    renderPicker();
    await openMenu();
    fireEvent.click(await screen.findByText("Sign out of omni.example"));
    await waitFor(() =>
      expect(errorToast).toHaveBeenCalledWith("Couldn't sign out of omni.example"),
    );
    errorToast.mockRestore();
  });

  it("hides Sign out when the shell can't sign the server out", async () => {
    getServerPicker.mockResolvedValue({
      currentOrigin: "http://localhost:8000",
      recentServers: [],
    });
    renderPicker();
    await openMenu();
    expect(await screen.findByText("Connect to new server…")).toBeInTheDocument();
    expect(screen.queryByTestId("sidebar-server-sign-out")).toBeNull();
  });

  it("doesn't offer the workspace host it moved to as another server", async () => {
    getServerPicker.mockResolvedValue({
      currentOrigin: "https://dbc-1.cloud.databricks.com",
      currentServer: "https://accounts.example.com/omnigent?o=1",
      managedServers: ["https://dbc-1.cloud.databricks.com/"],
      recentServers: ["https://accounts.example.com/?o=1"],
    });
    renderPicker();
    await openMenu();
    const managedRow = screen
      .getAllByRole("menuitem")
      .find((item) => /dbc-1\.cloud\.databricks\.com/.test(item.textContent ?? ""));
    expect(managedRow).toHaveAttribute("data-disabled");
  });
});
