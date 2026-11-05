import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import {
  patchUsageBoardPreferences,
  readUsageContextPreferences,
} from "@/lib/usageContextPreferences";
import { UsagePriceSourceSettings } from "./UsagePriceSourceSettings";
import { UsageModelsPage } from "./UsageModelsPage";
import { UsageRangeControl } from "./UsageRangeControl";

const { fetchMock } = vi.hoisted(() => ({ fetchMock: vi.fn() }));
vi.mock("@/lib/identity", () => ({ authenticatedFetch: fetchMock }));

const initial = {
  source: "builtin",
  can_edit: true,
  model_count: 0,
  fetched_at: null,
  error: null,
};
beforeEach(() => {
  localStorage.clear();
  fetchMock.mockReset();
  patchUsageBoardPreferences({ enabled: true });
});
afterEach(cleanup);

function mount(view: "settings" | "models" | "range") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/usage/board/gateway/models"]}>
        <Routes>
          <Route
            path="/usage/board/:accountId/models"
            element={
              view === "models" ? (
                <UsageModelsPage />
              ) : view === "range" ? (
                <UsageRangeControl />
              ) : (
                <UsagePriceSourceSettings />
              )
            }
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

it("saves a selected map and leaves the prior selection after a rejected URL", async () => {
  let status = initial;
  fetchMock.mockImplementation(async (_path, init) => {
    if (init?.method === "PUT") {
      const body = JSON.parse(init.body);
      if (body.source === "custom")
        return new Response(JSON.stringify({ detail: "No valid prices" }), { status: 422 });
      status = { ...status, ...body, model_count: 12 };
    }
    return new Response(JSON.stringify(status));
  });
  mount("settings");
  const select = await screen.findByLabelText("Price source");
  fireEvent.change(select, { target: { value: "litellm" } });
  await screen.findByText("Saved");
  expect((select as HTMLSelectElement).value).toBe("litellm");
  fireEvent.click(screen.getByRole("button", { name: "Add price source URL" }));
  fireEvent.change(screen.getByLabelText("Price map HTTPS URL"), {
    target: { value: "https://prices.example.test/empty.json" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Add source" }));
  await screen.findByText("No valid prices");
  expect((select as HTMLSelectElement).value).toBe("litellm");
  expect(screen.getByRole("dialog")).toBeTruthy();
});

it("applies Custom dates live, moves the end with the start and rejects future dates", () => {
  const today = new Date();
  const day = (offset: number) => {
    const date = new Date(today.getFullYear(), today.getMonth(), today.getDate() + offset);
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
  };
  patchUsageBoardPreferences({ range: { key: "d7" } });
  mount("range");
  fireEvent.click(screen.getByRole("button", { name: "Usage range" }));
  fireEvent.click(screen.getByRole("button", { name: /^Custom/ }));
  expect(screen.queryByRole("button", { name: "Apply" })).toBeNull();
  expect(readUsageContextPreferences().board.range?.key).toBe("custom");
  const start = screen.getByLabelText("Usage from date");
  const end = screen.getByLabelText("Usage to date");
  expect(start.getAttribute("max")).toBe(day(0));
  expect(end.getAttribute("max")).toBe(day(0));
  fireEvent.change(start, { target: { value: day(-4) } });
  fireEvent.change(end, { target: { value: day(-3) } });
  expect(readUsageContextPreferences().board.range).toEqual({
    key: "custom",
    from: day(-4),
    to: day(-3),
  });
  fireEvent.change(start, { target: { value: day(-1) } });
  expect(readUsageContextPreferences().board.range).toEqual({
    key: "custom",
    from: day(-1),
    to: day(-1),
  });
  fireEvent.change(start, { target: { value: day(1) } });
  fireEvent.change(end, { target: { value: day(1) } });
  expect(readUsageContextPreferences().board.range).toEqual({
    key: "custom",
    from: day(-1),
    to: day(-1),
  });
});

it("shows a server-wide source read-only to a member", async () => {
  fetchMock.mockResolvedValue(
    new Response(JSON.stringify({ ...initial, source: "openrouter", can_edit: false })),
  );
  mount("settings");
  await screen.findByText("OpenRouter");
  expect(screen.queryByRole("combobox")).toBeNull();
  expect(screen.queryByRole("button", { name: "Add price source URL" })).toBeNull();
});

it("reads models for the selected account and remembered range, with a tokens switch", async () => {
  patchUsageBoardPreferences({ range: { key: "d30" } });
  fetchMock.mockImplementation(
    async () =>
      new Response(
        JSON.stringify({
          account: "gateway",
          display_name: "Gateway",
          range: "d30",
          span: null,
          models: [
            {
              id: "vendor/model",
              sources: [
                {
                  kind: "host",
                  label: "Laptop",
                  stale: true,
                  configuration: { label: "CLI config", name: "Gateway" },
                },
              ],
              pricing: { input_per_token: 2e-6, output_per_token: 6e-6 },
              usage: { cost_usd: 3, tokens: 1500000, unpriced: 0 },
            },
          ],
        }),
      ),
  );
  mount("models");
  await screen.findByText("vendor/model");
  expect(screen.getByText("$2.00 · $6.00")).toBeTruthy();
  expect(screen.getByText("$3.00")).toBeTruthy();
  expect(screen.getByText(/Laptop · old · CLI config/)).toBeTruthy();
  expect(
    fetchMock.mock.calls.some(
      ([path]) => path.includes("account=gateway") && path.includes("range=d30"),
    ),
  ).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "Tokens" }));
  await screen.findByText("1.5M");
  fireEvent.click(screen.getByRole("button", { name: "Usage range" }));
  fireEvent.click(screen.getByRole("button", { name: /^7 days/ }));
  await waitFor(() =>
    expect(fetchMock.mock.calls.some(([path]) => path.includes("range=d7"))).toBe(true),
  );
  expect(readUsageContextPreferences().board.range?.key).toBe("d7");
});
