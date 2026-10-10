import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  hosts: [] as Record<string, unknown>[],
  catalogRows: [] as Record<string, unknown>[],
  listCatalogs: vi.fn(),
  sync: vi.fn(),
  queuePatch: vi.fn(),
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
});

afterEach(() => {
  cleanup();
  localStorage.clear();
});

describe("CallingDefaultsSection", () => {
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
