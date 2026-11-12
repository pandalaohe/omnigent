import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import { UsageBoardFrame } from "./UsageBoardFrame";
import { UsageTile } from "./UsageTile";
import { UsageSummary } from "./UsageAmounts";
import { usageBoardData } from "./usageBoardFixtures";
import { usageActivityFixture } from "./usageActivityFixtures";
import { formatMoney, formatUsageValue } from "./usageBoardFormat";

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
  expect(pool.getAttribute("href")).toContain("focus=Pool%20B");
  expect(within(pool).getAllByTestId("usage-ring")).toHaveLength(2);
  expect(pool.textContent).toContain(".61%");
  fireEvent.click(pool);
  expect(open).toHaveBeenCalledWith("Pool B");
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
    "/usage/board/cost",
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
  expect(formatUsageValue(1.23456, "credits")).toBe("1.235 credits");
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
  const ring = screen.getByTestId("usage-ring");
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
  expect(screen.getByText("Period reported cost")).toBeTruthy();
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
