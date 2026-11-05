import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { CustomAgent } from "@/lib/customAgentsApi";
import type { HarnessCapability } from "@/lib/agentLabels";
import type * as AgentLabels from "@/lib/agentLabels";
import type * as CustomAgentsApi from "@/lib/customAgentsApi";

const mocks = vi.hoisted(() => ({
  hosts: [] as Record<string, unknown>[],
  capabilities: {} as Record<string, HarnessCapability>,
  catalogRows: [] as Record<string, unknown>[],
  listCatalogs: vi.fn(),
  sync: vi.fn(),
  queuePatch: vi.fn(),
  customAgents: [] as CustomAgent[],
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
import { CallingDefaultsSection } from "./CallingDefaultsSection";

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
  mocks.capabilities = {};
});

afterEach(() => {
  cleanup();
  localStorage.clear();
});

describe("CallingDefaultsSection", () => {
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
    expect(screen.getByTestId("layered-cell-pi-native-host-a")).toHaveTextContent("Full access");
    fireEvent.click(screen.getByTestId("layered-cell-codex-host-a"));
    fireEvent.click(screen.getByRole("button", { name: "Use inherited" }));
    expect(screen.getByTestId("layered-cell-codex-host-a")).toHaveTextContent("Follows Codex");
    expect(readMaster()["host-a"].codex).toEqual({});
  });
  it.each(["claude-native", "claude-sdk"])(
    "reads and saves %s speed with focused help",
    async (harness) => {
      localStorage.setItem(
        DEFAULTS_KEY,
        JSON.stringify({ "host-a": { [harness]: { speed: "fast" } } }),
      );
      render(<CallingDefaultsSection />);
      expect(screen.getByTestId(`calling-defaults-speed-${harness}`)).toHaveTextContent("Fast");
      expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
      fireEvent.focus(
        screen.getAllByRole("button", { name: "About Claude fast mode" })[
          harness === "claude-native" ? 0 : 1
        ],
      );
      expect(await screen.findByRole("tooltip")).toHaveTextContent("usage credits");
      fireEvent.blur(
        screen.getAllByRole("button", { name: "About Claude fast mode" })[
          harness === "claude-native" ? 0 : 1
        ],
      );
      await pickOption(`calling-defaults-speed-${harness}`, "Standard");
      expect(readMaster()["host-a"][harness]).toEqual({ speed: "standard" });
      await pickOption(`calling-defaults-speed-${harness}`, "Default");
      expect(readMaster()["host-a"][harness]).toEqual({});
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
  fireEvent.click(screen.getByTestId("layered-cell-custom-host-a"));
  fireEvent.click(screen.getByRole("combobox", { name: "Permission" }));
  fireEvent.click(await screen.findByRole("option", { name: "Review" }));
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  expect(readMaster()["host-a"].custom).toEqual({ permission: "review" });
  fireEvent.click(screen.getByRole("radio", { name: "Model" }));
  expect(screen.queryByTestId("layered-cell-custom-host-a")).not.toBeInTheDocument();
});
