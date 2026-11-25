import type { ReactElement } from "react";
import { MemoryRouter, Outlet } from "react-router-dom";
import {
  cleanup,
  fireEvent,
  render as renderComponent,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { CustomAgent } from "@/lib/customAgentsApi";
import type { HarnessCapability } from "@/lib/agentLabels";
import type * as AgentLabels from "@/lib/agentLabels";
import type * as CustomAgentsApi from "@/lib/customAgentsApi";
import { Link, useLocation } from "@/lib/routing";
import App from "@/App";
import { writeAgentBadgePreferences } from "@/lib/agentBadgePreferences";

const mocks = vi.hoisted(() => ({
  hosts: [] as Record<string, unknown>[],
  capabilities: {} as Record<string, HarnessCapability>,
  catalogRows: [] as Record<string, unknown>[],
  listCatalogs: vi.fn(),
  sync: vi.fn(),
  queuePatch: vi.fn(),
  customAgents: [] as CustomAgent[],
  installed: [] as { id: string; name: string; harness: string; integration_mode?: string }[],
  installedNext: [] as { id: string; name: string; harness: string }[],
  agents: [
    {
      id: "agent-claude",
      name: "claude-native",
      display_name: "Claude Code",
      harness: "claude-native",
      skills: [],
      description: null,
    },
  ],
}));
vi.mock("@/hooks/useAvailableAgents", () => ({
  useAvailableAgents: () => ({ data: mocks.agents }),
}));
vi.mock("@/lib/customAgentsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof CustomAgentsApi>()),
  useCustomAgents: () => ({ data: mocks.customAgents }),
}));

vi.mock("@/hooks/useHosts", () => ({
  useHosts: () => ({ data: mocks.hosts }),
}));
vi.mock("@/lib/callingDefaultsApi", () => ({
  listCallingDefaultCatalogs: () => mocks.listCatalogs(),
  syncCallingDefaults: (options: unknown) => mocks.sync(options),
}));
vi.mock("@/lib/userPreferencesSync", () => ({
  queueUserPreferencePatch: (...args: unknown[]) => mocks.queuePatch(...args),
}));

vi.mock("@/lib/agentLabels", async (original) => ({
  ...(await original<typeof AgentLabels>()),
  useHarnessCapabilities: () => mocks.capabilities,
}));
vi.mock("@/hooks/useAgentRegistry", async (original) => ({
  ...(await original<Record<string, unknown>>()),
  useAgentRegistry: () => ({ items: [], installedServerAgents: [], legacyProducts: {} }),
}));
vi.mock("@/hooks/useCustomAgents", async () => {
  const { useState } = await import("react");
  return {
    useCustomAgents: () => {
      const [page, setPage] = useState(0);
      return {
        query: { data: { data: page === 0 ? mocks.installed : mocks.installedNext } },
        pager: {
          page,
          hasNext: page === 0 && mocks.installedNext.length > 0,
          onPrevious: () => setPage(0),
          onNext: () => setPage(1),
        },
      };
    },
  };
});
vi.mock("@/pages/settings/SettingsHarnessesSection", () => ({
  SettingsHarnessesSection: () => null,
}));
vi.mock("@/shell/AppShell", () => ({
  AppShell: () => <Outlet />,
}));
vi.mock("@/pages/SettingsPage", async () => {
  const { SettingsAgentsSection } = await import("@/pages/settings/SettingsAgentsSection");
  return { SettingsPage: SettingsAgentsSection };
});
vi.mock("@/pages/NotFoundPage", () => ({
  NotFoundPage: () => <p>Page not found</p>,
}));
vi.mock("@/pages/settings/SettingsCustomAgentsSection", () => ({
  SettingsCustomAgentsSection: () => {
    const location = useLocation();
    return location.state?.fromAgentsTable === true ? (
      <Link to="/settings/agents#agent-settings-table">Return to table</Link>
    ) : null;
  },
}));
vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({
    agent_install: true,
    agent_detail: true,
    features: { custom_agents_settings_ui: true },
  }),
}));
import { CallingDefaultsSection } from "./CallingDefaultsSection";

function render(ui: ReactElement, path = "/") {
  const result = renderComponent(<MemoryRouter initialEntries={[path]}>{ui}</MemoryRouter>);
  const options = screen.queryByText("New session options")?.closest("details");
  if (options) options.open = true;
  return result;
}

const DEFAULTS_KEY = "omnigent:calling-defaults";
const LAST_KEY = "omnigent:calling-last";

