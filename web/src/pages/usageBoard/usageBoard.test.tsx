import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ServerInfo } from "@/lib/capabilities";
import type * as UsageBoardDataModule from "./usageBoardData";
import {
  DEFAULT_USAGE_CONTEXT_PREFERENCES,
  type UsageBoardPreferences,
} from "@/lib/usageContextPreferences";

import { UsageBoardRoute } from "./UsageBoardRoute";
import { usageBoardData } from "./usageBoardFixtures";
import { formatPercent, formatTimeLeft, stateTone } from "./usageBoardFormat";
import { boardBands, moveTile, seedBands } from "./usageBoardLayout";
import { UsageTipCard } from "./UsageTip";

const mocks = vi.hoisted(() => ({
  overview: { current: undefined as unknown },
  info: { current: undefined as unknown },
  board: {
    current: {
      enabled: true,
      density: "std",
      bands: [],
      windows: {},
      renewals: {},
      hidden: [],
    } as UsageBoardPreferences,
  },
  collab: { current: undefined as unknown },
}));

vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => mocks.info.current,
  useCachedServerInfo: () => mocks.info.current,
}));

vi.mock("./usageBoardData", async (importOriginal) => ({
  ...(await importOriginal<typeof UsageBoardDataModule>()),
  useUsageOverview: () => mocks.overview.current,
}));

vi.mock("@/hooks/useUsageContextPreferences", () => ({
  useUsageContextPreferences: () => ({
    ...DEFAULT_USAGE_CONTEXT_PREFERENCES,
    board: mocks.board.current,
  }),
}));

vi.mock("@/hooks/useSessionCollabPreferences", () => ({
  useSessionCollabPreferences: () => mocks.collab.current,
}));

const SERVER_INFO = {
  usage_board_enabled: true,
} as unknown as ServerInfo;

const LOADED = { isPending: false, error: null, refetch: vi.fn(), data: { accounts: [] } };

function LocationProbe() {
  const { pathname, search, hash } = useLocation();
  return (
    <span data-testid="board-location">
      {pathname}
      {search}
      {hash}
    </span>
  );
}

