// Findings from the slice's first review, each as its own case: the copied
// Connect command must submit, arrangement must not navigate on Enter, Compact
// must not lose arrangement, phone targets must reach 44 px, and the colour
// line's two handles must be independently operable.

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ServerInfo } from "@/lib/capabilities";
import { readSessionCollabPreferences } from "@/lib/sessionCollabPreferences";
import { DEFAULT_USAGE_CONTEXT_PREFERENCES } from "@/lib/usageContextPreferences";
import type * as PrefsModule from "@/lib/usageContextPreferences";
import type * as UsageBoardDataModule from "@/pages/usageBoard/usageBoardData";

import { UsageBoardRoute } from "../usageBoard/UsageBoardRoute";
import { UsageBoardSettings } from "../usageBoard/UsageBoardSettings";
import { sampleTotals } from "./usageActivityFixtures";
import { usageBoardData } from "../usageBoard/usageBoardFixtures";
import {
  buildConnectCommand,
  CONNECT_SAMPLE_RESET_SECONDS,
  connectCommandPayload,
} from "../usageBoard/usageBoardConnect";
import { boardBands, moveTile, seedBands } from "../usageBoard/usageBoardLayout";

const mocks = vi.hoisted(() => ({
  extensions: { current: [] as unknown[] },
  patched: { current: [] as Record<string, unknown>[] },
  overview: { current: undefined as unknown },
  info: { current: undefined as unknown },
  board: {
    current: undefined as unknown,
  },
  collab: { current: undefined as unknown },
}));

vi.mock("@/extensions/ExtensionProvider", () => ({
  useExtensions: () => mocks.extensions.current,
}));

vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => mocks.info.current,
  useCachedServerInfo: () => mocks.info.current,
}));

vi.mock("@/pages/usageBoard/usageBoardData", async (importOriginal) => ({
  ...(await importOriginal<typeof UsageBoardDataModule>()),
  useUsageOverview: () => mocks.overview.current,
}));

vi.mock("@/hooks/useUsageContextPreferences", () => ({
  useUsageContextPreferences: () => ({
    ...DEFAULT_USAGE_CONTEXT_PREFERENCES,
    board: mocks.board.current,
  }),
}));

// The mocked preference hook cannot re-render, so the arrangement case reads
// the write instead: the same call the real hook would fan out.
vi.mock("@/lib/usageContextPreferences", async (importOriginal) => {
  const actual = await importOriginal<typeof PrefsModule>();
  return {
    ...actual,
    patchUsageBoardPreferences: (patch: Record<string, unknown>) => {
      mocks.patched.current.push(patch);
    },
  };
});

vi.mock("@/hooks/useSessionCollabPreferences", () => ({
  useSessionCollabPreferences: () => mocks.collab.current,
}));

const SERVER_INFO = { usage_board_enabled: true } as unknown as ServerInfo;
const LOADED = { isPending: false, error: null, refetch: vi.fn(), data: { accounts: [] } };
const clipboardDescriptor = Object.getOwnPropertyDescriptor(Navigator.prototype, "clipboard");
const execCommandDescriptor = Object.getOwnPropertyDescriptor(Document.prototype, "execCommand");

function renderBoard(path = "/usage/board") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/frame/usage/board" element={<UsageBoardRoute view="board" />} />
          <Route
            path="/frame/usage/board/:accountId"
            element={<UsageBoardRoute view="account" />}
          />
          <Route path="/usage/board" element={<UsageBoardRoute view="board" />} />
          <Route path="/usage/board/:accountId" element={<UsageBoardRoute view="account" />} />
          <Route
            path="/usage/board/:accountId/settings"
            element={<UsageBoardRoute view="account_settings" />}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mocks.extensions.current = [];
  mocks.info.current = SERVER_INFO;
  mocks.overview.current = { ...LOADED, data: usageBoardData() };
  mocks.board.current = {
    enabled: true,
    density: "std",
    bands: [],
    windows: {},
    renewals: {},
    hidden: [],
  };
  mocks.collab.current = {
    quotaTightBelowPct: 50,
    quotaLowBelowPct: 20,
    quotaFloorPct: null,
    quotaPeriodDefault: null,
    quotaPeriods: {},
  };
});

