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
  expect(screen.getAllByRole("link")).toHaveLength(4);
  expect(screen.getByText(/API-price parity pending/)).toBeTruthy();
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
  mount(
    <UsageBoardFrame
      title="Usage board"
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
});
