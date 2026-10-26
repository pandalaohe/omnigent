import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { InstructionsPage } from "./InstructionsPage";
import { TooltipProvider } from "@/components/ui/tooltip";
import type {
  InstructionLayer,
  InstructionLayers,
  InstructionPreview,
} from "@/hooks/useInstructionLayers";
import type * as InstructionHooks from "@/hooks/useInstructionLayers";
import type * as AgentLabelsModule from "@/lib/agentLabels";
import type * as AgentRegistryModule from "@/hooks/useAgentRegistry";
import type * as NativeBridge from "@/lib/nativeBridge";

const mocks = vi.hoisted(() => ({
  admin: true,
  ios: false,
  layers: null as InstructionLayers | null,
  preview: null as InstructionPreview | null,
  save: vi.fn(),
  copy: vi.fn(),
  previewQuery: vi.fn(),
}));
const capabilities = vi.hoisted(() => ({}));
vi.mock("@/lib/agentLabels", async (original) => ({
  ...(await original<typeof AgentLabelsModule>()),
  useHarnessCapabilities: () => capabilities,
  useHarnessCatalog: (_selector: unknown, fallback: unknown) => fallback,
}));
vi.mock("@/hooks/useAgentRegistry", async (original) => ({
  ...(await original<typeof AgentRegistryModule>()),
  useAgentRegistry: () => ({
    items: [],
    legacyLabels: {},
    legacyProducts: {},
    isLoading: false,
    isError: false,
    retry: vi.fn(),
  }),
}));
vi.mock("@/hooks/useIsAdmin", () => ({ useIsAdmin: () => mocks.admin }));
vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({ single_user: false, accounts_enabled: true }),
}));
vi.mock("@/hooks/useHosts", () => ({
  useHosts: (options: { includeSandbox?: boolean } = {}) => ({
    data: [
      {
        host_id: "host-a",
        name: "Laptop",
        status: "online",
        configured_harnesses: { "codex-native": true },
      },
      ...(options.includeSandbox
        ? [{ host_id: "host-cloud", name: "Cloud", status: "offline", sandbox_provider: "managed" }]
        : []),
    ],
    isError: false,
  }),
}));
vi.mock("@/lib/nativeBridge", async (importOriginal) => ({
  ...(await importOriginal<typeof NativeBridge>()),
  isIOSShell: () => mocks.ios,
}));
vi.mock("@/components/ai-elements/message", () => ({
  MessageResponse: ({ children }: { children: string }) => <div>{children}</div>,
}));
vi.mock("@/lib/clipboard", () => ({ copyText: (text: string) => mocks.copy(text) }));
vi.mock("@/hooks/useInstructionLayers", async (importOriginal) => ({
  ...(await importOriginal<typeof InstructionHooks>()),
  useInstructionLayers: () => ({ data: mocks.layers, isPending: false, isError: false }),
  useInstructionPreview: (query: URLSearchParams | null) => {
    mocks.previewQuery(query?.toString());
    return { data: query ? mocks.preview : undefined, isPending: false, isError: false };
  },
  useSaveInstructionLayer: () => ({ mutateAsync: mocks.save, isPending: false, isError: false }),
  useInstructionRevisions: () => ({
    data: { data: [{ id: "old", text: "Older shared", created_at: 100, created_by: "Admin" }] },
  }),
}));

function layer(
  scope: InstructionLayer["scope"],
  text: string,
  agent_key = "",
  host_id = "",
): InstructionLayer {
  return {
    scope,
    text,
    agent_key,
    host_id,
    revision_id: scope,
    updated_at: 1,
    updated_by: null,
    chars: [...text].length,
    host_removed: false,
    stale_layers: [],
  };
}
function Location() {
  const location = useLocation();
  const navigate = useNavigate();
  return (
    <>
      <output data-testid="location" data-history-key={location.key}>
        {location.search}
      </output>
      <button type="button" onClick={() => navigate(-1)}>
        History back
      </button>
      <button type="button" onClick={() => navigate(1)}>
        History forward
      </button>
    </>
  );
}
function page(url = "/settings/instructions") {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <TooltipProvider>
        <InstructionsPage />
      </TooltipProvider>
      <Location />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  localStorage.clear();
  mocks.admin = true;
  mocks.ios = false;
  mocks.layers = {
    data: [
      layer("shared", "Shared text"),
      layer("agent", "Agent text", "codex"),
      layer("host", "Host text", "", "host-a"),
    ],
    agents: [{ key: "codex", display_name: "Codex" }],
    order: ["shared", "agent", "host"],
    route_sizes: [],
    size_reminder_chars: 10000,
  };
  mocks.preview = {
    subject: { name: "Codex terminal agent", harness: "codex-native" },
    route: { host_id: "host-a", agent_key: "codex", child: false, peer: true, spawn: true },
    parts: [
      {
        kind: "user_layers",
        scope: "shared",
        agent_key: "",
        host_id: "",
        text: "Shared text",
        chars: 11,
        sent: true,
        readonly: false,
      },
      { kind: "framework", text: "Framework text", chars: 14, sent: true, readonly: true },
    ],
    text: "Shared text\n\nFramework text",
    total_chars: 27,
    framework_unavailable: false,
  };
  mocks.save.mockReset().mockResolvedValue({ over_budget_routes: [] });
  mocks.copy.mockReset().mockResolvedValue(undefined);
  mocks.previewQuery.mockClear();
});
afterEach(cleanup);