afterEach(() => {
  cleanup();
  if (clipboardDescriptor) {
    Object.defineProperty(Navigator.prototype, "clipboard", clipboardDescriptor);
  } else {
    delete (Navigator.prototype as { clipboard?: unknown }).clipboard;
  }
  if (execCommandDescriptor) {
    Object.defineProperty(Document.prototype, "execCommand", execCommandDescriptor);
  } else {
    delete (Document.prototype as { execCommand?: unknown }).execCommand;
  }
});

describe("Connect command", () => {
  // The route's own contract: `observed_at` is a strict number, `resets_at`
  // must not be in the past, and a window needs percent_used or used_value.
  const NOW = 1_793_000_000;

  it("posts a numeric observation time and a future reset", () => {
    const payload = connectCommandPayload(buildConnectCommand("https://omnigent.example"), NOW) as {
      observed_at: number;
      windows: { id: string; percent_used: number; window_minutes: number; resets_at: number }[];
    };
    expect(typeof payload.observed_at).toBe("number");
    expect(payload.observed_at).toBe(NOW);
    const [window] = payload.windows;
    expect(window.id).toBe("month");
    expect(typeof window.percent_used).toBe("number");
    expect(typeof window.window_minutes).toBe("number");
    expect(window.resets_at).toBe(NOW + CONNECT_SAMPLE_RESET_SECONDS);
    expect(window.resets_at).toBeGreaterThan(NOW);
  });

  it("keeps the body double-quoted so the shell substitutes, and names the route", () => {
    const command = buildConnectCommand("https://omnigent.example");
    expect(command).toContain('-d "');
    expect(command).not.toContain("-d '");
    expect(command).toContain("https://omnigent.example/v1/me/usage/accounts/my-gateway");
  });

  it("shows exactly that command in the Connect sheet", async () => {
    render(
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <MemoryRouter>
          <UsageBoardSettings />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    fireEvent.click(await screen.findByRole("button", { name: /connect/i }));
    const shown = await screen.findByText(/\/v1\/me\/usage\/accounts\/my-gateway/);
    expect(shown.textContent).toBe(buildConnectCommand(window.location.origin));
  });

  it("copies through the fallback and confirms without the Clipboard API", async () => {
    const copiedText = vi.fn();
    Object.defineProperty(Navigator.prototype, "clipboard", {
      configurable: true,
      value: undefined,
    });
    Object.defineProperty(Document.prototype, "execCommand", {
      configurable: true,
      value: vi.fn((command: string) => {
        if (command !== "copy") return false;
        const event = new Event("copy", { bubbles: true, cancelable: true }) as ClipboardEvent;
        Object.defineProperty(event, "clipboardData", {
          value: { setData: copiedText },
        });
        document.dispatchEvent(event);
        return event.defaultPrevented;
      }),
    });
    render(
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <MemoryRouter>
          <UsageBoardSettings />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    fireEvent.click(await screen.findByRole("button", { name: /connect/i }));
    fireEvent.click(screen.getByRole("button", { name: "Copy" }));

    await waitFor(() => {
      expect(copiedText).toHaveBeenCalledWith(
        "text/plain",
        buildConnectCommand(window.location.origin),
      );
    });
    expect(document.execCommand).toHaveBeenCalledWith("copy");
    expect(await screen.findByRole("button", { name: "Copied" })).toBeTruthy();
  });

  it("keeps Connect, Price source and the Issues link in Settings when the board switch is off", async () => {
    mocks.board.current = { ...(mocks.board.current as object), enabled: false };
    render(
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <MemoryRouter>
          <UsageBoardSettings />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(screen.getByRole("link", { name: /Issues/ }).getAttribute("href")).toBe(
      "/usage/board/issues",
    );
    expect(screen.getByText("Price source")).toBeTruthy();
    expect(await screen.findByRole("button", { name: /connect/i })).toBeTruthy();
    expect(screen.queryByText("Ring colors")).toBeNull();
    expect(screen.queryByText("On board")).toBeNull();
  });
});

describe("arrangement", () => {
  it("does not open the account when Enter activates a move button", async () => {
    renderBoard();
    await screen.findAllByTestId("usage-tile");
    fireEvent.click(screen.getByRole("button", { name: "Arrange groups and order" }));
    const forward = screen.getByRole("button", { name: "Move Codex forward" });
    fireEvent.keyDown(forward, { key: "Enter" });
    // The move happened and the board is still the board.
    expect(screen.queryByRole("heading", { level: 1 })?.textContent).toBe("Usage board");
    expect(screen.getAllByTestId("usage-tile").length).toBe(5);
  });

  it("switches to rings when entering Arrange from Compact", async () => {
    mocks.board.current = { ...(mocks.board.current as object), density: "mini" };
    renderBoard();
    const compact = (await screen.findAllByTestId("usage-tile"))[0];
    expect(compact.className).toContain("min-h-[54px]");
    fireEvent.click(screen.getByRole("button", { name: "Arrange groups and order" }));
    // A compact row has no room for handles, so entering Arrange writes the ring
    // view; the mocked hook cannot re-render, so the write is what is asserted.
    expect(mocks.patched.current).toContainEqual(expect.objectContaining({ density: "std" }));
  });

  it("gives arrangement controls and the reading tip 44 px targets on phones", async () => {
    renderBoard();
    await screen.findAllByTestId("usage-tile");
    const tip = screen.getByRole("button", { name: /Last read 3 h ago/ });
    expect(tip.className).toContain("min-h-11");
    fireEvent.click(screen.getByRole("button", { name: "Arrange groups and order" }));
    const forward = screen.getByRole("button", { name: "Move Codex forward" });
    expect(forward.className).toContain("min-h-11");
    expect(forward.className).toContain("min-w-11");
    // A desktop pointer keeps the compact 28 px button.
    expect(forward.className).toContain("md:min-h-7");
  });

  it("materializes an account that arrived after the arrangement was made", () => {
    const board = {
      enabled: true,
      density: "std" as const,
      bands: [{ id: "paid", name: "Paid", accounts: ["codex"], open: true }],
      windows: {},
      renewals: {},
      hidden: [],
    };
    const accounts = usageBoardData().accounts;
    // It shows through the leftover rule …
    const rendered = boardBands(board, accounts);
    expect(rendered[0].accounts.map((account) => account.id)).toContain("grok");
    // … and seeding writes it into membership so a move can find it.
    const seeded = seedBands(board, accounts);
    expect(seeded[0].accounts).toEqual(expect.arrayContaining(["codex", "grok"]));
    // With membership materialized, a move finds the tile's band.
    const before = seeded[0].accounts.indexOf("grok");
    const moved = moveTile(seeded, accounts, board, "grok", -1);
    expect(moved[0].accounts.indexOf("grok")).toBe(before - 1);
  });
});

describe("detail tips on a phone", () => {
  it("opens the window card by tapping its ring", async () => {
    renderBoard("/usage/board/codex");
    const ring = (await screen.findAllByTestId("usage-ring"))[0];
    fireEvent.click(ring);
    expect(await screen.findByTestId("usage-tip-card")).toBeTruthy();
  });

  it("keeps a board tile tap going to the detail page instead", async () => {
    renderBoard();
    const ring = (await screen.findAllByTestId("usage-ring"))[0];
    fireEvent.click(ring);
    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Codex" }).textContent).toBe("Codex");
    });
    expect(screen.queryByTestId("usage-tip-card")).toBeNull();
  });
});

describe("colour line", () => {
  it("keeps both handles pointer-operable and independently labelled", async () => {
    render(
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <MemoryRouter>
          <UsageBoardSettings />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    const amber = await screen.findByLabelText(/Amber below/);
    const red = screen.getByLabelText(/Red below a pace/);
    expect(amber).not.toBe(red);
    // Neither range is hidden under the other, so a pointer reaches each.
    expect(amber.className).not.toContain("opacity-0");
    expect(red.className).not.toContain("opacity-0");
    fireEvent.change(amber, { target: { value: "70" } });
    expect(readCollab().quotaTightBelowPct).toBe(70);
    fireEvent.change(red, { target: { value: "30" } });
    expect(readCollab().quotaLowBelowPct).toBe(30);
    // The floor field shares the block and stays reachable by keyboard.
    const floor = screen.getByLabelText(/Red below, percent left/);
    fireEvent.change(floor, { target: { value: "10" } });
    expect(readCollab().quotaFloorPct).toBe(10);
    within(amber.closest("div") as HTMLElement).getByText(/Amber/);
  });

  it.each([
    { handle: "amber", value: "15", tight: 15, low: 10 },
    { handle: "red", value: "60", tight: 65, low: 60 },
  ])(
    "keeps a valid pair when the $handle handle crosses the other",
    async ({ handle, value, tight, low }) => {
      render(
        <QueryClientProvider
          client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
        >
          <MemoryRouter>
            <UsageBoardSettings />
          </MemoryRouter>
        </QueryClientProvider>,
      );
      const amber = await screen.findByLabelText(/Amber below/);
      const red = screen.getByLabelText(/Red below a pace/);
      fireEvent.change(handle === "amber" ? amber : red, { target: { value } });

      const preferences = readSessionCollabPreferences();
      expect(preferences).toMatchObject({
        quotaTightBelowPct: tight,
        quotaLowBelowPct: low,
      });
      expect(preferences.quotaLowBelowPct).toBeGreaterThan(0);
      expect(preferences.quotaLowBelowPct).toBeLessThan(preferences.quotaTightBelowPct);
    },
  );
});

function readCollab() {
  return JSON.parse(window.localStorage.getItem("omnigent:session-collab") ?? "{}") as {
    quotaTightBelowPct?: number;
    quotaLowBelowPct?: number;
    quotaFloorPct?: number | null;
  };
}

it("keeps imperative account navigation and back/settings links inside a frame", async () => {
  window.history.replaceState({}, "", "/frame/usage/board");
  try {
    renderBoard("/frame/usage/board");
    fireEvent.click((await screen.findAllByTestId("usage-ring"))[0]);
    await waitFor(() =>
      expect(screen.getByRole("heading", { name: "Codex" }).textContent).toBe("Codex"),
    );
    expect(screen.getByRole("link", { name: "Account settings" }).getAttribute("href")).toBe(
      "/frame/usage/board/codex/settings",
    );
    const back = screen.getByRole("link", { name: "Usage board" });
    expect(back.getAttribute("href")).toBe("/frame/usage/board");
  } finally {
    window.history.replaceState({}, "", "/");
  }
});

describe("Account detail composition", () => {
  it("prioritizes the requested quota group and retains support workflows", () => {
    const data = usageBoardData();
    const account = data.accounts[0];
    account.windows = [
      { ...account.windows[0], id: "first", group: "Pool A" },
      { ...account.windows[0], id: "second", group: "Pool B" },
    ];
    mocks.overview.current = { ...LOADED, data };
    const view = renderBoard(`/usage/board/${encodeURIComponent(account.id)}?focus=Pool%20B`);
    expect(view.container.querySelector("[data-pool]")?.getAttribute("data-pool")).toBe("Pool B");
    expect(screen.getByRole("group", { name: "Cost time range" })).toBeTruthy();
    expect(screen.getByText("Hosts", { exact: true }).closest("details")?.open).toBe(false);
    expect(screen.getByText("Records", { exact: true })).toBeTruthy();
    expect(screen.getAllByText(/Model pools · Not available yet/).length).toBeGreaterThan(0);
    expect(screen.getByRole("link", { name: "Account settings" })).toHaveProperty(
      "href",
      expect.stringContaining("/settings"),
    );
  });

  it("keeps failed overview reads visible instead of claiming the account was removed", () => {
    mocks.overview.current = { ...LOADED, error: new Error("Source unavailable"), data: undefined };
    renderBoard("/usage/board/example");
    expect(screen.getByText("Usage could not be read")).toBeTruthy();
    expect(screen.queryByText("This account is not on the board any more.")).toBeNull();
  });
});

describe("Mounted model pools page", () => {
  it("uses the selected installed page and withdraws its link when unavailable", () => {
    mocks.extensions.current = [
      {
        id: "example.pools",
        display_name: "Pool manager",
        pages: [{ id: "example.pools.main", title: "Model pools", route: "main" }],
      },
    ];
    mocks.board.current = {
      ...(mocks.board.current as object),
      modelPoolsPage: "/extensions/example.pools/main",
    };
    const view = renderBoard();
    expect(screen.getByRole("link", { name: "Model pools ↗" }).getAttribute("href")).toBe(
      "/extensions/example.pools/main",
    );
    view.unmount();
    mocks.extensions.current = [];
    renderBoard();
    expect(screen.queryByRole("link", { name: "Model pools ↗" })).toBeNull();
    expect(screen.getByText(/Model pools · Not available yet/)).toBeTruthy();
  });
});

describe("Proven pool usage", () => {
  it("keeps all cost cells and model detail inside the selected pool", async () => {
    const account = usageBoardData().accounts[0].id;
    mocks.board.current = { ...(mocks.board.current as object), unit: "tokens" };
    const totals = sampleTotals(3.2, 1000);
    const now = Date.now() / 1000;
    const fetch = vi.spyOn(globalThis, "fetch").mockImplementation(
      async () =>
        new Response(
          JSON.stringify({
            generated_at: now,
            accounts: [],
            span: {
              requested_from: now - 86400,
              counted_from: now - 86400,
              until: now,
              approx: false,
            },
            account_spans: {},
            pool_catalog: [
              {
                parent_account_id: account,
                pool_id: "pool-a",
                display_name: "Pool A",
                usage_available: true,
              },
            ],
            pools: [
              { parent_account_id: account, pool_id: "pool-a", channel: "usage", totals },
              {
                parent_account_id: account,
                pool_id: "pool-b",
                channel: "usage",
                totals: sampleTotals(99, 99999),
              },
            ],
            rows: [
              {
                parent_account_id: account,
                pool_id: "pool-a",
                channel: "usage",
                hour: now - 3600,
                model: "selected-model",
                cost_usd: 3.2,
                tokens: 1000,
                contributes_money: true,
                contributes_usage: true,
              },
              {
                parent_account_id: account,
                pool_id: "pool-b",
                channel: "usage",
                hour: now - 3600,
                model: "other-model",
                cost_usd: 99,
                tokens: 99999,
                contributes_money: true,
                contributes_usage: true,
              },
            ],
          }),
          { headers: { "Content-Type": "application/json" } },
        ),
    );
    try {
      renderBoard(`/usage/board/${encodeURIComponent(account)}?focus=Pool%20A`);
      const cells = screen.getByRole("group", { name: "Cost time range" });
      await waitFor(() => expect(within(cells).getAllByText("$3.20")).toHaveLength(3));
      fireEvent.click(screen.getByText("Cost breakdown", { exact: true }));
      expect(
        screen.getByText("selected-model", { exact: true }).parentElement?.textContent,
      ).toContain("$3.20");
      expect(screen.queryByText("other-model", { exact: true })).toBeNull();
      expect(within(cells).queryByText("$99.00")).toBeNull();
    } finally {
      fetch.mockRestore();
    }
  });
});