const HOST = {
  host_id: "host-a",
  name: "Alpha",
  owner: "alice",
  status: "online",
  configured_harnesses: {
    "codex-native": true,
    codex: true,
    "claude-native": true,
    "claude-sdk": true,
  },
};

const CATALOG_ROWS = [
  {
    host_id: "host-a",
    harness: "codex-native",
    models: [
      {
        id: "gpt-6-astra",
        displayName: "GPT-6-Astra",
        isDefault: true,
        serviceTiers: [
          { id: "priority", name: "Fast" },
          { id: "ultrafast", name: "Ultrafast" },
        ],
        supportedReasoningEfforts: [{ reasoningEffort: "low" }, { reasoningEffort: "medium" }],
      },
    ],
    fetched_at: 1_700_000_000,
    stale: false,
    error: null,
  },
];

function readMaster(): Record<string, Record<string, unknown>> {
  return JSON.parse(localStorage.getItem(DEFAULTS_KEY) ?? "{}");
}

async function pickOption(triggerTestId: string, optionName: string) {
  fireEvent.click(screen.getByTestId(triggerTestId));
  fireEvent.click(await screen.findByRole("option", { name: optionName }));
}

beforeEach(() => {
  localStorage.clear();
  mocks.hosts = [HOST];
  mocks.catalogRows = CATALOG_ROWS;
  mocks.listCatalogs.mockReset().mockResolvedValue(mocks.catalogRows);
  mocks.sync.mockReset().mockResolvedValue(mocks.catalogRows);
  mocks.queuePatch.mockReset();
  mocks.customAgents = [];
  mocks.installed = [];
  mocks.installedNext = [];
  mocks.capabilities = {};
});

afterEach(() => {
  cleanup();
  localStorage.clear();
});