describe("InstructionsPage", () => {
  it("saves the Mother grid's Codex column with its role key", async () => {
    page();
    await userEvent.click(screen.getByRole("tab", { name: "Mother" }));
    expect(screen.getByTestId("location")).toHaveTextContent("role=mother");
    fireEvent.click(within(screen.getByRole("table")).getByRole("button", { name: "Edit Codex" }));
    fireEvent.change(screen.getByLabelText("Instruction text"), {
      target: { value: "Codex mother" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save layer" }));
    await waitFor(() =>
      expect(mocks.save).toHaveBeenCalledWith({
        scope: "mother",
        agent_key: "codex",
        host_id: "",
        text: "Codex mother",
      }),
    );
  });

  it.each(
    (["mother", "child"] as const).flatMap((role) => [
      { role, name: "Shared", text: "Role shared", agent_key: "", host_id: "" },
      { role, name: "Codex", text: "Role agent", agent_key: "codex", host_id: "" },
      { role, name: "Laptop", text: "Role host", agent_key: "", host_id: "host-a" },
    ]),
  )("reads and saves the $role $name key", async ({ role, name, text, agent_key, host_id }) => {
    mocks.layers!.data.push(
      layer(role, "Role shared"),
      layer(role, "Role agent", "codex"),
      layer(role, "Role host", "", "host-a"),
    );
    page(`/settings/instructions?role=${role}`);
    fireEvent.click(screen.getByRole("button", { name: `Edit ${name}` }));
    expect(screen.getByLabelText("Instruction text")).toHaveValue(text);
    fireEvent.change(screen.getByLabelText("Instruction text"), {
      target: { value: `New ${text}` },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save layer" }));
    await screen.findByText("Layer saved.");
    expect(mocks.save).toHaveBeenLastCalledWith({
      scope: role,
      agent_key,
      host_id,
      text: `New ${text}`,
    });
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
  });

  it.each(["", "?role=route", "?role=invalid"])("defaults %s to the shared grid", (query) => {
    page(`/settings/instructions${query}`);
    expect(screen.getByRole("tab", { name: "Shared instructions" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    fireEvent.click(screen.getByRole("button", { name: "Edit Shared" }));
    expect(screen.getByLabelText("Instruction text")).toHaveValue("Shared text");
  });

  it("switches one grid with keyboard tabs, retaining shared axes and URL context without saves", async () => {
    const layout = { hosts: ["host-a", "host-cloud"], agents: ["codex"] };
    localStorage.setItem("omnigent:instruction-grid", JSON.stringify(layout));
    page("/settings/instructions?host=host-cloud&agent=codex&peer=0&custom=retained");
    const table = screen.getByRole("table");
    act(() => screen.getByRole("tab", { name: "Shared instructions" }).focus());
    await userEvent.keyboard("{ArrowRight}");
    await waitFor(() =>
      expect(screen.getByRole("tab", { name: "Mother" })).toHaveAttribute("aria-selected", "true"),
    );
    await userEvent.keyboard("{ArrowRight}");
    await waitFor(() =>
      expect(screen.getByRole("tab", { name: "Child" })).toHaveAttribute("aria-selected", "true"),
    );
    expect(screen.getByRole("table")).toBe(table);
    expect(screen.getByRole("button", { name: "Edit Cloud" })).toBeInTheDocument();
    const query = new URLSearchParams(screen.getByTestId("location").textContent!);
    expect(Object.fromEntries(query)).toMatchObject({
      role: "child",
      host: "host-cloud",
      agent: "codex",
      peer: "0",
      custom: "retained",
    });
    expect(JSON.parse(localStorage.getItem("omnigent:instruction-grid")!)).toEqual(layout);
    expect(mocks.save).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Edit Mother block" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit Child block" })).not.toBeInTheDocument();
  });

  it("restores role selection through real history Back and Forward while Child retains focus", async () => {
    page("/settings/instructions?role=route&host=host-a&agent=codex&peer=0&custom=retained");
    const location = screen.getByTestId("location");
    const sharedHistoryKey = location.getAttribute("data-history-key");
    const mother = screen.getByRole("tab", { name: "Mother" });
    const child = screen.getByRole("tab", { name: "Child" });
    await userEvent.click(mother);
    await waitFor(() => {
      expect(new URLSearchParams(location.textContent!).get("role")).toBe("mother");
      expect(mother).toHaveAttribute("aria-selected", "true");
    });
    const motherHistoryKey = location.getAttribute("data-history-key");
    await userEvent.click(child);
    await waitFor(() => {
      expect(new URLSearchParams(location.textContent!).get("role")).toBe("child");
      expect(child).toHaveAttribute("aria-selected", "true");
    });
    const childHistoryKey = location.getAttribute("data-history-key");
    expect(child).toHaveFocus();

    // Toolbar history navigation does not move focus to an in-page button.
    fireEvent.click(screen.getByRole("button", { name: "History back" }));
    await waitFor(() => {
      expect(location).not.toHaveAttribute("data-history-key", childHistoryKey);
      expect(new URLSearchParams(location.textContent!).get("role")).toBe("mother");
      expect(location).toHaveAttribute("data-history-key", motherHistoryKey);
      expect(mother).toHaveAttribute("aria-selected", "true");
      expect(child).toHaveAttribute("aria-selected", "false");
    });
    expect(child).toHaveFocus();
    fireEvent.click(screen.getByRole("button", { name: "History back" }));
    await waitFor(() => {
      expect(new URLSearchParams(location.textContent!).get("role")).toBe("route");
      expect(location).toHaveAttribute("data-history-key", sharedHistoryKey);
      expect(screen.getByRole("tab", { name: "Shared instructions" })).toHaveAttribute(
        "aria-selected",
        "true",
      );
    });
    expect(child).toHaveFocus();
    fireEvent.click(screen.getByRole("button", { name: "History forward" }));
    await waitFor(() => {
      expect(new URLSearchParams(location.textContent!).get("role")).toBe("mother");
      expect(location).toHaveAttribute("data-history-key", motherHistoryKey);
      expect(mother).toHaveAttribute("aria-selected", "true");
    });
    fireEvent.click(screen.getByRole("button", { name: "History forward" }));
    await waitFor(() => {
      expect(new URLSearchParams(location.textContent!).get("role")).toBe("child");
      expect(location).toHaveAttribute("data-history-key", childHistoryKey);
      expect(child).toHaveAttribute("aria-selected", "true");
    });
    expect(child).toHaveFocus();
    expect(Object.fromEntries(new URLSearchParams(location.textContent!))).toEqual({
      role: "child",
      host: "host-a",
      agent: "codex",
      peer: "0",
      custom: "retained",
    });
    expect(mocks.save).not.toHaveBeenCalled();
  });

  it.each(["mother", "child"] as const)(
    "previews the %s grid's role while preserving unrelated route context",
    (role) => {
      page(`/settings/instructions?role=${role}&child=0&spawn=0&peer=0&custom=retained`);
      fireEvent.click(screen.getByRole("button", { name: "View route" }));
      const query = new URLSearchParams(screen.getByTestId("location").textContent!);
      expect(Object.fromEntries(query)).toMatchObject({
        role,
        view: "route",
        host: "host-a",
        agent: "codex",
        child: role === "child" ? "1" : "0",
        spawn: role === "mother" ? "1" : "0",
        peer: "0",
        custom: "retained",
      });
      expect(new URLSearchParams(mocks.previewQuery.mock.lastCall![0]).get("child")).toBe(
        String(role === "child"),
      );
      expect(new URLSearchParams(mocks.previewQuery.mock.lastCall![0]).get("spawn")).toBe(
        String(role === "mother"),
      );
    },
  );

  it.each(["mother", "child"] as const)(
    "prefills and saves only the %s role's independent sources",
    async (role) => {
      mocks.layers!.data.push(
        layer(role, " Role shared "),
        layer(role, " Role agent ", "codex"),
        layer(role, "\u0085", "", "host-a"),
      );
      page(`/settings/instructions?role=${role}`);
      fireEvent.click(screen.getByRole("button", { name: "Make Laptop / Codex independent" }));
      expect(screen.getByLabelText("Instruction text")).toHaveValue(
        " Role shared \n\n Role agent ",
      );
      expect(
        screen.getByText(
          `This setting replaces Shared, Agent and Host ${role} instructions on this route. Shared instructions still apply.`,
        ),
      ).toBeInTheDocument();
      expect(screen.queryByText(/· Unsaved changes/)).not.toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "Save layer" }));
      await screen.findByText("Layer saved.");
      expect(mocks.save).toHaveBeenCalledWith({
        scope: role,
        agent_key: "codex",
        host_id: "host-a",
        text: " Role shared \n\n Role agent ",
      });
      fireEvent.click(screen.getByRole("button", { name: "Close" }));
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    },
  );

  it.each(["mother", "child"] as const)(
    "clears a %s independent setting without clearing route layers",
    async (role) => {
      mocks.layers!.data.push(layer(role, "Role cell", "codex", "host-a"));
      page(`/settings/instructions?role=${role}`);
      fireEvent.click(screen.getByRole("button", { name: "Edit Laptop / Codex" }));
      fireEvent.click(screen.getByRole("button", { name: "Remove independent setting" }));
      await waitFor(() =>
        expect(mocks.save).toHaveBeenCalledExactlyOnceWith({
          scope: role,
          agent_key: "codex",
          host_id: "host-a",
          text: "",
        }),
      );
    },
  );

  it.each([
    { scope: "mother", agent_key: "codex", host_id: "" },
    { scope: "mother", agent_key: "codex", host_id: "host-a" },
    { scope: "child", agent_key: "", host_id: "host-a" },
    { scope: "child", agent_key: "codex", host_id: "host-a" },
  ] as const)("edits the original preview role key $scope / $agent_key / $host_id", async (key) => {
    mocks.layers!.data.push(layer(key.scope, "Role preview", key.agent_key, key.host_id));
    mocks.preview!.parts = [
      {
        ...key,
        kind: `${key.scope}_block`,
        text: "Role preview",
        chars: 12,
        sent: true,
        readonly: false,
      },
    ];
    page("/settings/instructions?session=session-a");
    fireEvent.click(screen.getByRole("button", { name: "Edit layer" }));
    expect(screen.getByLabelText("Instruction text")).toHaveValue("Role preview");
    fireEvent.change(screen.getByLabelText("Instruction text"), {
      target: { value: "Changed role" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save layer" }));
    await waitFor(() => expect(mocks.save).toHaveBeenCalledWith({ ...key, text: "Changed role" }));
  });

  it.each([
    { total: 9999, compact: null },
    { total: 10000, compact: null },
    { total: 10001, compact: "10.0k" },
    { total: 11234, compact: "11.2k" },
    { total: 123456, compact: "123.5k" },
  ])("only shows a compact warning total above the limit: $total", ({ total, compact }) => {
    mocks.layers!.route_sizes = [{ host_id: "host-a", agent_key: "codex", total_chars: total }];
    page();
    const warning = screen.queryByRole("button", { name: /instruction characters/ });
    if (compact === null) expect(warning).not.toBeInTheDocument();
    else {
      expect(warning).toHaveTextContent(compact);
      expect(warning).toHaveClass("text-warning");
    }
    expect(screen.queryByText("Over 10,000")).not.toBeInTheDocument();
  });
  it("explains the exact total and included sections in the warning tooltip", async () => {
    mocks.layers!.route_sizes = [
      { host_id: "host-a", agent_key: "codex", total_chars: 11234, over_budget: true },
    ];
    page();
    fireEvent.focus(screen.getByRole("button", { name: "11,234 instruction characters" }));
    const tooltip = await screen.findByRole("tooltip");
    expect(tooltip).toHaveTextContent("11,234 characters");
    expect(tooltip).toHaveTextContent("largest of top-level and child-session totals");
    expect(tooltip).toHaveTextContent("including built-in sections");
    expect(tooltip).toHaveTextContent("never truncated");
  });
  it("qualifies the count when built-in sections could not be counted", async () => {
    mocks.layers!.route_sizes = [
      {
        host_id: "host-a",
        agent_key: "codex",
        total_chars: 11234,
        framework_unavailable: true,
      },
    ];
    page();
    fireEvent.focus(screen.getByRole("button", { name: "11,234 instruction characters" }));
    const tooltip = await screen.findByRole("tooltip");
    expect(tooltip).toHaveTextContent("Built-in sections are unavailable and not counted");
    expect(tooltip).not.toHaveTextContent("including built-in sections");
    expect(tooltip).toHaveTextContent("never truncated");
  });
  it("includes managed hosts in the matrix and route picker", async () => {
    page();
    fireEvent.click(screen.getByRole("button", { name: "Add host" }));
    fireEvent.click(await screen.findByRole("button", { name: /Cloud.*offline/ }));
    expect(
      screen.getByRole("button", { name: "Make Cloud / Codex independent" }),
    ).toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: "Route total" }));
    fireEvent.click(screen.getByRole("combobox", { name: "Host" }));
    expect(await screen.findByRole("option", { name: "Cloud" })).toBeInTheDocument();
  });
  it("preserves a session's missing host and product without editing a different route", () => {
    mocks.preview!.route.host_id = null;
    mocks.preview!.route.agent_key = null;
    page("/settings/instructions?session=session-a");
    expect(screen.getByRole("combobox", { name: "Host" })).toHaveTextContent("Select host");
    expect(screen.getByRole("combobox", { name: "Agent" })).toHaveTextContent("Select agent");
    expect(
      screen.queryByRole("button", { name: "Edit independent setting" }),
    ).not.toBeInTheDocument();
  });
  it("reapplies bulk disclosure actions after individual sections change", async () => {
    page("/settings/instructions?view=route");
    const details = screen.getByText("Framework text").closest("details")!;
    fireEvent.click(screen.getByRole("button", { name: "Expand all" }));
    expect(details.open).toBe(true);
    details.open = false;
    fireEvent(details, new Event("toggle"));
    fireEvent.click(screen.getByRole("button", { name: "Expand all" }));
    await waitFor(() => expect(details.open).toBe(true));
    fireEvent.click(screen.getByRole("button", { name: "Collapse all" }));
    details.open = true;
    fireEvent(details, new Event("toggle"));
    fireEvent.click(screen.getByRole("button", { name: "Collapse all" }));
    await waitFor(() => expect(details.open).toBe(false));
  });
  it("pins the iOS editor within the visible viewport and safe areas", () => {
    mocks.ios = true;
    page();
    fireEvent.click(screen.getByRole("button", { name: "Edit Shared" }));
    expect(screen.getByRole("dialog").style.top).toBe("var(--omnigent-safe-top, 0px)");
    expect(screen.getByRole("dialog").style.height).toContain("--omnigent-viewport-height");
    expect(screen.getByRole("dialog").style.maxHeight).toContain("--omnigent-safe-bottom");
  });
  it("searches a route picker and retains an addressable host and agent", async () => {
    page("/settings/instructions?view=route");
    fireEvent.click(screen.getByRole("combobox", { name: "Host" }));
    fireEvent.change(screen.getByPlaceholderText("Search host…"), { target: { value: "Lap" } });
    fireEvent.click(await screen.findByRole("option", { name: "Laptop" }));
    expect(screen.getByTestId("location")).toHaveTextContent("host=host-a&agent=codex");
  });
  it("warns about new over-budget routes while retaining the saved text", async () => {
    mocks.save.mockResolvedValue({
      over_budget_routes: [{ host_id: "host-a", agent_key: "codex", total_chars: 10001 }],
    });
    page();
    fireEvent.click(screen.getByRole("button", { name: "Edit Shared" }));
    fireEvent.change(screen.getByLabelText("Instruction text"), { target: { value: "Changed" } });
    fireEvent.click(screen.getByRole("button", { name: "Save layer" }));
    await screen.findByText(/Newly over 10,000 characters/);
    expect(screen.getByLabelText("Instruction text")).toHaveValue("Changed");
  });
  it("clears an independent setting with a new blank revision", async () => {
    mocks.layers!.data.push(layer("independent", "Independent text", "codex", "host-a"));
    page();
    fireEvent.click(screen.getByRole("button", { name: "Edit Laptop / Codex" }));
    fireEvent.click(screen.getByRole("button", { name: "Remove independent setting" }));
    await waitFor(() =>
      expect(mocks.save).toHaveBeenCalledWith({
        scope: "independent",
        agent_key: "codex",
        host_id: "host-a",
        text: "",
      }),
    );
  });
  it("edits a scoped layer, counts Unicode, restores history and saves large text intact", async () => {
    page();
    fireEvent.click(screen.getByRole("button", { name: "Edit Shared" }));
    const editor = screen.getByLabelText("Instruction text");
    expect(editor).toHaveValue("Shared text");
    expect(screen.getByRole("button", { name: "Save layer" })).toBeDisabled();
    fireEvent.change(editor, { target: { value: "😀😀" } });
    expect(screen.getByText("2 characters · Unsaved changes")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Older shared/ }));
    expect(editor).toHaveValue("Older shared");
    const text = "x".repeat(65536);
    fireEvent.change(editor, { target: { value: text } });
    fireEvent.click(screen.getByRole("button", { name: "Save layer" }));
    await waitFor(() =>
      expect(mocks.save).toHaveBeenCalledWith({
        scope: "shared",
        agent_key: "",
        host_id: "",
        text,
      }),
    );
  });
  it("keeps a newer draft when an earlier save completes", async () => {
    let finish: (value: { over_budget_routes: [] }) => void = () => {};
    mocks.save.mockReturnValue(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    page();
    fireEvent.click(screen.getByRole("button", { name: "Edit Shared" }));
    const editor = screen.getByLabelText("Instruction text");
    fireEvent.change(editor, { target: { value: "AB" } });
    fireEvent.click(screen.getByRole("button", { name: "Save layer" }));
    fireEvent.change(editor, { target: { value: "ABC" } });
    await act(async () => finish({ over_budget_routes: [] }));
    expect(editor).toHaveValue("ABC");
    expect(screen.getByRole("button", { name: "Save layer" })).toBeEnabled();
  });
  it("prefills an independent setting from shared → agent → host", () => {
    page();
    fireEvent.click(screen.getByRole("button", { name: "Make Laptop / Codex independent" }));
    expect(screen.getByLabelText("Instruction text")).toHaveValue(
      "Shared text\n\nAgent text\n\nHost text",
    );
    expect(mocks.save).not.toHaveBeenCalled();
  });
  it("saves an untouched prefill as an independent setting without marking it dirty", async () => {
    page("/settings/instructions?view=route");
    fireEvent.click(screen.getByRole("button", { name: "Edit independent setting" }));
    expect(screen.queryByText(/· Unsaved changes/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Save layer" }));
    await screen.findByText("Layer saved.");
    expect(mocks.save).toHaveBeenCalledWith({
      scope: "independent",
      agent_key: "codex",
      host_id: "host-a",
      text: "Shared text\n\nAgent text\n\nHost text",
    });
    expect(screen.getByRole("button", { name: "Save layer" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
  it.each(["Close", "Escape"])("closes an untouched independent draft with %s", async (action) => {
    page();
    fireEvent.click(screen.getByRole("button", { name: "Make Laptop / Codex independent" }));
    expect(screen.getByLabelText("Instruction text")).toHaveValue(
      "Shared text\n\nAgent text\n\nHost text",
    );
    if (action === "Close") fireEvent.click(screen.getByRole("button", { name: "Close" }));
    else await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(mocks.save).not.toHaveBeenCalled();
  });
  it("keeps the discard prompt above long edited text and discards only on request", async () => {
    page();
    fireEvent.click(screen.getByRole("button", { name: "Make Laptop / Codex independent" }));
    const editor = screen.getByLabelText("Instruction text");
    fireEvent.change(editor, { target: { value: "Edited instructions\n".repeat(500) } });
    await userEvent.keyboard("{Escape}");
    const prompt = screen.getByRole("alert");
    expect(prompt).toHaveTextContent("You have unsaved changes.");
    expect(prompt.compareDocumentPosition(editor) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(prompt).toHaveClass("sticky", "top-0");
    fireEvent.click(screen.getByRole("button", { name: "Keep editing" }));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(editor).toHaveValue("Edited instructions\n".repeat(500));
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    fireEvent.click(screen.getByRole("button", { name: "Discard changes" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(mocks.save).not.toHaveBeenCalled();
  });
  it.each(["Shared", "Codex", "Laptop"])("preserves dirty tracking for the %s layer", (name) => {
    page();
    fireEvent.click(screen.getByRole("button", { name: `Edit ${name}` }));
    const editor = screen.getByLabelText("Instruction text");
    const original = (editor as HTMLTextAreaElement).value;
    expect(screen.getByRole("button", { name: "Save layer" })).toBeDisabled();
    fireEvent.change(editor, { target: { value: "Changed" } });
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.getByRole("alert")).toHaveTextContent("You have unsaved changes.");
    fireEvent.click(screen.getByRole("button", { name: "Keep editing" }));
    fireEvent.change(editor, { target: { value: original } });
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
  it("reads the selected URL route and copies the exact composed total", async () => {
    page("/settings/instructions?view=route&host=host-a&agent=codex&child=1&peer=0");
    expect(mocks.previewQuery).toHaveBeenLastCalledWith(
      "host_id=host-a&agent_key=codex&child=true&peer=false",
    );
    fireEvent.click(screen.getByRole("button", { name: "Copy all" }));
    await waitFor(() => expect(mocks.copy).toHaveBeenCalledWith("Shared text\n\nFramework text"));
  });
  it("previews a session's own route and allows readers without edit controls", () => {
    mocks.admin = false;
    page("/settings/instructions?session=session-a");
    expect(mocks.previewQuery).toHaveBeenLastCalledWith("session_id=session-a");
    expect(screen.getByText("Codex terminal agent")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Edit independent setting" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit layer" })).not.toBeInTheDocument();
  });
  it.each([
    { scope: "host" as const, text: "" },
    { scope: "host" as const, text: "\u0085" },
    { scope: "host" as const, text: "\u001c" },
    { scope: "independent" as const, text: " \n " },
  ])(
    "labels an empty $scope layer as not sent without an independent setting",
    ({ scope, text }) => {
      mocks.preview!.parts.push({
        kind: "user_layers",
        scope,
        host_id: "host-a",
        agent_key: scope === "independent" ? "codex" : "",
        text,
        chars: text.length,
        sent: false,
        readonly: false,
      });
      page("/settings/instructions?view=route");
      const empty = screen.getByRole("heading", { name: "Empty layers · Not sent" }).parentElement!;
      expect(
        within(empty).getByText(scope === "host" ? "Host" : "Independent"),
      ).toBeInTheDocument();
      expect(within(empty).getByText(/Not sent on this route/)).toBeInTheDocument();
      expect(
        screen.queryByRole("heading", { name: "Replaced by the independent setting" }),
      ).not.toBeInTheDocument();
      expect(screen.getByText("Shared text")).toBeInTheDocument();
    },
  );
  it.each(["Shared text", "\ufeff"])("separates replaced text (%j) from empty layers", (text) => {
    mocks.preview!.parts[0].sent = false;
    mocks.preview!.parts[0].text = text;
    mocks.preview!.parts[0].chars = text.length;
    mocks.preview!.parts.push(
      {
        kind: "user_layers",
        scope: "independent",
        host_id: "host-a",
        agent_key: "codex",
        text: "Independent text",
        chars: 16,
        sent: true,
        readonly: false,
      },
      {
        kind: "user_layers",
        scope: "host",
        host_id: "host-a",
        agent_key: "",
        text: "",
        chars: 0,
        sent: false,
        readonly: false,
      },
    );
    mocks.preview!.framework_unavailable = true;
    page("/settings/instructions?view=route");
    const replaced = screen.getByRole("heading", {
      name: "Replaced by the independent setting",
    }).parentElement!;
    expect(within(replaced).getByText("Shared")).toBeInTheDocument();
    expect(replaced.textContent).toContain(text);
    expect(within(replaced).queryByText("Host")).not.toBeInTheDocument();
    const empty = screen.getByRole("heading", { name: "Empty layers · Not sent" }).parentElement!;
    expect(within(empty).getByText("Host")).toBeInTheDocument();
    expect(within(empty).queryByText("Shared text")).not.toBeInTheDocument();
    expect(screen.getByText("Independent text")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("This preview is incomplete");
  });
});

it("persists an added ACP column while a blank latest layer alone stays hidden", async () => {
  mocks.layers!.agents.push({
    key: "acp:plugin",
    display_name: "Plugin",
    harnesses: ["acp:plugin"],
  });
  mocks.layers!.data.push(layer("agent", "  ", "acp:plugin"));
  const view = page();
  expect(screen.queryByRole("button", { name: "Edit Plugin" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Add agent" }));
  fireEvent.click(await screen.findByRole("button", { name: /Plugin.*ACP/ }));
  expect(screen.getByRole("button", { name: "Edit Plugin" })).toBeInTheDocument();
  view.unmount();
  page();
  expect(screen.getByRole("button", { name: "Edit Plugin" })).toBeInTheDocument();
});

it("starts with Shared and the axis pickers when no layers hold text", () => {
  mocks.layers!.data = [];
  page();
  const table = screen.getByRole("table", { name: "Instruction layers by host and agent" });
  expect(within(table).getByRole("button", { name: "Edit Shared" })).toBeInTheDocument();
  expect(within(table).getByRole("button", { name: "Add agent" })).toBeInTheDocument();
  expect(within(table).getByRole("button", { name: "Add host" })).toBeInTheDocument();
  expect(
    within(table).queryByRole("button", { name: /^Edit (Laptop|Cloud|Codex)/ }),
  ).not.toBeInTheDocument();
  expect(within(table).queryByRole("button", { name: /^Make / })).not.toBeInTheDocument();
  expect(
    screen.getByText(
      "Add an agent column or a host row to give one agent or one host its own text.",
    ),
  ).toBeInTheDocument();
  expect(
    within(table)
      .getAllByRole("row")
      .map((row) => row.children.length),
  ).toEqual([2, 2]);
});

it("adds and removes an empty host row without saving and persists the removal", async () => {
  mocks.admin = false;
  const view = page();
  fireEvent.click(screen.getByRole("button", { name: "Add host" }));
  expect(
    await screen.findByRole("button", { name: /Laptop.*online.*already listed/ }),
  ).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: /Cloud.*offline/ }));
  expect(screen.getByRole("button", { name: "Edit Cloud" })).toBeInTheDocument();
  expect(screen.getAllByRole("row").map((row) => row.children.length)).toEqual([3, 3, 3, 3]);
  view.unmount();
  const remounted = page();
  expect(screen.getByRole("button", { name: "Edit Cloud" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Remove Cloud row" }));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Edit Cloud" })).not.toBeInTheDocument();
  expect(mocks.save).not.toHaveBeenCalled();
  remounted.unmount();
  page();
  expect(screen.queryByRole("button", { name: "Edit Cloud" })).not.toBeInTheDocument();
});

it("confirms every held layer in a column and clears only those keys sequentially", async () => {
  mocks.layers!.data.push(layer("independent", "Independent text", "codex", "host-a"), {
    ...layer("independent", "Retained", "codex", "removed"),
    host_removed: true,
  });
  localStorage.setItem(
    "omnigent:instruction-grid",
    JSON.stringify({ hosts: ["host-a"], agents: ["codex"] }),
  );
  page();
  expect(screen.getByRole("button", { name: "Edit Laptop / Codex" })).toHaveTextContent(
    "Independent16 chars",
  );
  fireEvent.click(screen.getByRole("button", { name: "Remove Codex column" }));
  const dialog = screen.getByRole("dialog");
  expect(within(dialog).getByRole("heading", { name: "Remove Codex?" })).toBeInTheDocument();
  expect(
    within(dialog)
      .getAllByRole("listitem")
      .map((item) => item.textContent),
  ).toEqual(["Codex · 10 chars", "Laptop / Codex · 16 chars"]);
  fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
  expect(mocks.save).not.toHaveBeenCalled();
  expect(JSON.parse(localStorage.getItem("omnigent:instruction-grid")!)).toEqual({
    hosts: ["host-a"],
    agents: ["codex"],
  });
  let finish: (value: unknown) => void = () => {};
  mocks.save.mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  fireEvent.click(screen.getByRole("button", { name: "Remove Codex column" }));
  fireEvent.click(screen.getByRole("button", { name: "Clear text and remove" }));
  expect(mocks.save).toHaveBeenCalledTimes(1);
  expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
  await act(async () => finish({ over_budget_routes: [] }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(mocks.save.mock.calls).toEqual([
    [{ scope: "agent", agent_key: "codex", host_id: "", text: "", expected_revision_id: "agent" }],
    [
      {
        scope: "independent",
        agent_key: "codex",
        host_id: "host-a",
        text: "",
        expected_revision_id: "independent",
      },
    ],
  ]);
  expect(JSON.parse(localStorage.getItem("omnigent:instruction-grid")!)).toEqual({
    hosts: ["host-a"],
    agents: [],
  });
});

it("keeps only failed layers in the removal dialog and retries them with their revisions", async () => {
  mocks.layers!.data.push(layer("independent", "Independent text", "codex", "host-a"));
  const layout = { hosts: ["host-a"], agents: ["codex"] };
  localStorage.setItem("omnigent:instruction-grid", JSON.stringify(layout));
  mocks.save
    .mockResolvedValueOnce({ over_budget_routes: [] })
    .mockRejectedValueOnce(new Error("Save failed"));
  page();
  fireEvent.click(screen.getByRole("button", { name: "Remove Codex column" }));
  fireEvent.click(screen.getByRole("button", { name: "Clear text and remove" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Save failed");
  expect(screen.getByRole("dialog")).toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: "Remove Codex column", hidden: true }),
  ).toBeInTheDocument();
  expect(JSON.parse(localStorage.getItem("omnigent:instruction-grid")!)).toEqual(layout);
  expect(
    within(screen.getByRole("dialog"))
      .getAllByRole("listitem")
      .map((item) => item.textContent),
  ).toEqual(["Laptop / Codex · 16 chars"]);
  expect(mocks.save).toHaveBeenCalledTimes(2);
  fireEvent.click(screen.getByRole("button", { name: "Clear text and remove" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  const independentClear = {
    scope: "independent",
    agent_key: "codex",
    host_id: "host-a",
    text: "",
    expected_revision_id: "independent",
  };
  expect(mocks.save.mock.calls).toEqual([
    [{ scope: "agent", agent_key: "codex", host_id: "", text: "", expected_revision_id: "agent" }],
    [independentClear],
    [independentClear],
  ]);
  expect(JSON.parse(localStorage.getItem("omnigent:instruction-grid")!)).toEqual({
    hosts: ["host-a"],
    agents: [],
  });
});

it.each([
  { axis: "hosts", button: "Remove Laptop row", remaining: ["host-cloud"] },
  { axis: "agents", button: "Remove Codex column", remaining: ["agent-b"] },
])(
  "preserves layout additions while clearing a $axis axis",
  async ({ axis, button, remaining }) => {
    localStorage.setItem(
      "omnigent:instruction-grid",
      JSON.stringify({ hosts: ["host-a"], agents: ["codex"] }),
    );
    let finish: (value: unknown) => void = () => {};
    mocks.save.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    page();
    fireEvent.click(screen.getByRole("button", { name: button }));
    fireEvent.click(screen.getByRole("button", { name: "Clear text and remove" }));
    expect(mocks.save).toHaveBeenCalledTimes(1);
    const latest = { hosts: ["host-a", "host-cloud"], agents: ["codex", "agent-b"] };
    act(() => {
      localStorage.setItem("omnigent:instruction-grid", JSON.stringify(latest));
      window.dispatchEvent(new Event("omnigent:instruction-grid-changed"));
    });
    await act(async () => finish({ over_budget_routes: [] }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(JSON.parse(localStorage.getItem("omnigent:instruction-grid")!)).toEqual({
      ...latest,
      [axis]: remaining,
    });
  },
);

it.each([
  { picker: "Add host", choice: /Cloud.*offline/, axis: "hosts", id: "host-cloud" },
  { picker: "Add agent", choice: /Agent B/, axis: "agents", id: "agent-b" },
])("uses the latest stored layout when choosing $picker", async ({ picker, choice, axis, id }) => {
  mocks.layers!.agents.push({ key: "agent-b", display_name: "Agent B" });
  page();
  fireEvent.click(screen.getByRole("button", { name: picker }));
  const latest = { hosts: ["host-a"], agents: ["codex"] };
  localStorage.setItem("omnigent:instruction-grid", JSON.stringify(latest));
  fireEvent.click(await screen.findByRole("button", { name: choice }));
  expect(JSON.parse(localStorage.getItem("omnigent:instruction-grid")!)).toEqual({
    ...latest,
    [axis]: [...latest[axis as keyof typeof latest], id],
  });
});

it.each(["host", "mother", "child"] as const)(
  "disables removing a row held by %s text for readers",
  (scope) => {
    mocks.admin = false;
    mocks.layers!.data = [layer(scope, "Held host text", "", "host-a")];
    page();
    const remove = screen.getByRole("button", { name: "Remove Laptop row" });
    expect(remove).toBeDisabled();
    expect(remove).toHaveAttribute("title", "Only administrators can clear saved text.");
  },
);

it.each(["hosts", "agents"] as const)(
  "clears a %s axis across three grids and retries only pending role cells",
  async (axis) => {
    const sources = [
      layer(
        axis === "hosts" ? "host" : "agent",
        "Route source",
        axis === "agents" ? "codex" : "",
        axis === "hosts" ? "host-a" : "",
      ),
      ...(["mother", "child"] as const).flatMap((scope) => [
        layer(
          scope,
          "Role source",
          axis === "agents" ? "codex" : "",
          axis === "hosts" ? "host-a" : "",
        ),
        layer(scope, "Role cell", "codex", "host-a"),
      ]),
    ];
    const retained = [
      layer("shared", "All routes"),
      layer("mother", "All mothers"),
      layer("child", "All children"),
      { ...layer("mother", "Removed host", "codex", "removed"), host_removed: true },
      layer("independent", "\u0085", "codex", "host-a"),
    ];
    mocks.layers!.data = [...sources, ...retained];
    const layout = { hosts: ["host-a"], agents: ["codex"] };
    localStorage.setItem("omnigent:instruction-grid", JSON.stringify(layout));
    mocks.save
      .mockResolvedValueOnce({ over_budget_routes: [] })
      .mockResolvedValueOnce({ over_budget_routes: [] })
      .mockRejectedValueOnce(new Error("Role save failed"));
    page("/settings/instructions?role=mother");
    fireEvent.click(
      screen.getByRole("button", {
        name: axis === "hosts" ? "Remove Laptop row" : "Remove Codex column",
      }),
    );
    const dialog = screen.getByRole("dialog");
    for (const name of ["Shared instructions · 1 layer", "Mother · 2 layers", "Child · 2 layers"]) {
      expect(within(dialog).getByRole("heading", { name })).toBeInTheDocument();
    }
    expect(within(dialog).getAllByRole("listitem")).toHaveLength(5);
    expect(
      within(dialog)
        .getAllByRole("listitem")
        .map((item) => item.textContent),
    ).toEqual([
      `${axis === "hosts" ? "Laptop" : "Codex"} · 12 chars`,
      `${axis === "hosts" ? "Laptop" : "Codex"} · 11 chars`,
      "Laptop / Codex · 9 chars",
      `${axis === "hosts" ? "Laptop" : "Codex"} · 11 chars`,
      "Laptop / Codex · 9 chars",
    ]);
    fireEvent.click(screen.getByRole("button", { name: "Clear text and remove" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Role save failed");
    expect(
      within(dialog).queryByRole("heading", { name: "Shared instructions · 1 layer" }),
    ).not.toBeInTheDocument();
    expect(within(dialog).getByRole("heading", { name: "Mother · 1 layer" })).toBeInTheDocument();
    expect(within(dialog).getByRole("heading", { name: "Child · 2 layers" })).toBeInTheDocument();
    expect(within(dialog).getAllByRole("listitem")).toHaveLength(3);
    expect(JSON.parse(localStorage.getItem("omnigent:instruction-grid")!)).toEqual(layout);
    fireEvent.click(screen.getByRole("button", { name: "Clear text and remove" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(mocks.save.mock.calls.map(([body]) => body)).toEqual(
      [sources[0], sources[1], sources[2], sources[2], sources[3], sources[4]].map((source) => ({
        scope: source.scope,
        agent_key: source.agent_key,
        host_id: source.host_id,
        text: "",
        expected_revision_id: source.revision_id,
      })),
    );
    expect(JSON.parse(localStorage.getItem("omnigent:instruction-grid")!)).toEqual({
      ...layout,
      [axis]: [],
    });
  },
);
