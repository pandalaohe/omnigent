import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { UsageCostPage } from "./UsageCostPage";
import { usageAnalysisV2Fixture } from "./usageActivityFixtures";
import type { AnalysisHour, UsageAnalysisV2, UsageRequestRow } from "./usageAnalysisData";

const { fetchMock } = vi.hoisted(() => ({ fetchMock: vi.fn() }));
vi.mock("@/lib/identity", () => ({ authenticatedFetch: fetchMock }));

function makeHour(
  start: number,
  label: string,
  tokens: number,
  overrides: Partial<AnalysisHour> = {},
): AnalysisHour {
  return {
    start,
    label,
    tokens,
    requests: tokens > 0 ? 10 : 0,
    failed: 0,
    last_at: tokens > 0 ? start + 100 : null,
    coverage: "complete",
    totals: {
      tokens,
      requests: tokens > 0 ? 10 : 0,
      failed: 0,
      last_at: tokens > 0 ? start + 100 : null,
      coverage: "complete",
      api_value: {
        state: "exact",
        low: (tokens / 500).toFixed(2),
        high: (tokens / 500).toFixed(2),
        currency: "USD",
        reason: null,
        formatted: { low: (tokens / 500).toFixed(2), high: (tokens / 500).toFixed(2) },
      },
      paid: { state: "unknown", low: null, high: null, currency: null, reason: "test" },
      payback: { state: "unknown", low: null, high: null, currency: null, reason: "test" },
    },
    top_host: tokens > 0 ? "east" : null,
    top_agent: tokens > 0 ? "codex" : null,
    top_model: tokens > 0 ? "Sonnet" : null,
    ...overrides,
  };
}

function makeDay(day: string, hours: AnalysisHour[]): UsageAnalysisV2["days"][number] {
  const tokens = hours.reduce((sum, item) => sum + (item.tokens ?? 0), 0);
  const requests = hours.reduce((sum, item) => sum + (item.requests ?? 0), 0);
  return {
    day,
    tokens,
    requests,
    failed: 0,
    last_at: hours.at(0)?.last_at ?? null,
    coverage: "complete",
    api_value: {
      state: "exact",
      low: (tokens / 500).toFixed(2),
      high: (tokens / 500).toFixed(2),
      currency: "USD",
      reason: null,
      formatted: { low: (tokens / 500).toFixed(2), high: (tokens / 500).toFixed(2) },
    },
    paid: { state: "unknown", low: null, high: null, currency: null, reason: "test" },
    payback: { state: "unknown", low: null, high: null, currency: null, reason: "test" },
    classes: {
      basis: "price_table_estimate",
      items: [
        {
          kind: "input",
          tokens: Math.round(tokens * 0.6),
          cost_low_usd: "1",
          cost_high_usd: "1",
          unpriced_tokens: 0,
        },
        {
          kind: "output",
          tokens: Math.round(tokens * 0.4),
          cost_low_usd: "1",
          cost_high_usd: "1",
          unpriced_tokens: 0,
        },
      ],
      unpriced_tokens: {},
      unsplit_tokens: 0,
      unsplit_cost: { low: "0", high: "0" },
    },
    hours,
  };
}

function makeRequestRows(count: number, offset = 0): UsageRequestRow[] {
  return Array.from({ length: count }, (_, index) => {
    const num = offset + index + 1;
    return {
      id: `req-${num}`,
      source: "litellm",
      at: `2026-11-05T11:${String(59 - index).padStart(2, "0")}:05.000Z`,
      host: "east",
      agent: "codex",
      account: "claude",
      group: "shared",
      model: "Sonnet",
      model_requested: "Sonnet",
      tokens: { input: 100 + num, cache_read: 20, cache_write: 5, output: 40, reasoning: 0 },
      cost_usd: "0.02",
      cost_basis: "litellm",
      ttft_ms: 150,
      duration_ms: 1200,
      tps: 40,
      status: "ok",
      http_status: 200,
      error_class: null,
      fallback: false,
    };
  });
}

function CurrentUrl() {
  const { pathname, search } = useLocation();
  return (
    <output data-testid="location">
      {pathname}
      {search}
    </output>
  );
}