function renderRoute(path = "/usage/board") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <LocationProbe />
        <Routes>
          <Route path="/usage/board" element={<UsageBoardRoute view="board" />} />
          <Route path="/frame/usage/board/cost" element={<UsageBoardRoute view="cost" />} />
          <Route path="/frame/usage/board/analysis" element={<UsageBoardRoute view="cost" />} />
          <Route path="/usage/board/:accountId" element={<UsageBoardRoute view="account" />} />
          <Route
            path="/usage/board/:accountId/settings"
            element={<UsageBoardRoute view="account_settings" />}
          />
          <Route path="/settings/general" element={<div>settings page</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mocks.info.current = SERVER_INFO;
  mocks.overview.current = LOADED;
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

afterEach(() => cleanup());

describe("formatting", () => {
  it("writes time left in two units, and only minutes under an hour", () => {
    expect(formatTimeLeft(6 * 86_400 + 2 * 3_600)).toBe("6d 2h");
    expect(formatTimeLeft(4 * 3_600 + 51 * 60)).toBe("4h 51m");
    expect(formatTimeLeft(45 * 60)).toBe("45m");
    expect(formatTimeLeft(null)).toBe("");
  });

  it("reads a sliver above zero as <1, and nothing as an em dash", () => {
    expect(formatPercent(0.4)).toBe("<1");
    expect(formatPercent(0)).toBe("0");
    expect(formatPercent(null)).toBe("—");
  });

  it("maps the server's pace states to the three colours", () => {
    expect(stateTone("ok")).toBe("ok");
    expect(stateTone("tight")).toBe("warn");
    expect(stateTone("low")).toBe("crit");
    expect(stateTone("exhausted")).toBe("crit");
    expect(stateTone("stale")).toBe("mute");
    expect(stateTone("past_reset")).toBe("mute");
  });
});

describe("board", () => {
  it("opens on All with one quota row and switch per visible account", () => {
    const data = usageBoardData();
    mocks.overview.current = { ...LOADED, data };
    renderRoute();
    const accounts = screen.getByRole("navigation", { name: "Usage accounts" });
    expect(within(accounts).getAllByRole("link")).toHaveLength(data.accounts.length + 1);
    expect(within(accounts).getByRole("link", { name: "All" })).toHaveAttribute(
      "aria-current",
      "true",
    );
    const quota = screen.getByRole("heading", { name: "Quota" }).closest("section")!;
    expect(within(quota).getAllByRole("listitem")).toHaveLength(data.accounts.length);
    const grok = within(quota).getByRole("link", { name: /Grok/ });
    expect(within(grok).getByTestId("usage-ring")).toHaveAttribute("data-state", "none");
  });

  it("selects an account in the URL and displays every quota window", async () => {
    mocks.overview.current = { ...LOADED, data: usageBoardData() };
    renderRoute();
    const accounts = screen.getByRole("navigation", { name: "Usage accounts" });
    const codex = within(accounts).getByRole("link", { name: "Codex" });
    expect(codex).toHaveAttribute("href", "/usage/board?account=codex");
    codex.click();
    await waitFor(() => expect(codex).toHaveAttribute("aria-current", "true"));
    expect(screen.getAllByText(/^Resets /)).toHaveLength(2);
  });

  it("replaces an old account URL with a selected overview and retains its theme and fragment", () => {
    mocks.overview.current = { ...LOADED, data: usageBoardData() };
    renderRoute("/usage/board/codex?theme=dark#quota");
    expect(screen.getByTestId("board-location")).toHaveTextContent(
      "/usage/board?theme=dark&account=codex#quota",
    );
    expect(screen.getByTestId("usage-overview").closest("[data-usage-theme]")).toHaveAttribute(
      "data-usage-theme",
      "dark",
    );
    expect(screen.getByRole("link", { name: "Account details" })).toHaveAttribute(
      "href",
      "/usage/board?theme=dark&account=codex&view=details",
    );
  });

  it("keeps default values and unknown parameters on a legacy analysis bookmark", () => {
    const suffix = "?range=d30&extra=keep&group=account&theme=invalid#summary";
    renderRoute(`/frame/usage/board/cost${suffix}`);
    expect(screen.getByTestId("board-location")).toHaveTextContent(
      `/frame/usage/board/analysis${suffix}`,
    );
    expect(screen.getByRole("heading", { level: 1 }).closest("[data-usage-theme]")).toBeNull();
  });

  it("shows the stale reading and the value-only quota on their selected account", () => {
    mocks.overview.current = { ...LOADED, data: usageBoardData() };
    renderRoute("/usage/board?account=claude");
    const quota = screen.getByRole("heading", { name: "Quota" }).closest("section")!;
    expect(within(quota).getByText("Read 3 h ago")).toBeTruthy();
    expect(within(quota).getByTestId("usage-ring")).toHaveAttribute("data-tone", "mute");
    cleanup();
    renderRoute("/usage/board?account=litellm");
    expect(screen.getByTestId("usage-figure").textContent).toContain("$41.00");
  });

  it("keeps settings controls off the board and the detail page", async () => {
    mocks.overview.current = { ...LOADED, data: usageBoardData() };
    renderRoute();
    await screen.findByTestId("usage-overview");
    expect(screen.queryAllByRole("switch").length).toBe(0);
    expect(screen.queryAllByRole("radio").length).toBe(0);
  });

  it("leaves an account the user switched off off the board, keeping its page", async () => {
    mocks.overview.current = { ...LOADED, data: usageBoardData() };
    mocks.board.current = { ...mocks.board.current, hidden: ["codex"] };
    renderRoute();
    await screen.findByTestId("usage-overview");
    expect(screen.queryByText("Codex")).toBeNull();
    cleanup();
    // A bookmark to the account still resolves: the switch hides it from the
    // board, it does not delete its reading.
    renderRoute("/usage/board/codex");
    expect(await screen.findByRole("heading", { level: 2, name: "Codex" })).toBeTruthy();
  });

  it("says an unknown account is gone rather than rendering an empty page", async () => {
    mocks.overview.current = { ...LOADED, data: usageBoardData() };
    renderRoute("/usage/board/nobody");
    expect(await screen.findByText("This account is not on the board any more.")).toBeTruthy();
  });

  it("tells the visitor where the switch is when the module is off for them", () => {
    mocks.board.current = { ...mocks.board.current, enabled: false };
    renderRoute();
    expect(screen.getByText("Usage board is off")).toBeTruthy();
    expect(screen.getByText("Turn it on in Settings")).toBeTruthy();
  });

  it("keeps account and account settings sub-routes reachable when the board switch is off", async () => {
    mocks.overview.current = { ...LOADED, data: usageBoardData() };
    mocks.board.current = { ...mocks.board.current, enabled: false };
    renderRoute("/usage/board/codex");
    expect((await screen.findByRole("heading", { level: 2, name: "Codex" })).textContent).toBe(
      "Codex",
    );
    expect(screen.queryByText("Usage board is off")).toBeNull();
    cleanup();
    renderRoute("/usage/board/codex/settings");
    expect((await screen.findByRole("heading", { level: 1 })).textContent).toBe("Account settings");
    expect(screen.getByText("Window")).toBeTruthy();
    expect(screen.getByText("Renews")).toBeTruthy();
    expect(screen.queryByText("Usage board is off")).toBeNull();
  });

  it("says the deployment turned the module off rather than showing an empty board", () => {
    mocks.info.current = { usage_board_enabled: false } as unknown as ServerInfo;
    renderRoute();
    expect(screen.getByText("Usage board is off on this server")).toBeTruthy();
  });

  it("shows the loading state while the first read is in flight", () => {
    mocks.overview.current = { ...LOADED, isPending: true, data: undefined };
    renderRoute();
    expect(screen.getByText("Reading your usage…")).toBeTruthy();
  });

  it("reports a failed read instead of drawing an empty board", () => {
    mocks.overview.current = { ...LOADED, error: new Error("Usage read failed (500)") };
    renderRoute();
    expect(screen.getByText("Usage could not be read")).toBeTruthy();
    expect(screen.getByText("Usage read failed (500)")).toBeTruthy();
  });

  it("opens the account's settings without a control on the display page", async () => {
    const data = usageBoardData();
    data.accounts[0] = {
      ...data.accounts[0],
      source: "Quota reader",
      source_type: "subscription",
      source_at: 1794000000,
    };
    mocks.overview.current = { ...LOADED, data };
    renderRoute("/usage/board/codex/settings");
    expect(await screen.findByRole("heading", { level: 1 })).toBeTruthy();
    // The preview tile and the two rows that apply at once; no Save button.
    expect(screen.queryAllByRole("switch").length).toBe(0);
    expect(screen.queryByText("Save")).toBeNull();
    expect(screen.getByText("Window")).toBeTruthy();
    expect(screen.getByText("Renews")).toBeTruthy();
    expect(screen.getByText("Reported by Quota reader")).toBeTruthy();
    expect(screen.getByText("Source type: subscription")).toBeTruthy();
    expect(screen.getByText(/^Reported (Mon|Tue|Wed|Thu|Fri|Sat|Sun)/)).toBeTruthy();
  });
});

describe("tip card", () => {
  it("renders a title, a status chip, bars and rows, never one stacked sentence", () => {
    render(
      <UsageTipCard
        model={{
          title: "7d",
          chip: { label: "Faster than time", tone: "warn" },
          rows: [
            { label: "Used", value: "72%", bar: 72, tone: "warn" },
            { label: "Elapsed", value: "40%", bar: 40, tone: "ok" },
            { label: "Resets", value: "Fri Nov 7, 16:07" },
          ],
          dividerBefore: 2,
        }}
      />,
    );
    const card = screen.getByTestId("usage-tip-card");
    expect(within(card).getByText("7d")).toBeTruthy();
    expect(within(card).getByText("Faster than time")).toBeTruthy();
    expect(within(card).getByText("72%")).toBeTruthy();
    expect(within(card).getByText("Fri Nov 7, 16:07")).toBeTruthy();
    expect(card.className).toContain("w-[216px]");
  });
});

describe("layout", () => {
  const accounts = usageBoardData().accounts;

  it("renders one implicit band in the server's order before anything is arranged", () => {
    const bands = boardBands({ ...mocks.board.current, bands: [] }, accounts);
    expect(bands).toHaveLength(1);
    expect(bands[0].accounts.map((account) => account.id)).toEqual(
      accounts.map((account) => account.id),
    );
  });

  it("keeps the stored order and appends an account the arrangement never saw", () => {
    const bands = boardBands(
      {
        ...mocks.board.current,
        bands: [{ id: "b1", name: "Paid", accounts: ["litellm"], open: true }],
      },
      accounts,
    );
    expect(bands[0].accounts[0].id).toBe("litellm");
    // Everything else follows in the last band, so a new account never vanishes.
    expect(bands[0].accounts).toHaveLength(accounts.length);
  });

  it("drops a hidden account from every band", () => {
    const bands = boardBands({ ...mocks.board.current, hidden: ["grok"] }, accounts);
    expect(bands[0].accounts.map((account) => account.id)).not.toContain("grok");
  });

  it("swaps a tile with its neighbour, and hands it on at the edge", () => {
    const seeded = seedBands({ ...mocks.board.current, bands: [] }, accounts);
    expect(seeded[0].accounts[0]).toBe(accounts[0].id);
    const swapped = moveTile(seeded, accounts, mocks.board.current, accounts[0].id, 1);
    expect(swapped[0].accounts[1]).toBe(accounts[0].id);
    // A second band receives a tile pushed past the end of the first.
    const twoBands = [
      { id: "a", name: "A", accounts: [accounts[0].id], open: true },
      { id: "b", name: "B", accounts: [accounts[1].id], open: true },
    ];
    const moved = moveTile(
      twoBands,
      accounts,
      { ...mocks.board.current, bands: twoBands },
      accounts[0].id,
      1,
    );
    expect(moved[1].accounts[0]).toBe(accounts[0].id);
  });
});
