import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NewTerminalButton } from "./NewTerminalButton";

const state = vi.hoisted(() => ({
  shells: ["zsh", "bash"],
  admin: true,
  phone: false,
  local: vi.fn(),
  linked: vi.fn(),
  launch: vi.fn(),
  toast: vi.fn(),
}));
vi.mock("@/components/ui/toast", () => ({ showToast: state.toast }));
vi.mock("@/hooks/useAgents", () => ({
  useSessionAgent: () => ({ data: { terminals: state.shells } }),
}));
vi.mock("@/hooks/useSession", () => ({ useSession: () => ({ session: { hostId: "host-a" } }) }));
vi.mock("@/hooks/useIsAdmin", () => ({ useIsAdmin: () => state.admin }));
vi.mock("@/hooks/useIsPhone", () => ({ useIsPhone: () => state.phone }));
vi.mock("@/hooks/useHosts", () => ({
  useHosts: () => ({
    data: [
      {
        host_id: "host-a",
        name: "Session machine",
        status: "online",
        capabilities: ["terminal_elevated_v1"],
      },
      {
        host_id: "host-b",
        name: "Other machine",
        status: "online",
        capabilities: ["host_terminal_links_v1", "terminal_elevated_v1"],
      },
      {
        host_id: "host-c",
        name: "Offline machine",
        status: "offline",
        capabilities: ["host_terminal_links_v1"],
      },
      { host_id: "host-d", name: "Old machine", status: "online", capabilities: [] },
    ],
  }),
}));
vi.mock("@/hooks/useTerminals", () => ({
  useCreateTerminal: () => ({ mutateAsync: state.local, launch: state.launch, isPending: false }),
  terminalTabKey: (t: { id: string }) => `terminal:${t.id}`,
}));
vi.mock("@/hooks/useLinkedTerminals", () => ({
  useCreateLinkedTerminal: () => ({ mutateAsync: state.linked, isPending: false }),
}));

async function openMenu() {
  fireEvent.pointerDown(screen.getByRole("button", { name: "New shell" }), { button: 0 });
  await screen.findByRole("menuitem", { name: "Shell (zsh)" });
}
async function options() {
  await openMenu();
  fireEvent.click(screen.getByRole("button", { name: "Shell options" }));
  await screen.findByText("Shell options");
}
beforeEach(() => {
  state.shells = ["zsh", "bash"];
  state.admin = true;
  state.phone = false;
  state.local.mockReset().mockResolvedValue({ id: "local" });
  state.linked.mockReset().mockResolvedValue({ id: "remote" });
  state.launch.mockReset();
  state.toast.mockReset();
  localStorage.clear();
});
afterEach(cleanup);

describe("unified shell entry", () => {
  it("reports a delayed default launch failure after the menu unmounts", async () => {
    let rejectLaunch!: (error: Error) => void;
    state.launch.mockImplementation((_shell, callbacks) => {
      rejectLaunch = callbacks.onError;
    });
    render(<NewTerminalButton conversationId="session" />);
    await openMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "Shell (zsh)" }));
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    act(() => rejectLaunch(new Error("The terminal host went offline")));
    expect(state.toast).toHaveBeenCalledWith(
      "Couldn't open a shell: The terminal host went offline",
      { duration: 0 },
    );
    expect(screen.queryByRole("menu")).toBeNull();
  });
  it("uses the shell selected in Options as the next default", async () => {
    render(<NewTerminalButton conversationId="session" />);
    await options();
    fireEvent.change(screen.getByRole("combobox", { name: "Shell" }), {
      target: { value: "bash" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Open shell" }));
    await waitFor(() =>
      expect(state.local).toHaveBeenCalledWith(expect.objectContaining({ terminal: "bash" })),
    );
    await waitFor(() => expect(screen.queryByRole("combobox", { name: "Shell" })).toBeNull());
    fireEvent.click(await screen.findByRole("menuitem", { name: "Shell (bash)" }));
    expect(state.launch).toHaveBeenCalledWith("bash", expect.any(Object));
  });
  it("opens phone options as a dismissible sheet without creating a shell", async () => {
    state.phone = true;
    render(<NewTerminalButton conversationId="session" />);
    await openMenu();
    fireEvent.click(screen.getByRole("button", { name: "Shell options" }));
    const sheet = await screen.findByRole("dialog", { name: "Shell options" });
    expect(within(sheet).getByRole("combobox", { name: "Host" })).toHaveValue("host-a");
    fireEvent.click(within(sheet).getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(state.local).not.toHaveBeenCalled();
    expect(state.linked).not.toHaveBeenCalled();
  });
  it("has one default Shell entry and no separate Admin entry", async () => {
    render(<NewTerminalButton conversationId="session" />);
    await openMenu();
    expect(screen.getAllByRole("menuitem")).toHaveLength(1);
    fireEvent.click(screen.getByRole("menuitem", { name: "Shell (zsh)" }));
    expect(state.launch).toHaveBeenCalledWith("zsh", expect.any(Object));
  });
  it("hides the entry when no shell is offered", () => {
    state.shells = [];
    render(<NewTerminalButton conversationId="session" />);
    expect(screen.queryByRole("button", { name: "New shell" })).toBeNull();
  });
  it("defaults Options to this session host and read/write, gates offline/old hosts", async () => {
    render(<NewTerminalButton conversationId="session" />);
    await options();
    expect(screen.getByRole("combobox", { name: "Host" })).toHaveValue("host-a");
    expect(screen.getByRole("option", { name: /Offline machine/ })).toBeDisabled();
    expect(screen.getByRole("option", { name: /Old machine.*update the host/ })).toBeDisabled();
    expect(
      within(screen.getByRole("group", { name: "Initial agent access" })).getByRole("button", {
        name: "Read & write",
      }),
    ).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByRole("button", { name: "Open shell" }));
    await waitFor(() =>
      expect(state.local).toHaveBeenCalledWith({
        terminal: "zsh",
        elevated: false,
        agentTier: "read_write",
      }),
    );
  });
  it("admin defaults to Off and other-host opening preserves the selected Read tier", async () => {
    const created = vi.fn();
    render(<NewTerminalButton conversationId="session" onCreated={created} />);
    await options();
    fireEvent.change(screen.getByRole("combobox", { name: "Host" }), {
      target: { value: "host-b" },
    });
    fireEvent.click(screen.getByRole("checkbox", { name: "Administrator" }));
    const group = within(screen.getByRole("group", { name: "Initial agent access" }));
    expect(group.getByRole("button", { name: "Off" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(group.getByRole("button", { name: "Read" }));
    fireEvent.click(screen.getByRole("button", { name: "Open shell" }));
    await waitFor(() =>
      expect(state.linked).toHaveBeenCalledWith({
        host_id: "host-b",
        elevated: true,
        agent_tier: "read",
      }),
    );
    expect(created).toHaveBeenCalledWith("terminal:remote");
  });
  it("shows why admin is unavailable and reports create errors", async () => {
    state.admin = false;
    state.local.mockRejectedValue(new Error("host unavailable"));
    render(<NewTerminalButton conversationId="session" />);
    await options();
    expect(screen.getByRole("checkbox", { name: "Administrator" })).toBeDisabled();
    expect(screen.getByText("Administrator access is required.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Open shell" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("host unavailable");
  });
});