function mount(query = "?range=d90") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/usage/board/analysis${query}`]}>
        <UsageCostPage />
        <CurrentUrl />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  fetchMock.mockReset();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

it("paginates 90 days at 10 per page and closes the open day when paging", async () => {
  const baseDay = Date.UTC(2026, 10, 5, 0, 0, 0) / 1000;
  const ninetyDays = Array.from({ length: 90 }, (_, index) => {
    const ts = baseDay - index * 86400;
    const iso = new Date(ts * 1000).toISOString().slice(0, 10);
    return makeDay(iso, [makeHour(ts + 10 * 3600, "10:00 +0000", 1000 + index)]);
  });

  fetchMock.mockImplementation(async (path: string) => {
    const url = new URL(path, "https://app.example.test");
    if (url.pathname.endsWith("/analysis")) {
      const fixture = usageAnalysisV2Fixture(url.searchParams);
      return Response.json({ ...fixture, days: ninetyDays, requests_configured: true });
    }
    if (url.pathname.endsWith("/analysis/hours")) {
      const day = url.searchParams.get("day")!;
      const found = ninetyDays.find((item) => item.day === day);
      return Response.json({
        filter: {},
        span: { requested_from: baseDay, counted_from: baseDay, until: baseDay + 86400 },
        complete: true,
        incomplete_reason: null,
        approx: false,
        requests_configured: true,
        hours: found?.hours ?? [],
      });
    }
    return Response.json({ accounts: [] });
  });

  mount("?range=d90");
  const section = await screen.findByRole("region", { name: "Usage records" });
  expect(within(section).getByText(/90 days/)).toBeVisible();
  expect(within(section).getByText("Page 1 / 9 · 10 days per page")).toBeVisible();
  expect(within(section).getAllByRole("button", { name: /^Expand / })).toHaveLength(10);

  // Open day 1, verify hour drill loads.
  fireEvent.click(within(section).getByRole("button", { name: "Expand 2026-11-05" }));
  expect(await within(section).findByTestId("day-hours-2026-11-05")).toBeVisible();

  // Paging to page 2 closes the open day.
  fireEvent.click(within(section).getByRole("button", { name: "Next" }));
  expect(within(section).getByText("Page 2 / 9 · 10 days per page")).toBeVisible();
  expect(within(section).queryByTestId("day-hours-2026-11-05")).toBeNull();
  expect(within(section).getByText("2026-10-26")).toBeVisible();
});

it("drills hours newest-first, preserves 23h/25h DST days and current-hour today, and paginates 逐条 at 8 per page", async () => {
  const springDay = makeDay(
    "2026-03-08",
    Array.from({ length: 23 }, (_, i) =>
      makeHour(1772946000 + i * 3600, `${String(i).padStart(2, "0")}:00 -0400`, i === 5 ? 500 : 0),
    ),
  );
  const fallDay = makeDay(
    "2026-11-01",
    Array.from({ length: 25 }, (_, i) => {
      const label =
        i === 1
          ? "01:00 -0400"
          : i === 2
            ? "01:00 -0500"
            : `${String(i > 1 ? i - 1 : i).padStart(2, "0")}:00 ${i < 2 ? "-0400" : "-0500"}`;
      return makeHour(1793505600 + i * 3600, label, i === 1 || i === 2 ? 700 : 0);
    }),
  );
  const todayDay = makeDay(
    "2026-11-05",
    Array.from({ length: 12 }, (_, i) =>
      makeHour(1793851200 + i * 3600, `${String(i).padStart(2, "0")}:00 -0500`, i === 11 ? 900 : 0),
    ),
  );
  const days = [todayDay, fallDay, springDay];

  const page1Rows = makeRequestRows(8, 0);
  const page2Rows = makeRequestRows(3, 8);
  fetchMock.mockImplementation(async (path: string) => {
    const url = new URL(path, "https://app.example.test");
    if (url.pathname.endsWith("/analysis")) {
      const fixture = usageAnalysisV2Fixture(url.searchParams);
      return Response.json({ ...fixture, tz: "America/New_York", days, requests_configured: true });
    }
    if (url.pathname.endsWith("/analysis/hours")) {
      const day = url.searchParams.get("day");
      const found = days.find((item) => item.day === day);
      const used = (found?.hours ?? []).filter((hour) => hour.last_at !== null).reverse();
      return Response.json({
        filter: {},
        span: { requested_from: 0, counted_from: 0, until: 3600 },
        complete: true,
        incomplete_reason: null,
        approx: false,
        requests_configured: true,
        hours: used,
      });
    }
    if (url.pathname.endsWith("/requests")) {
      const cursor = url.searchParams.get("cursor");
      expect(url.searchParams.get("limit")).toBe("8");
      if (!cursor) {
        return Response.json({
          hour: Number(url.searchParams.get("hour")),
          tz: "America/New_York",
          rows: page1Rows,
          next_cursor: "cursor-page-2",
          retained_from: 0,
          sections: null,
        });
      }
      expect(cursor).toBe("cursor-page-2");
      return Response.json({
        hour: Number(url.searchParams.get("hour")),
        tz: "America/New_York",
        rows: page2Rows,
        next_cursor: null,
        retained_from: 0,
        sections: null,
      });
    }
    return Response.json({ accounts: [] });
  });

  mount("?range=d30");
  const section = await screen.findByRole("region", { name: "Usage records" });
  expect(within(section).getByLabelText("2026-03-08 hourly strip (23 hours)")).toBeVisible();
  expect(within(section).getByLabelText("2026-11-01 hourly strip (25 hours)")).toBeVisible();
  expect(within(section).getByLabelText("2026-11-05 hourly strip (12 hours)")).toBeVisible();

  // Open fall-back DST day -> both repeated 01:00 offsets shown newest-first.
  fireEvent.click(within(section).getByRole("button", { name: "Expand 2026-11-01" }));
  const hoursPanel = await within(section).findByTestId("day-hours-2026-11-01");
  const hourLabels = (await within(hoursPanel).findAllByText(/^01:00 -0[45]00$/)).map(
    (node) => node.textContent,
  );
  expect(hourLabels).toEqual(["01:00 -0500", "01:00 -0400"]);

  // Click 逐条 on the newest hour -> 8 rows on page 1, Next replaces list with page 2 (3 rows).
  const reqButtons = within(hoursPanel).getAllByRole("button", { name: "逐条" });
  fireEvent.click(reqButtons[0]);
  const reqPanel = await within(hoursPanel).findByTestId(`hour-requests-${1793505600 + 2 * 3600}`);
  await waitFor(() => expect(within(reqPanel).getAllByText(/^OK$/)).toHaveLength(8));
  expect(within(reqPanel).getByText("Page 1 · 8 per page")).toBeVisible();

  fireEvent.click(within(reqPanel).getByRole("button", { name: "Next" }));
  await waitFor(() => expect(within(reqPanel).getAllByText(/^OK$/)).toHaveLength(3));
  expect(within(reqPanel).getByText("Page 2 · 8 per page")).toBeVisible();
  expect(within(reqPanel).getByRole("button", { name: "Next" })).toBeDisabled();

  // Filter above to this day updates the URL day parameter.
  fireEvent.click(within(hoursPanel).getByRole("button", { name: "Filter above to 2026-11-01" }));
  await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("day=2026-11-01"));
});

it("aborts in-flight 逐条 read on collapse, shows 504 timeout with non-duplicating retry, and hides 逐条 when unconfigured", async () => {
  const hourStart = 1793851200;
  const day = makeDay("2026-11-05", [makeHour(hourStart, "11:00 +0000", 500)]);

  const signalRef: { current?: AbortSignal } = {};
  let mode: "hang" | "timeout" | "ok" | "not_configured" = "hang";
  fetchMock.mockImplementation((path: string, init?: RequestInit) => {
    const url = new URL(path, "https://app.example.test");
    if (url.pathname.endsWith("/analysis")) {
      const fixture = usageAnalysisV2Fixture(url.searchParams);
      return Promise.resolve(Response.json({ ...fixture, days: [day], requests_configured: true }));
    }
    if (url.pathname.endsWith("/analysis/hours")) {
      return Promise.resolve(
        Response.json({
          filter: {},
          span: { requested_from: hourStart, counted_from: hourStart, until: hourStart + 3600 },
          complete: true,
          incomplete_reason: null,
          approx: false,
          requests_configured: true,
          hours: day.hours,
        }),
      );
    }
    if (url.pathname.endsWith("/requests")) {
      if (mode === "hang") {
        signalRef.current = init?.signal ?? undefined;
        return new Promise(() => {});
      }
      if (mode === "timeout") {
        return Promise.resolve(
          Response.json({ detail: "request_source_timeout" }, { status: 504 }),
        );
      }
      if (mode === "not_configured") {
        return Promise.resolve(Response.json({ detail: "not_configured" }, { status: 404 }));
      }
      return Promise.resolve(
        Response.json({
          hour: hourStart,
          tz: "UTC",
          rows: makeRequestRows(2, 0),
          next_cursor: null,
          retained_from: 0,
          sections: null,
        }),
      );
    }
    return Promise.resolve(Response.json({ accounts: [] }));
  });

  mount("?range=d7");
  const section = await screen.findByRole("region", { name: "Usage records" });
  fireEvent.click(within(section).getByRole("button", { name: "Expand 2026-11-05" }));
  const hoursPanel = await within(section).findByTestId("day-hours-2026-11-05");

  // 1. Collapse aborts in-flight request.
  const toggle = await within(hoursPanel).findByRole("button", { name: "逐条" });
  fireEvent.click(toggle);
  await waitFor(() => expect(signalRef.current?.aborted).toBe(false));
  fireEvent.click(toggle);
  expect(signalRef.current?.aborted).toBe(true);

  // 2. 504 timeout -> shows 逐条读取超时 with 重试; retry succeeds and does not duplicate.
  mode = "timeout";
  fireEvent.click(within(hoursPanel).getByRole("button", { name: "逐条" }));
  const reqPanel = await within(hoursPanel).findByTestId(`hour-requests-${hourStart}`);
  expect(await within(reqPanel).findByRole("alert")).toHaveTextContent("逐条读取超时");
  mode = "ok";
  fireEvent.click(within(reqPanel).getByRole("button", { name: "重试" }));
  await waitFor(() => expect(within(reqPanel).getAllByText(/^OK$/)).toHaveLength(2));

  // Collapse and reopen with 404 not_configured -> hides 逐条 and shows 逐条读取没有配置.
  fireEvent.click(within(hoursPanel).getByRole("button", { name: "逐条" }));
  mode = "not_configured";
  fireEvent.click(within(hoursPanel).getByRole("button", { name: "逐条" }));
  expect(await within(hoursPanel).findByText("逐条读取没有配置")).toBeVisible();
  expect(within(hoursPanel).queryByRole("button", { name: "逐条" })).toBeNull();
});

it.each([
  { state: "expired", text: "Gateway records for this hour have expired.", count: 0 },
  { state: "partial", text: "Gateway records for this hour are incomplete.", count: 0 },
  { state: "partial", text: "Gateway records for this hour are incomplete.", count: 1 },
  { state: "ok", text: "No gateway requests in this hour.", count: 0 },
  { state: "unbound", text: "No gateway account is linked to this selection.", count: 0 },
])("shows the $state gateway result with $count rows", async ({ state, text, count }) => {
  const hour = 1793851200;
  const day = makeDay("2026-11-05", [makeHour(hour, "11:00 +0000", 500)]);
  fetchMock.mockImplementation((path: string) => {
    const url = new URL(path, "https://app.example.test");
    if (url.pathname.endsWith("/analysis")) {
      const fixture = usageAnalysisV2Fixture(url.searchParams);
      return Response.json({ ...fixture, days: [day], requests_configured: true });
    }
    if (url.pathname.endsWith("/analysis/hours")) {
      return Response.json({ hours: day.hours, requests_configured: true });
    }
    if (url.pathname.endsWith("/requests")) {
      if (state === "unbound") {
        return Response.json({ detail: "no_gateway_binding" }, { status: 404 });
      }
      return Response.json({
        hour,
        state,
        rows: makeRequestRows(count),
        next_cursor: null,
      });
    }
    return Response.json({ accounts: [] });
  });

  mount("?range=d7");
  const section = await screen.findByRole("region", { name: "Usage records" });
  fireEvent.click(within(section).getByRole("button", { name: "Expand 2026-11-05" }));
  const hoursPanel = await within(section).findByTestId("day-hours-2026-11-05");
  fireEvent.click(await within(hoursPanel).findByRole("button", { name: "逐条" }));
  const panel = await within(hoursPanel).findByTestId(`hour-requests-${hour}`);
  expect(await within(panel).findByText(text)).toBeVisible();
  expect(within(panel).queryAllByText("OK")).toHaveLength(count);
  expect(within(panel).queryByText("No gateway requests in this hour.") !== null).toBe(
    state === "ok",
  );
});
