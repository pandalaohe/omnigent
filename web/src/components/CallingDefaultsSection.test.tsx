import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { CustomAgent } from "@/lib/customAgentsApi";
import type * as CustomAgentsApi from "@/lib/customAgentsApi";

const mocks = vi.hoisted(() => ({
  hosts: [] as Record<string, unknown>[],
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
});

afterEach(() => {
  cleanup();
  localStorage.clear();
});

describe("CallingDefaultsSection", () => {
  it("adds a configured ACP row using only its advertised effort choices", async () => {
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
    fireEvent.click(screen.getByTestId("calling-defaults-add-row"));
    await pickOption("calling-defaults-add-harness", "ACP · neutral");
    expect(await screen.findByTestId("calling-defaults-row-acp:neutral")).toBeInTheDocument();
    fireEvent.click(await screen.findByTestId("calling-defaults-effort-acp:neutral"));
    expect(screen.getByRole("option", { name: /deliberate/i })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "High" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("option", { name: /deliberate/i }));
    expect(readMaster()["host-a"]["acp:neutral"]).toEqual({ effort: "deliberate" });
  });

  it("hides the ACP effort control when the selected model advertises none", async () => {
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
    expect(
      await screen.findByTestId("calling-defaults-effort-unavailable-acp:neutral"),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("calling-defaults-effort-acp:neutral")).not.toBeInTheDocument();
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
  it("keeps a saved offline host editable", async () => {
    mocks.hosts = [{ ...HOST, status: "offline" }];
    localStorage.setItem(DEFAULTS_KEY, JSON.stringify({ "host-a": { "codex-native": {} } }));
    render(<CallingDefaultsSection />);

    expect(screen.getByTestId("calling-defaults-host-host-a")).toHaveTextContent("offline");
    expect(screen.getByTestId("calling-defaults-row-codex-native")).toBeInTheDocument();
    await pickOption("calling-defaults-model-codex-native", "GPT-6-Astra");
    await waitFor(() =>
      expect(readMaster()["host-a"]["codex-native"]).toEqual({ model: "gpt-6-astra" }),
    );
  });

  it("adds, edits, and deletes a harness row", async () => {
    render(<CallingDefaultsSection />);

    expect(await screen.findByTestId("calling-defaults-follows-codex")).toBeInTheDocument();
    expect(screen.getByTestId("calling-defaults-follows-claude-sdk")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("calling-defaults-add-row"));
    fireEvent.click(screen.getByTestId("calling-defaults-add-harness"));
    fireEvent.click(await screen.findByRole("option", { name: "Codex" }));
    await waitFor(() => expect(readMaster()).toEqual({ "host-a": { "codex-native": {} } }));

    await pickOption("calling-defaults-model-codex-native", "GPT-6-Astra");
    await waitFor(() =>
      expect(readMaster()).toEqual({
        "host-a": { "codex-native": { model: "gpt-6-astra" } },
      }),
    );

    await pickOption("calling-defaults-effort-codex-native", "Medium");
    await waitFor(() =>
      expect(readMaster()).toEqual({
        "host-a": { "codex-native": { model: "gpt-6-astra", effort: "medium" } },
      }),
    );

    fireEvent.click(screen.getByTestId("calling-defaults-delete-codex-native"));
    await waitFor(() => expect(readMaster()).toEqual({ "host-a": {} }));
    expect(screen.queryByTestId("calling-defaults-row-codex-native")).not.toBeInTheDocument();
    expect(screen.getByTestId("calling-defaults-follows-codex")).toBeInTheDocument();
  });

  it("edits and clears supported session modes while hiding unsupported controls", async () => {
    localStorage.setItem(
      DEFAULTS_KEY,
      JSON.stringify({ "host-a": { "codex-native": {}, "claude-native": {}, "pi-native": {} } }),
    );
    render(<CallingDefaultsSection />);
    await pickOption("calling-defaults-speed-codex-native", "Fast");
    await pickOption("calling-defaults-permission-codex-native", "Approve for me");
    expect(readMaster()["host-a"]["codex-native"]).toEqual({
      speed: "fast",
      permission: "approve-for-me",
    });
    await pickOption("calling-defaults-speed-codex-native", "Default");
    await pickOption("calling-defaults-permission-codex-native", "Default");
    expect(readMaster()["host-a"]["codex-native"]).toEqual({});
    expect(screen.getByTestId("calling-defaults-speed-claude-native")).toHaveTextContent("Default");
    await pickOption("calling-defaults-permission-claude-native", "Accept edits");
    expect(readMaster()["host-a"]["claude-native"]).toEqual({ permission: "acceptEdits" });
    expect(screen.queryByTestId("calling-defaults-permission-pi-native")).not.toBeInTheDocument();
  });

  it("returns an SDK row to Follows after deleting its own entry", async () => {
    render(<CallingDefaultsSection />);

    expect(await screen.findByTestId("calling-defaults-follows-codex")).toHaveTextContent(
      "Follows Codex",
    );

    fireEvent.click(screen.getByTestId("calling-defaults-set-separately-codex"));
    await waitFor(() => expect(readMaster()).toEqual({ "host-a": { codex: {} } }));
    expect(screen.queryByTestId("calling-defaults-follows-codex")).not.toBeInTheDocument();
    expect(screen.getByTestId("calling-defaults-row-codex")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("calling-defaults-delete-codex"));
    await waitFor(() =>
      expect(screen.queryByTestId("calling-defaults-row-codex")).not.toBeInTheDocument(),
    );
    expect(screen.getByTestId("calling-defaults-follows-codex")).toBeInTheDocument();
    expect(readMaster()).toEqual({ "host-a": {} });
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
      fireEvent.focus(screen.getByRole("button", { name: "About Claude fast mode" }));
      expect(await screen.findByRole("tooltip")).toHaveTextContent("usage credits");
      fireEvent.blur(screen.getByRole("button", { name: "About Claude fast mode" }));
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
