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
  sessions: new Set<string>(),
  fetch: vi.fn(),
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
      legacyProducts: {},
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
  queueAcknowledgedUserPreferencePatch: vi.fn().mockResolvedValue(undefined),
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
  mocks.sessions = new Set(["harness:codex"]);
  mocks.fetch.mockReset();
  mocks.fetch.mockImplementation(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/keep_warm/convert")) {
      return jsonResponse({ layers: LAYERS, already: true });
    }
    return jsonResponse({ version: 1, settings: { keep_warm: { layers: LAYERS } } });
  });
});

afterEach(cleanup);

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
    const cell = await screen.findByTestId(`layered-cell-harness:codex-${"1".repeat(32)}`);
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
