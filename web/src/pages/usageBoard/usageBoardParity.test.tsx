import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import { UsageBoardFrame } from "./UsageBoardFrame";
import { UsageTile } from "./UsageTile";
import { UsageRing } from "./UsageRing";
import { niceTicks } from "./UsageCostChart";
import { UsageSummary } from "./UsageAmounts";
import { usageBoardData } from "./usageBoardFixtures";
import { usageActivityFixture } from "./usageActivityFixtures";
import {
  figureEms,
  ringFigureSize,
  formatCount,
  formatTileAmount,
  formatPercentReading,
  formatRingPercent,
  splitPercentReading,
  formatMoney,
  formatUsageValue,
} from "./usageBoardFormat";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function mount(children: React.ReactNode) {
  return render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>,
  );
}

it("reads all four fixed ranges once without changing the selected analysis range", async () => {
  const requests: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string) => {
      const params = new URL(path, "https://example.test").searchParams;
      requests.push(params.get("range")!);
      return new Response(JSON.stringify(usageActivityFixture(params)));
    }),
  );
  mount(<UsageSummary accountIds={["litellm"]} />);
  await waitFor(() =>
    expect(screen.getByRole("region", { name: "Usage summary" }).textContent).toContain("$"),
  );
  expect(requests.sort()).toEqual(["d30", "d7", "h24", "today"]);
  expect(screen.getAllByRole("link")).toHaveLength(1);
  expect(screen.getByRole("status").textContent).toContain("API valuation unavailable");
});

it("keeps independent pool links inside a single account tile, with a main ring and satellites", () => {
  const account = usageBoardData().accounts[0];
  account.windows = account.windows.flatMap((window) => [
    { ...window, id: `a-${window.id}`, group: "Pool A", percent_used: 22.25 },
    { ...window, id: `b-${window.id}`, group: "Pool B", percent_used: 90.61 },
  ]);
  const open = vi.fn();
  mount(
    <UsageTile
      account={account}
      density="std"
      pinnedWindowId={undefined}
      renewal={null}
      onOpen={() => {}}
      onOpenGroup={open}
    />,
  );
  expect(screen.getAllByTestId("usage-tile")).toHaveLength(1);
  const pool = screen.getByRole("link", { name: "Codex Pool B" });
  expect(pool).toHaveAttribute("href", "/usage/board?account=codex&view=details&focus=Pool+B");
  expect(within(pool).getAllByTestId("usage-ring")).toHaveLength(2);
  expect(pool.textContent).toContain("90.61%");
  fireEvent.click(pool);
  expect(open).toHaveBeenCalledWith("Pool B");
});

it.each(["std", "mini"] as const)("labels cold source rejection in the %s tile", (density) => {
  const account = usageBoardData().accounts[0];
  mount(
    <UsageTile
      account={{
        ...account,
        windows: [],
        read_at: null,
        age_s: null,
        freshness: "none",
        quota_source_rejection: { last_valid_at: null, reason: "Reset change rejected" },
      }}
      density={density}
      pinnedWindowId={undefined}
      renewal={null}
      onOpen={() => {}}
    />,
  );
  expect(
    screen.getByText("Source rejected · No valid reading · Reset change rejected"),
  ).toBeVisible();
});

it("keeps navigation and the tools outside the matched tile composition", () => {
  const name = "Long account name with multiple segments and meaningful suffix";
  mount(
    <UsageBoardFrame
      title="Usage board"
      back={{ to: "/usage/board/plan", label: name }}
      actions={
        <details>
          <summary>Board tools</summary>
          <button type="button">Arrange</button>
        </details>
      }
    >
      <span>Body</span>
    </UsageBoardFrame>,
  );
  expect(screen.getByRole("navigation", { name: "Usage pages" })).toBeTruthy();
  expect(screen.getByRole("link", { name: "Usage" }).getAttribute("aria-current")).toBe("page");
  expect(screen.getByRole("link", { name: "Analysis" }).getAttribute("href")).toBe(
    "/usage/board/analysis",
  );
  expect(screen.getByRole("link", { name }).getAttribute("title")).toBe(name);
});

it("renders monetary zero, small and large amounts with exactly two decimal places", () => {
  expect([0, 0.2195, 1234.567, null].map(formatMoney)).toEqual([
    "$0.00",
    "$0.22",
    "$1,234.57",
    "—",
  ]);
});

it("formats monetary balances separately from non-monetary counts", () => {
  expect(formatUsageValue(298.244, "CNY")).toBe("¥298.24");
  expect(formatUsageValue(1.23456, "credits")).toBe("1.23 credits");
  expect(formatUsageValue(1234567890123.45, "USD")).toBe("$1,234,567,890,123.45");
});

it("rounds monetary ties half-even only at the final display boundary", () => {
  expect([1.005, 1.015, -1.005, -1.015].map(formatMoney)).toEqual([
    "$1.00",
    "$1.02",
    "$-1.00",
    "$-1.02",
  ]);
  expect(formatUsageValue(1.005, "USD")).toBe("$1.00");
  expect(formatUsageValue(1.015, "HKD")).toBe("HK$1.02");
  expect(formatMoney(1.0051)).toBe("$1.01");
});

