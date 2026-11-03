import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as AgentRegistryModule from "@/hooks/useAgentRegistry";
import type * as AgentLabelsModule from "@/lib/agentLabels";
import type * as HostModule from "@/lib/host";
import type * as IdentityModule from "@/lib/identity";
import type { RegistryItem } from "@/hooks/useAgentRegistry";
import { KEEP_WARM_DEFAULTS, type KeepWarmLayers } from "@/lib/keepWarmPreferences";
import { KeepWarmTable } from "./KeepWarmTable";

const mocks = vi.hoisted(() => ({
  items: [] as RegistryItem[],
  legacyProducts: {} as Record<string, RegistryItem>,
  sessions: new Set<string>(),
  fetch: vi.fn(),
  patch: vi.fn(),
  layers: null as KeepWarmLayers | null,
  already: true,
}));

const LAYERS: KeepWarmLayers = {
  corner: { main: true, maxSeconds: 14400 },
  hosts: { ["1".repeat(32)]: { main: false } },
  agents: { "harness:codex": { maxSeconds: 7200 } },
  cells: {},
  hostWins: {},
};

const ITEM: RegistryItem = {
  key: "harness:codex",
  label: "Codex",
  name: "codex",
  section: "sdk",
  harness: "codex",
  description: null,
  source: "builtin",
  deployed: true,
};

vi.mock("@/hooks/useAgentRegistry", async (importOriginal) => {
  const actual = await importOriginal<typeof AgentRegistryModule>();
  return {
    ...actual,
    useAgentRegistry: () => ({
      items: mocks.items,
      legacyProducts: mocks.legacyProducts,
      legacyLabels: {},
      isLoading: false,
      isError: false,
      retry: vi.fn(),
    }),
    useAgentsWithSessions: () => ({
      data: mocks.sessions,
      isPending: false,
      isError: false,
    }),
  };
});

vi.mock("@/hooks/useHosts", () => ({
  useHosts: () => ({
    data: [
      {
        host_id: "1".repeat(32),
        name: "Desktop Alpha",
        owner: "alice",
        status: "online",
        platform: "darwin",
      },
    ],
    isError: false,
  }),
}));

vi.mock("@/hooks/useKeepWarmPreferences", () => ({
  useKeepWarmPreferences: () => KEEP_WARM_DEFAULTS,
}));

vi.mock("@/lib/agentLabels", async (importOriginal) => ({
  ...(await importOriginal<typeof AgentLabelsModule>()),
  useHarnessCatalog: (
    selector: (catalog: {
      capabilitiesByHarness: Record<
        string,
        { integration_mode: string; keep_warm_family?: string }
      >;
    }) => unknown,
  ) =>
    selector({
      capabilitiesByHarness: {
        codex: { integration_mode: "cli-subprocess", keep_warm_family: "codex" },
      },
    }),
}));

vi.mock("@/lib/host", async (importOriginal) => ({
  ...(await importOriginal<typeof HostModule>()),
  getOmnigentServerIdentity: () => "server",
}));

vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  authenticatedFetch: mocks.fetch,
  getCurrentUserId: () => "alice",
  subscribeIdentity: () => () => {},
}));

vi.mock("@/lib/userPreferencesSync", () => ({
  queueUserPreferencePatch: vi.fn(),
  queueAcknowledgedUserPreferencePatch: mocks.patch,
}));

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function renderTable() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <KeepWarmTable />
    </QueryClientProvider>,
  );
}

async function openDialog(): Promise<HTMLElement> {
  await waitFor(() => {
    expect(
      document.querySelector('[data-slot="popover-content"][data-state="open"]'),
    ).not.toBeNull();
  });
  return document.querySelector('[data-slot="popover-content"][data-state="open"]') as HTMLElement;
}

beforeEach(() => {
  localStorage.clear();
  mocks.items = [ITEM];
  mocks.legacyProducts = {};
  mocks.sessions = new Set(["harness:codex"]);
  mocks.layers = structuredClone(LAYERS);
  mocks.already = true;
  mocks.patch.mockReset();
  mocks.patch.mockResolvedValue(undefined);
  mocks.fetch.mockReset();
  mocks.fetch.mockImplementation(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/keep_warm/convert")) {
      return jsonResponse({ layers: mocks.layers, already: mocks.already });
    }
    return jsonResponse({ version: 1, settings: { keep_warm: { layers: mocks.layers } } });
  });
});