describe("CallingDefaultsSection", () => {
  it("links separately installed agents to their own settings without adding harness defaults", async () => {
    mocks.installed = [
      {
        id: "agent-custom",
        name: "Release assistant",
        harness: "devin-acp",
        integration_mode: "native-server",
      },
    ];
    writeAgentBadgePreferences({
      version: 1,
      enabled: true,
      entries: { "agent-custom": { label: "RA", borderColor: "#16a34a", textColor: "theme" } },
    });
    render(<CallingDefaultsSection />);
    await waitFor(() => expect(mocks.listCatalogs).toHaveBeenCalled());
    expect(screen.getByRole("link", { name: "Release assistant details" })).toHaveAttribute(
      "href",
      "/settings/agents/custom/agent-custom",
    );
    const installed = within(screen.getByRole("link", { name: "Release assistant details" }));
    expect(installed.getByTestId("agent-name-badge")).toHaveTextContent("RA");
    expect(installed.queryByTestId("agent-name-mark")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Select cells" }));
    expect(screen.queryByRole("button", { name: /Select Release assistant/ })).toBeNull();
  });

  it("keeps the selected setting and page when returning from an installed agent", async () => {
    mocks.installedNext = [
      { id: "agent-next", name: "Release assistant", harness: "codex-native" },
    ];
    render(<App />, "/settings/agents");
    await waitFor(() => expect(mocks.listCatalogs).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("radio", { name: "Thinking" }));
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    fireEvent.click(screen.getByRole("link", { name: "Release assistant details" }));
    expect(screen.queryByRole("radio", { name: "Thinking" })).toBeNull();
    fireEvent.click(await screen.findByRole("link", { name: "Return to table" }));
    expect(screen.getByRole("radio", { name: "Thinking" })).toBeChecked();
    expect(screen.getByRole("link", { name: "Release assistant details" })).toBeVisible();
    expect(screen.getByText("Page 2")).toBeVisible();
  });

  it("keeps host aliases out and directs an ACP row to the host that reported it", async () => {
    mocks.hosts = [
      { ...HOST, configured_harnesses: { "claude-sdk": true, claude: true } },
      { ...HOST, host_id: "host-b", configured_harnesses: { "acp:example": true } },
    ];
    render(<CallingDefaultsSection />);
    await waitFor(() => expect(mocks.listCatalogs).toHaveBeenCalled());
    expect(screen.getAllByRole("link", { name: /Claude.*details/ })).toHaveLength(1);
    expect(screen.queryByRole("link", { name: /^claude details$/i })).toBeNull();
    expect(screen.getByRole("link", { name: "ACP · example details" })).toHaveAttribute(
      "href",
      "/settings/agents/acp%3Aexample?host=host-b",
    );
  });

  it("shows every configured ACP row and saves advertised thinking choices", async () => {
    mocks.hosts = [{ ...HOST, configured_harnesses: { "acp:neutral": true } }];
    mocks.listCatalogs.mockResolvedValue([
      {
        host_id: "host-a",
        harness: "acp:neutral",
        fetched_at: 1,
        stale: false,
        error: null,
        models: [{ id: "model-a", isDefault: true, efforts: ["quick", "deliberate"] }],
      },
    ]);
    render(<CallingDefaultsSection />);
    await waitFor(() => expect(mocks.listCatalogs).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("radio", { name: "Thinking" }));
    fireEvent.click(screen.getByTestId("layered-cell-acp:neutral-host-a"));
    fireEvent.click(screen.getByRole("combobox", { name: "Thinking" }));
    expect(await screen.findByRole("option", { name: /deliberate/i })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "High" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("option", { name: /deliberate/i }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(readMaster()["host-a"]["acp:neutral"]).toEqual({ effort: "deliberate" });
  });
  it("hides a thinking editor when the selected ACP model has no choices", async () => {
    localStorage.setItem(
      DEFAULTS_KEY,
      JSON.stringify({ "host-a": { "acp:neutral": { model: "model-b", effort: "high" } } }),
    );
    mocks.listCatalogs.mockResolvedValue([
      {
        host_id: "host-a",
        harness: "acp:neutral",
        fetched_at: 1,
        stale: false,
        error: null,
        models: [{ id: "model-b", isDefault: true, efforts: [] }],
      },
    ]);
    render(<CallingDefaultsSection />);
    await waitFor(() => expect(mocks.listCatalogs).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("radio", { name: "Thinking" }));
    expect(screen.queryByTestId("layered-cell-acp:neutral-host-a")).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Thinking" })).not.toBeInTheDocument();
  });
  it("offers library agents only when all member targets are ready", async () => {
    const member = {
      name: "Lead",
      description: null,
      harness: "claude-native",
      model: null,
      reasoning_effort: null,
      lead: true,
      host_id: "host-b",
    };
    const saved = {
      id: "ca_ready",
      name: "Review team",
      description: null,
      harness: null,
      model: null,
      members: [member],
      version: 1,
      created_at: 1,
      updated_at: null,
    };
    mocks.customAgents = [
      saved,
      {
        ...saved,
        id: "ca_missing",
        name: "Missing host",
        members: [{ ...member, host_id: "missing" }],
      },
    ];
    mocks.hosts = [HOST, { ...HOST, host_id: "host-b", name: "Beta" }];
    render(<CallingDefaultsSection />);
    fireEvent.click(screen.getByTestId("new-project-agent"));
    expect(await screen.findByRole("option", { name: "Review team" })).toBeVisible();
    expect(screen.queryByRole("option", { name: "Missing host" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("option", { name: "Review team" }));
    expect(mocks.queuePatch).toHaveBeenCalledWith("project_host_agents", { "host-a": "ca_ready" });
  });
  it("edits the new-project agent per host without changing harness defaults", async () => {
    mocks.hosts = [HOST, { ...HOST, host_id: "host-b", name: "Beta" }];
    localStorage.setItem(
      DEFAULTS_KEY,
      JSON.stringify({ "host-a": { "codex-native": { model: "saved" } } }),
    );
    render(<CallingDefaultsSection />);
    await pickOption("new-project-agent", "Claude Code");
    expect(mocks.queuePatch).toHaveBeenCalledWith("project_host_agents", {
      "host-a": "agent-claude",
    });
    expect(readMaster()["host-a"]["codex-native"]).toEqual({ model: "saved" });
    fireEvent.click(screen.getByTestId("calling-defaults-host-host-b"));
    expect(screen.getByTestId("new-project-agent")).toHaveTextContent("No template");
    await pickOption("new-project-agent", "Claude Code");
    expect(JSON.parse(localStorage.getItem("omnigent:project-host-agents")!)).toEqual({
      "host-a": "agent-claude",
      "host-b": "agent-claude",
    });
    await pickOption("new-project-agent", "No template");
    expect(mocks.queuePatch).toHaveBeenLastCalledWith("project_host_agents", { "host-b": null });
  });
  it("keeps a saved offline host editable without adding a harness row", async () => {
    mocks.hosts = [{ ...HOST, status: "offline" }];
    localStorage.setItem(DEFAULTS_KEY, JSON.stringify({ "host-a": { "codex-native": {} } }));
    render(<CallingDefaultsSection />);
    await waitFor(() => expect(mocks.listCatalogs).toHaveBeenCalled());
    expect(screen.getByTestId("calling-defaults-host-host-a")).toHaveTextContent("offline");
    fireEvent.click(screen.getByRole("radio", { name: "Model" }));
    fireEvent.click(screen.getByTestId("layered-cell-codex-native-host-a"));
    fireEvent.click(screen.getByRole("combobox", { name: "Model" }));
    fireEvent.click(await screen.findByRole("option", { name: "GPT-6-Astra" }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(readMaster()["host-a"]["codex-native"]).toEqual({ model: "gpt-6-astra" });
    expect(screen.queryByTestId("calling-defaults-add-row")).not.toBeInTheDocument();
  });
  it("keeps saved full access on the agent and lets a cleared SDK cell follow Native", async () => {
    mocks.hosts = [
      { ...HOST, configured_harnesses: { ...HOST.configured_harnesses, "pi-native": true } },
    ];
    localStorage.setItem(
      DEFAULTS_KEY,
      JSON.stringify({
        "host-a": {
          codex: { permission: "approve-for-me" },
          "codex-native": { permission: "read-only" },
          "pi-native": { permission: "full" },
        },
      }),
    );
    render(<CallingDefaultsSection />);
    await waitFor(() => expect(mocks.listCatalogs).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("radio", { name: "Permission" }));
    expect(screen.getByTestId("layered-cell-pi-native-host-a")).toHaveTextContent("Full access");
    fireEvent.click(screen.getByTestId("layered-cell-codex-host-a"));
    fireEvent.click(screen.getByRole("button", { name: "Use inherited" }));
    expect(screen.getByTestId("layered-cell-codex-host-a")).toHaveTextContent("Follows Codex");
    expect(readMaster()["host-a"].codex).toEqual({});
  });
  it.each(["claude-native", "claude-sdk"])(
    "edits %s speed through the same layered table",
    async (harness) => {
      localStorage.setItem(
        DEFAULTS_KEY,
        JSON.stringify({ "host-a": { [harness]: { speed: "fast" } } }),
      );
      render(<CallingDefaultsSection />);
      fireEvent.click(screen.getByRole("radio", { name: "Speed" }));
      expect(screen.getByTestId(`layered-cell-${harness}-host-a`)).toHaveTextContent("Fast");
      fireEvent.click(screen.getByTestId(`layered-cell-${harness}-host-a`));
      fireEvent.focus(screen.getByRole("button", { name: "About Claude fast mode" }));
      expect(await screen.findByRole("tooltip")).toHaveTextContent("usage credits");
      fireEvent.blur(screen.getByRole("button", { name: "About Claude fast mode" }));
      fireEvent.click(screen.getByRole("combobox", { name: "Speed" }));
      fireEvent.click(await screen.findByRole("option", { name: "Standard" }));
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
      expect(readMaster()["host-a"][harness]).toEqual({ speed: "standard" });
      fireEvent.click(screen.getByRole("button", { name: "Undo" }));
      expect(readMaster()["host-a"][harness]).toEqual({ speed: "fast" });
    },
  );

  it("starts carry-over off and writes enabled when toggled", async () => {
    render(<CallingDefaultsSection />);

    const toggle = screen.getByTestId("calling-defaults-carry-over");
    expect(toggle).toHaveAttribute("aria-checked", "false");

    fireEvent.click(toggle);
    await waitFor(() => expect(toggle).toHaveAttribute("aria-checked", "true"));
    expect(JSON.parse(localStorage.getItem(LAST_KEY)!)).toEqual({ enabled: true });
    expect(mocks.queuePatch).toHaveBeenCalledWith("calling_last", { enabled: true });
  });
});

it("adds a plugin declared setting and saves its mode", async () => {
  mocks.hosts = [{ ...HOST, configured_harnesses: { custom: true } }];
  mocks.capabilities = {
    custom: {
      calling_defaults: { model_options: false, inherits_from: null },
      session_modes: {
        permission: {
          handler: "plugin-review",
          options: [
            {
              value: "review",
              label: "Review",
              concept: "manual",
              contexts: ["default", "create"],
            },
          ],
        },
        speed: null,
      },
    },
  };
  render(<CallingDefaultsSection />);
  fireEvent.click(screen.getByRole("radio", { name: "Permission" }));
  fireEvent.click(screen.getByTestId("layered-cell-custom-host-a"));
  fireEvent.click(screen.getByRole("combobox", { name: "Permission" }));
  fireEvent.click(await screen.findByRole("option", { name: "Review" }));
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  expect(readMaster()["host-a"].custom).toEqual({ permission: "review" });
  fireEvent.click(screen.getByRole("radio", { name: "Model" }));
  expect(screen.queryByTestId("layered-cell-custom-host-a")).not.toBeInTheDocument();
});