it("uses the same final monetary precision in the visible ring and opened Now/Limit tooltip", async () => {
  const account = usageBoardData().accounts[2];
  account.windows = [
    {
      ...account.windows[0],
      used_value: 1.005,
      limit_value: 1.015,
      unit: "USD",
      percent_used: null,
    },
  ];
  mount(
    <UsageTile
      account={account}
      density="std"
      pinnedWindowId={undefined}
      renewal={null}
      onOpen={() => {}}
    />,
  );
  const ring = screen.getByTestId("usage-figure");
  expect(ring.textContent).toContain("$1.00");
  fireEvent.focus(ring.parentElement!, { target: ring.parentElement });
  fireEvent.pointerEnter(ring.parentElement!, { pointerType: "mouse" });
  const tip = await screen.findByTestId("usage-tip-card");
  expect(tip.textContent).toContain("Now$1.00");
  expect(tip.textContent).toContain("Limit$1.02");
  expect(account.windows[0].used_value).toBe(1.005);
});

it("keeps a critical short window on its satellite while the normal main window owns tile and box tones", () => {
  const account = usageBoardData().accounts[0];
  account.windows = [
    { ...account.windows[0], state: "low", percent_used: 96 },
    { ...account.windows[1], state: "ok", percent_used: 1.09 },
  ];
  const view = mount(
    <UsageTile
      account={account}
      density="std"
      pinnedWindowId={undefined}
      renewal={null}
      onOpen={() => {}}
    />,
  );
  expect(screen.getByTestId("usage-tile").getAttribute("data-tone")).toBe("ok");
  expect(screen.getAllByTestId("usage-ring").map((r) => r.getAttribute("data-tone"))).toEqual([
    "ok",
    "crit",
  ]);
  expect(screen.getByLabelText("Period reported cost")).toBeTruthy();
  view.unmount();
  account.windows = account.windows.flatMap((w) => [
    { ...w, id: `a-${w.id}`, group: "A" },
    { ...w, id: `b-${w.id}`, group: "B" },
  ]);
  mount(
    <UsageTile
      account={account}
      density="std"
      pinnedWindowId={undefined}
      renewal={null}
      onOpen={() => {}}
    />,
  );
  expect(screen.getByRole("link", { name: "Codex A" }).getAttribute("data-tone")).toBe("ok");
  expect(screen.getByRole("link", { name: "Codex B" }).getAttribute("data-tone")).toBe("ok");
});

it.each([
  [999, "999"],
  [1000, "1K"],
  [12345.678, "12.3K"],
  [0.001234, "0.00123"],
  [-12345.678, "-12.3K"],
])("formats non-money %s with at most three significant digits", (value, expected) => {
  expect(formatCount(value)).toBe(expected);
});
it.each([
  [99999.99, "USD", "$99,999.99"],
  [100000, "USD", "$100.00K"],
  [999999.99, "USD", "$999.99K"],
  [1.23e12, "USD", "$1.23T"],
  [123456, "CNY", "¥123.45K"],
  [123456, "HKD", "HK$123.45K"],
  [123456, "requests", "123K requests"],
])("compacts constrained amount %s %s", (value, unit, expected) => {
  expect(formatTileAmount(value, unit)).toBe(expected);
});
it.each([
  [0, "0"],
  [0.004, "<0.01"],
  [0.5, "0.5"],
  [9.999, "10"],
  [99.95, "99.9"],
  [100, "100"],
  [104, "104"],
  [1234, ">999"],
])("formats percent %s without false zero or full", (value, expected) => {
  expect(formatPercentReading(value)).toBe(expected);
});
it("draws a row glyph as one used arc and a full ring with its time arc and stacked reading", () => {
  const window = { ...usageBoardData().accounts[0].windows[0], percent_used: 42.5 };
  const { container } = render(
    <>
      <UsageRing window={window} size={16} showName={false} />
      <UsageRing window={window} size={96} />
    </>,
  );
  const [glyph, ring] = container.querySelectorAll('[data-testid="usage-ring"]');
  expect(glyph.querySelector(".usage-ring-time-track")).toBeNull();
  expect(glyph.querySelectorAll("circle")).toHaveLength(2);
  expect(glyph.textContent).toBe("");
  expect(ring.querySelector(".usage-ring-time-track")).toBeTruthy();
  const reading = ring.querySelector('[data-usage-percent="stack"]')!;
  expect(reading.textContent).toBe("42.50%");
  expect(reading.querySelector(".usage-ring-whole")!.textContent).toBe("42");
});
it.each([
  [0, "0.00", "0", ".00"],
  [0.004, "<0.01", "<0", ".01"],
  [12, "12.00", "12", ".00"],
  [90.613, "90.61", "90", ".61"],
  [99.996, "99.99", "99", ".99"],
  [100, "100.00", "100", ".00"],
  [1234, ">999", ">999", ""],
  ["31.00", "31.00", "31", ".00"],
  ["-31.00", "-31.00", "-31", ".00"],
  ["1234.00", "1234.00", "1234", ".00"],
])("writes ring percent %s with two decimals, split for the stack", (value, text, whole, rest) => {
  expect(formatRingPercent(value)).toBe(text);
  expect(splitPercentReading(text)).toEqual([whole, rest]);
});
it.each([0, 0.004, 3.45, 1.3e10])("gives distinct monetary axis labels for %s", (top) => {
  const ticks = niceTicks(top, 4, 0.01);
  expect(ticks[0]).toBe(0);
  expect(ticks.at(-1)).toBeGreaterThanOrEqual(top);
  expect(new Set(ticks.map((value) => formatTileAmount(value, "USD"))).size).toBe(ticks.length);
});
it.each(["100", "90.6", "<0.01", ">999"])(
  "fits %s and its name inside the clear ring",
  (reading) => {
    const ems = figureEms(reading, "%");
    const size = ringFigureSize(ems, 15, 34.5);
    expect(Math.hypot((ems * size) / 2, (15 + 0.74 * size) / 2)).toBeLessThanOrEqual(34.5);
  },
);