afterEach(cleanup);

const HOST_ID = "1".repeat(32);

describe("KeepWarmTable keyboard path", () => {
  it("opens a cell editor with Enter and keeps focus inside it", async () => {
    const user = userEvent.setup();
    renderTable();
    const corner = await screen.findByTestId("layered-corner");
    corner.focus();
    expect(corner).toHaveFocus();

    await user.keyboard("{Enter}");
    const dialog = await openDialog();
    expect(dialog).toBeVisible();
    expect(within(dialog).getByText("All agents, all hosts")).toBeVisible();
    await waitFor(() => {
      expect(dialog === document.activeElement || dialog.contains(document.activeElement)).toBe(
        true,
      );
    });

    expect(screen.queryByText("Keep non-archived main sessions warm.")).toBeNull();

    await user.tab();
    expect(document.activeElement).toHaveAccessibleName(/Main sessions/);
    await user.tab();
    const focused = document.activeElement as HTMLElement;
    expect(dialog.contains(focused)).toBe(true);
    expect(focused).toHaveAttribute("role", "radio");
    expect(focused).toHaveTextContent("On");

    await user.keyboard("{Escape}");
    await waitFor(() =>
      expect(document.querySelector('[data-slot="popover-content"][data-state="open"]')).toBeNull(),
    );
    expect(corner).toHaveFocus();
  });

  it("opens a body cell from the keyboard and shows what it inherits", async () => {
    const user = userEvent.setup();
    renderTable();
    const cell = await screen.findByTestId(`layered-cell-harness:codex-${HOST_ID}`);
    cell.focus();
    expect(cell).toHaveFocus();
    await user.keyboard("{Enter}");
    const dialog = await openDialog();
    expect(within(dialog).getByText("Codex on Desktop Alpha")).toBeVisible();
    expect(
      within(dialog).getByText("This cell, then Codex, then Desktop Alpha, then All."),
    ).toBeVisible();
  });
});

