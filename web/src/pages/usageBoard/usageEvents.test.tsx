import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import { UsageEvents } from "./UsageEvents";
import { usageBoardData } from "./usageBoardFixtures";
import { sampleTotals } from "./usageActivityFixtures";
import type { UsageActivity } from "./usageActivityData";
import { usageJson } from "./usagePriceSourceData";

const locale = vi.hoisted(() => ({ value: "en" }));
vi.mock("@/lib/uiLanguage", () => ({
  get uiLanguage() {
    return locale.value;
  },
}));
vi.mock("./usagePriceSourceData", () => ({ usageJson: vi.fn() }));
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  locale.value = "en";
});

function setup() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const activity: UsageActivity = {
    generated_at: 1000,
    range: "h24",
    tz: "UTC",
    accounts: [
      {
        account: "a",
        mark: null,
        previous: null,
        span: { requested_from: 0, counted_from: 0, until: 1000, approx: false },
        totals: { ...sampleTotals(1, 100), last_at: 100 },
        series: [],
      },
    ],
  };
  const view = (generated_at: number) => (
    <QueryClientProvider client={client}>
      <UsageEvents accounts={usageBoardData().accounts} activity={{ ...activity, generated_at }} />
    </QueryClientProvider>
  );
  return { view, client };
}

it("keeps the cost receipt time after ten refreshes and marks an expired source", async () => {
  vi.mocked(usageJson).mockImplementation(async (path) =>
    path.startsWith("sources")
      ? { sources: [{ fresh_until: 1 }] }
      : { events: [], next_before: null },
  );
  const { view, client } = setup();
  const rendered = render(view(1000));
  expect(screen.getByRole("region", { name: "Event history" })).toBeTruthy();
  expect(screen.getByRole("group", { name: "Event type" })).toBeTruthy();
  const label = await screen.findByText(/^Cost fetched/);
  const receipt = label.textContent;
  await waitFor(() => expect(label.className).toContain("text-amber-600"));
  for (let i = 1; i <= 10; i++) {
    rendered.rerender(view(1000 + i));
    expect(screen.getByText(/^Cost fetched/).textContent).toBe(receipt);
  }
  expect(label).not.toHaveTextContent(/next/i);
  client.clear();
});

it("renders a paired fault with raw details and filters before paging", async () => {
  locale.value = "zh-CN";
  vi.mocked(usageJson).mockImplementation(async (path) =>
    path.startsWith("sources")
      ? { sources: [] }
      : {
          events: [
            {
              id: "fault",
              source: "host:workstation",
              account: null,
              type: "connection",
              time: 100,
              severity: "error",
              data: { host: "Workstation" },
              episode: { recovered_at: 220, recover_event_id: "end" },
            },
          ],
          next_before: "cursor",
        },
  );
  const { view, client } = setup();
  render(view(1000));
  expect(screen.getByRole("region", { name: "事件记录" })).toBeTruthy();
  expect(screen.getByRole("group", { name: "事件类型" })).toBeTruthy();
  expect(await screen.findByText(/已恢复 · 2分0秒/)).toBeTruthy();
  fireEvent.click(screen.getByText("原文"));
  expect(screen.getByText(/"host": "Workstation"/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "下一页" }));
  await waitFor(() => expect(usageJson).toHaveBeenCalledWith("events?limit=6&before=cursor"));
  fireEvent.click(screen.getByRole("button", { name: "报警" }));
  await waitFor(() => expect(usageJson).toHaveBeenCalledWith("events?limit=6&kind=warn"));
  expect(screen.getByText("第 1 页")).toBeTruthy();
  client.clear();
});
