import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { SessionChoiceSettings } from "./SessionChoiceSettings";
import { EMPTY_CHOICE, type ChoiceMeters } from "./sessionChoiceData";
import type * as SessionChoiceData from "./sessionChoiceData";

const mocks = vi.hoisted(() => ({
  request: vi.fn(),
  refetch: vi.fn(),
  metersRefetch: vi.fn(),
  document: { current: undefined as unknown },
  meters: { current: { meters: [], running: {}, floors: {} } as ChoiceMeters },
}));
vi.mock("./sessionChoiceData", async (original) => ({
  ...(await original<typeof SessionChoiceData>()),
  choiceRequest: mocks.request,
  useSessionChoiceDocument: () => mocks.document.current,
  useChoiceMeters: () => ({
    data: mocks.meters.current,
    refetch: mocks.metersRefetch,
  }),
}));
vi.mock("./QuotaPreferencesSettings", () => ({
  QuotaPreferencesSettings: () => <div>Quota display controls</div>,
}));
vi.mock("./ChoicePolicySettings", () => ({ ChoicePolicySettings: () => null }));
vi.mock("@/hooks/useHosts", () => ({
  useHosts: () => ({
    data: [
      { host_id: "host-a", name: "Desktop" },
      { host_id: "host-b", name: "Laptop" },
    ],
  }),
}));
vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({ usage_board_enabled: false }),
}));

beforeEach(() => {
  mocks.meters.current = { meters: [], running: {}, floors: {} };
  mocks.refetch.mockResolvedValue({});
  mocks.document.current = {
    isPending: false,
    refetch: mocks.refetch,
    data: {
      revision: "revision-a",
      settings: {
        ...structuredClone(EMPTY_CHOICE),
        workTypes: [{ id: "work", label: "Work" }],
        presets: [{ id: "code", name: "Code", links: [{ agent: "agent-a" }] }],
        hosts: {
          "host-a": { work: [[{ preset: "code", links: [{ agent: "agent-a" }], weight: 2 }]] },
        },
      },
    },
  };
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});
function mount() {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <SessionChoiceSettings />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

it("saves the switch and spending while retaining the table and revision", async () => {
  mocks.request.mockResolvedValue({
    settings: EMPTY_CHOICE,
    revision: "revision-b",
    corrected: [],
  });
  mount();
  fireEvent.click(await screen.findByRole("switch", { name: "Session choice" }));
  fireEvent.change(screen.getByRole("combobox", { name: "Spending" }), {
    target: { value: "balance" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Save session choice" }));
  await waitFor(() => expect(mocks.request).toHaveBeenCalled());
  const body = JSON.parse(mocks.request.mock.calls[0][1].body);
  expect(body.expected_revision).toBe("revision-a");
  expect(body.settings).toMatchObject({
    enabled: true,
    strategy: "balance",
    presets: [{ id: "code" }],
    hosts: { "host-a": { work: [[{ weight: 2 }]] } },
  });
  expect(await screen.findByRole("status")).toHaveTextContent("Saved session choice.");
  expect(screen.queryByRole("link", { name: "Usage board" })).not.toBeInTheDocument();
});

it("shows a host without a strategy and keeps an unsaved change after a conflict", async () => {
  mocks.request.mockRejectedValue(
    new Error("Session choice changed. Read it again before saving."),
  );
  mount();
  fireEvent.change(await screen.findByRole("combobox", { name: "Host" }), {
    target: { value: "host-b" },
  });
  expect(
    screen.getByText("No strategy for Laptop. Creates use the current defaults."),
  ).toBeInTheDocument();
  fireEvent.click(screen.getByRole("switch", { name: "Session choice" }));
  fireEvent.click(screen.getByRole("button", { name: "Save session choice" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Read it again");
  expect(screen.getByRole("switch", { name: "Session choice" })).toBeChecked();
  fireEvent.click(screen.getByRole("button", { name: "Reload" }));
  expect(mocks.refetch).toHaveBeenCalledOnce();
});

it("offers a retry when the settings read fails", async () => {
  mocks.document.current = { isPending: false, refetch: mocks.refetch };
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "Retry" }));
  expect(mocks.refetch).toHaveBeenCalledOnce();
});

it("retains a dirty draft and its original revision when a background read changes", async () => {
  const view = mount();
  fireEvent.click(await screen.findByRole("switch", { name: "Session choice" }));
  mocks.document.current = {
    isPending: false,
    refetch: mocks.refetch,
    data: { revision: "changed-elsewhere", settings: structuredClone(EMPTY_CHOICE) },
  };
  view.rerender(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <SessionChoiceSettings />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  expect(screen.getByRole("switch", { name: "Session choice" })).toBeChecked();
  mocks.request.mockRejectedValueOnce(new Error("Conflict"));
  fireEvent.click(screen.getByRole("button", { name: "Save session choice" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Conflict");
  expect(JSON.parse(mocks.request.mock.calls[0][1].body).expected_revision).toBe("revision-a");
});

it("clears a draft to the empty document only when it is saved", async () => {
  mocks.request.mockResolvedValue({ settings: EMPTY_CHOICE, revision: "revision-b" });
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "Clear" }));
  expect(mocks.request).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Save session choice" }));
  await waitFor(() => expect(mocks.request).toHaveBeenCalled());
  expect(JSON.parse(mocks.request.mock.calls[0][1].body).settings).toEqual(EMPTY_CHOICE);
});

it("saves tile reserve and concurrency separately from the immediate floor control", async () => {
  mocks.meters.current = {
    meters: [
      {
        id: "account-a",
        display_name: "Coding plan",
        freshness: "fresh",
        windows: [
          {
            id: "7d",
            window_minutes: 10080,
            remaining_pct: 60,
            resets_at: Date.now() / 1000 + 4 * 86400,
          },
        ],
      },
    ] as ChoiceMeters["meters"],
    running: { "account-a": ["child-a"] },
    floors: {},
  };
  mocks.request.mockResolvedValue({ settings: EMPTY_CHOICE, revision: "revision-b" });
  mount();
  await screen.findByRole("switch", { name: "Session choice" });
  fireEvent.click(screen.getByText("Limits", { selector: "summary" }));
  fireEvent.change(screen.getByRole("combobox", { name: "Coding plan reserve mode" }), {
    target: { value: "daily" },
  });
  fireEvent.change(screen.getByRole("spinbutton", { name: "Coding plan reserve percent" }), {
    target: { value: "5" },
  });
  fireEvent.change(screen.getByRole("spinbutton", { name: "Coding plan max running" }), {
    target: { value: "2" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Save session choice" }));
  await screen.findByText("Saved session choice.");
  expect(JSON.parse(mocks.request.mock.calls[0][1].body).settings.limits).toEqual({
    "account-a": { reserve: { mode: "daily", window: "7d", pct: 5 }, maxRunning: 2 },
  });
  fireEvent.change(screen.getByRole("combobox", { name: "Coding plan floor" }), {
    target: { value: "manual" },
  });
  await waitFor(() => expect(mocks.metersRefetch).toHaveBeenCalledOnce());
  expect(mocks.request).toHaveBeenLastCalledWith(
    "/floor",
    expect.objectContaining({
      method: "POST",
      body: JSON.stringify({ tile: "account-a", off: true, keep: true }),
    }),
  );
});