describe("KeepWarmTable review regressions", () => {
  it("shows a custom agent's inherited harness value", async () => {
    mocks.items = [{ ...ITEM, key: "custom-agent", label: "Custom Codex" }];
    mocks.sessions = new Set(["custom-agent"]);
    mocks.layers = {
      corner: { main: false, child: false },
      hosts: { [HOST_ID]: { main: false, child: false } },
      agents: { "harness:codex": { main: true, child: true } },
      cells: {},
      hostWins: {},
    };
    renderTable();
    const cell = await screen.findByTestId(`layered-cell-custom-agent-${HOST_ID}`);
    expect(cell).toHaveTextContent("On");
  });

  it("shows a retained product-id value on its visible harness row", async () => {
    const product = "product-codex";
    mocks.legacyProducts = { [product]: { ...ITEM, key: product } };
    mocks.sessions = new Set([product]);
    mocks.layers = {
      corner: { main: false },
      hosts: {},
      agents: { [product]: { main: true }, "harness:codex": { main: false } },
      cells: {},
      hostWins: {},
    };
    renderTable();
    const cell = await screen.findByTestId(`layered-cell-harness:codex-${HOST_ID}`);
    expect(cell).toHaveTextContent("Main only");
  });

  it("shows a bound-agent override on its visible template row", async () => {
    mocks.items = [{ ...ITEM, key: "ca_library", label: "Library Codex", source: "library" }];
    mocks.sessions = Object.assign(new Set(["ca_library", "bound-agent"]), {
      templateByAgentId: { "bound-agent": "ca_library" },
    });
    mocks.layers = {
      corner: { main: false },
      hosts: {},
      agents: { "bound-agent": { main: true }, "harness:codex": { main: false } },
      cells: {},
      hostWins: {},
    };
    renderTable();
    const cell = await screen.findByTestId(`layered-cell-ca_library-${HOST_ID}`);
    expect(cell).toHaveTextContent("Main only");
  });

  it("converts and saves a first edit in one namespace write", async () => {
    mocks.already = false;
    mocks.patch.mockRejectedValue(
      Object.assign(new Error("validation refused first edit"), { status: 422 }),
    );
    const user = userEvent.setup();
    renderTable();
    await user.click(await screen.findByTestId("layered-corner"));
    const dialog = await openDialog();
    await user.click(
      within(within(dialog).getByRole("radiogroup", { name: "Main sessions" })).getByRole("radio", {
        name: "Off",
      }),
    );
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await screen.findByText("Save wasn't confirmed: validation refused first edit");
    const conversions = mocks.fetch.mock.calls.filter(([input]) =>
      String(input).includes("/keep_warm/convert"),
    );
    expect(conversions).toHaveLength(1);
    expect(JSON.parse(String(conversions[0][1]?.body))).toEqual({
      dry_run: true,
      target: "layers",
    });
    expect(mocks.patch).toHaveBeenCalledTimes(1);
    expect(mocks.patch.mock.calls[0][1].layers.corner.main).toBe(false);
  });

  it("clears Cold after to the platform rule instead of its parent", async () => {
    mocks.layers = {
      corner: { coldAfterSeconds: 3600 },
      hosts: {},
      agents: {},
      cells: { [HOST_ID]: { "harness:codex": { coldAfterSeconds: 7200 } } },
      hostWins: {},
    };
    const user = userEvent.setup();
    renderTable();
    await user.click(await screen.findByTestId(`layered-cell-harness:codex-${HOST_ID}`));
    const dialog = await openDialog();
    await user.clear(within(dialog).getByRole("spinbutton", { name: "Cold after in minutes" }));
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(mocks.patch).toHaveBeenCalledTimes(1));
    expect(
      mocks.patch.mock.calls[0][1].layers.cells[HOST_ID]["harness:codex"].coldAfterSeconds,
    ).toBeNull();
  });

  it("closes the old draft after clearing a level", async () => {
    mocks.layers = {
      corner: {},
      hosts: {},
      agents: {},
      cells: { [HOST_ID]: { "harness:codex": { coldAfterSeconds: 7200 } } },
      hostWins: {},
    };
    const user = userEvent.setup();
    renderTable();
    await user.click(await screen.findByTestId(`layered-cell-harness:codex-${HOST_ID}`));
    const dialog = await openDialog();
    await user.click(within(dialog).getByRole("button", { name: "Clear this level" }));
    await waitFor(() => expect(mocks.patch).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(document.querySelector('[data-slot="popover-content"][data-state="open"]')).toBeNull(),
    );
  });

  it("shows clash fields, values and winner as readable rows", async () => {
    mocks.layers = {
      corner: {},
      hosts: { [HOST_ID]: { main: false, maxSeconds: 7200 } },
      agents: { "harness:codex": { main: true, maxSeconds: 10800 } },
      cells: {},
      hostWins: {},
    };
    renderTable();
    const cell = await screen.findByTestId(`layered-cell-harness:codex-${HOST_ID}`);
    const warning = within(cell.closest("td")!).getByLabelText(/^Warning:/);
    const title = warning.getAttribute("aria-label") ?? "";
    expect(title).toContain("Main sessions\nCodex: On\nDesktop Alpha: Off\nWinner: Codex");
    expect(title).toContain("Up to\nCodex: up to 3 h\nDesktop Alpha: up to 2 h\nWinner: Codex");
    expect(title).not.toContain("maxSeconds");
  });

  it("states each level's value and dots only what that level sets", async () => {
    mocks.layers = {
      corner: { main: true, child: true, maxSeconds: 14400 },
      hosts: { [HOST_ID]: { maxSeconds: 7200 } },
      agents: { "harness:codex": { child: false } },
      cells: {},
      hostWins: { [HOST_ID]: true },
    };
    renderTable();
    const corner = await screen.findByTestId("layered-corner");
    expect(corner).toHaveTextContent("All agents, all hostsOnup to 4 h");
    expect(corner.querySelector(".bg-pink-500")).toBeNull();
    const column = screen.getByTestId(`layered-col-${HOST_ID}`);
    expect(column).toHaveTextContent("Host wins");
    expect(column).toHaveTextContent("up to 2 h");
    expect(column.querySelectorAll(".bg-pink-500")).toHaveLength(2);
    const row = screen.getByTestId("layered-row-harness:codex");
    expect(row).toHaveTextContent("Main only");
    expect(row.querySelectorAll(".bg-pink-500")).toHaveLength(1);
    expect(screen.getByText("Differs from the level above")).toBeVisible();
    expect(screen.getByText("No host: sessions not tied to a host")).toBeVisible();
  });
});
