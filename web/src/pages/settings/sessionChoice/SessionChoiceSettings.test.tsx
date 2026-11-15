import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { usageBoardData } from "@/pages/usageBoard/usageBoardFixtures";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { SessionChoiceSettings } from "./SessionChoiceSettings";
import {
  ChoiceRequestError,
  EMPTY_CHOICE,
  type ChoiceCandidatePreview,
  type ChoiceEnvelope,
  type ChoiceMeters,
  type SessionChoiceDocument,
} from "./sessionChoiceData";
import type * as SessionChoiceData from "./sessionChoiceData";

const mocks = vi.hoisted(() => ({
  request: vi.fn(),
  refetch: vi.fn(),
  metersRefetch: vi.fn(),
  document: { current: undefined as unknown },
  lines: { current: { tiles: {} } as { tiles: Record<string, unknown> } },
  linesRefetch: vi.fn(),
  meters: { current: { meters: [], running: {}, floors: {} } as ChoiceMeters },
}));
vi.mock("./sessionChoiceData", async (original) => ({
  ...(await original<typeof SessionChoiceData>()),
  choiceRequest: mocks.request,
  useSessionChoiceDocument: () => mocks.document.current,
  useChoiceLines: () => ({
    ...useQuery({
      queryKey: ["sessionChoiceLines"],
      queryFn: mocks.linesRefetch,
      initialData: mocks.lines.current,
      staleTime: Infinity,
      retry: false,
    }),
    data: mocks.lines.current,
  }),
  useChoiceMeters: () => ({
    ...useQuery({
      queryKey: ["sessionChoiceMeters"],
      queryFn: mocks.metersRefetch,
      initialData: mocks.meters.current,
      staleTime: Infinity,
      retry: false,
    }),
    data: mocks.meters.current,
  }),
}));
vi.mock("./QuotaPreferencesSettings", () => ({
  QuotaPreferencesSettings: () => <div>Quota display controls</div>,
}));
vi.mock("./ChoicePolicySettings", () => ({ ChoicePolicySettings: () => null }));
vi.mock("@/hooks/useAvailableAgents", () => ({
  useAvailableAgents: () => ({
    data: [{ id: "agent-a", name: "agent-a", display_name: "Agent A", harness: "codex" }],
  }),
}));
vi.mock("@/lib/callingDefaultsApi", () => ({ listCallingDefaultCatalogs: async () => [] }));
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
  mocks.lines.current = { tiles: {} };
  mocks.meters.current = { meters: [], running: {}, floors: {} };
  mocks.refetch.mockResolvedValue({});
  mocks.metersRefetch.mockResolvedValue({});
  mocks.linesRefetch.mockResolvedValue({ tiles: {} });
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

it("previews conversion, saves its proof, then restores legacy constraints inside v2", async () => {
  const data = (mocks.document.current as { data: ChoiceEnvelope }).data;
  data.capabilities = { documentVersions: [1, 2], limitRules: true, effectiveLimits: true };
  data.settings!.limits = { pool: { maxRunning: 2 }, retained: { maxRunning: 1 } };
  const source = structuredClone(data.settings!);
  const rule = { id: "converted", agent: "agent-a", model: "model-x", maxRunning: 2 };
  const target = {
    ...source,
    schemaVersion: 2,
    limits: { retained: { maxRunning: 1 } },
    limitRules: [rule],
  };
  const plan = {
    sourceRevision: "revision-a",
    mappingFingerprint: "mapping",
    replacedPaths: [["limits", "pool"]],
    newRuleIds: ["converted"],
    newRules: [rule],
    unmapped: [{ path: ["limits", "retained"], reason: "shared_pool" }],
    convertible: true,
    settings: target,
  };
  mocks.request.mockImplementation(async (path, init) =>
    path === "/preview"
      ? { legacyConversion: plan }
      : { settings: JSON.parse(init.body).settings, revision: "revision-b" },
  );
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "Preview legacy conversion" }));
  fireEvent.click(await screen.findByRole("button", { name: "Apply conversion to draft" }));
  fireEvent.click(screen.getByRole("button", { name: "Save session choice" }));
  await screen.findByRole("button", { name: "Undo last conversion" });
  const savedCall = mocks.request.mock.calls.find(([, init]) => init.method === "PUT");
  expect(JSON.parse(savedCall![1].body).conversion_proof).toEqual({
    source_revision: "revision-a",
    mapping_fingerprint: "mapping",
    replaced_paths: [["limits", "pool"]],
    new_rule_ids: ["converted"],
  });
  fireEvent.click(screen.getByRole("button", { name: "Undo last conversion" }));
  fireEvent.click(screen.getByRole("button", { name: "Save session choice" }));
  await waitFor(() =>
    expect(mocks.request.mock.calls.filter(([, init]) => init.method === "PUT")).toHaveLength(2),
  );
  const undo = JSON.parse(mocks.request.mock.calls.at(-1)![1].body);
  expect(undo.settings).toMatchObject({ schemaVersion: 2, limits: source.limits, limitRules: [] });
  expect(undo.conversion_proof).toBeUndefined();
});

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
  expect(await screen.findByText("Saved session choice.")).toHaveAttribute("role", "status");
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
  const settings = (mocks.document.current as { data: ChoiceEnvelope }).data.settings!;
  settings.presets[0].links[0].meter = "account-a";
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
  mocks.request.mockImplementation(async (_path: string, init?: RequestInit) => ({
    settings: init?.method === "PUT" ? JSON.parse(init.body as string).settings : EMPTY_CHOICE,
    revision: "revision-b",
  }));
  mount();
  await screen.findByRole("switch", { name: "Session choice" });
  fireEvent.click(
    screen.getByText("Legacy pool limits and current readings", { selector: "summary" }),
  );
  expect(
    screen.queryByRole("combobox", { name: "Coding plan reserve mode" }),
  ).not.toBeInTheDocument();
  fireEvent.change(screen.getByRole("combobox", { name: "Add legacy pool limit" }), {
    target: { value: "account-a" },
  });
  fireEvent.change(screen.getByRole("combobox", { name: "Coding plan reserve mode" }), {
    target: { value: "daily" },
  });
  fireEvent.change(screen.getByRole("spinbutton", { name: "Coding plan warn percent" }), {
    target: { value: "5" },
  });
  fireEvent.change(screen.getByRole("spinbutton", { name: "Coding plan max running" }), {
    target: { value: "2" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Save session choice" }));
  await screen.findByText("Saved session choice.");
  expect(JSON.parse(mocks.request.mock.calls[0][1].body).settings.limits).toEqual({
    "account-a": { reserve: { mode: "daily", window: "7d", pct: 5, stopPct: null }, maxRunning: 2 },
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

function quotaFixture() {
  mocks.meters.current = {
    meters: [
      {
        id: "plan",
        display_name: "Plan",
        freshness: "fresh",
        windows: [
          {
            id: "7d",
            label: "Weekly",
            window_minutes: 10080,
            remaining_pct: 5,
            resets_at: Date.now() / 1000 + 2.5 * 86400,
          },
        ],
      },
    ] as ChoiceMeters["meters"],
    running: { plan: ["wrong-count"] },
    floors: {},
  };
  mocks.lines.current = {
    tiles: {
      plan: {
        state: "warn",
        warn_line_pct: 12,
        stop_line_pct: 3,
        released: false,
        running: ["one", "two"],
      },
    },
  };
  mocks.document.current = {
    isPending: false,
    refetch: mocks.refetch,
    data: {
      revision: "a",
      settings: {
        ...structuredClone(EMPTY_CHOICE),
        limits: { plan: { reserve: { mode: "daily", window: "7d", pct: 4, stopPct: 1 } } },
      },
    },
  };
  mocks.request.mockResolvedValue({ settings: EMPTY_CHOICE, revision: "b" });
}
it("shows server lines, rounds draft days upward and uses enforcement targets for the cut count", async () => {
  quotaFixture();
  mount();
  await screen.findByRole("switch", { name: "Session choice" });
  fireEvent.click(
    screen.getByText("Legacy pool limits and current readings", { selector: "summary" }),
  );
  expect(screen.getByText("Warn: 12%")).toBeInTheDocument();
  expect(screen.getByText("Stop: 3%")).toBeInTheDocument();
  expect(screen.getByText("Warning", { exact: true })).toBeInTheDocument();
  expect(screen.queryByText(/Saving cuts/)).not.toBeInTheDocument();
  fireEvent.change(screen.getByRole("spinbutton", { name: "Plan stop percent" }), {
    target: { value: "2" },
  });
  expect(screen.getByText("Saving cuts 2 running sessions")).toBeInTheDocument();
  // Saved lines stay server-owned while the draft changes.
  expect(screen.getByText("Stop: 3%")).toBeInTheDocument();
});
it.each(["warn", "stop", "both"])(
  "clears %s inputs without turning blank into zero",
  async (which) => {
    quotaFixture();
    mount();
    await screen.findByRole("switch", { name: "Session choice" });
    fireEvent.click(
      screen.getByText("Legacy pool limits and current readings", { selector: "summary" }),
    );
    for (const field of which === "both" ? ["warn", "stop"] : [which])
      fireEvent.change(screen.getByRole("spinbutton", { name: `Plan ${field} percent` }), {
        target: { value: "" },
      });
    fireEvent.click(screen.getByRole("button", { name: "Save session choice" }));
    await screen.findByText("Saved session choice.");
    const reserve = JSON.parse(mocks.request.mock.calls[0][1].body).settings.limits.plan.reserve;
    expect(reserve).toEqual(
      which === "both"
        ? null
        : {
            mode: "daily",
            window: "7d",
            pct: which === "warn" ? null : 4,
            stopPct: which === "stop" ? null : 1,
          },
    );
  },
);
it("loads a legacy warning and accepts an explicit zero stop", async () => {
  quotaFixture();
  const doc = mocks.document.current as {
    data: { settings: { limits: { plan: { reserve: { stopPct?: number } } } } };
  };
  delete doc.data.settings.limits.plan.reserve.stopPct;
  mount();
  await screen.findByRole("switch", { name: "Session choice" });
  fireEvent.click(
    screen.getByText("Legacy pool limits and current readings", { selector: "summary" }),
  );
  expect(screen.getByRole("spinbutton", { name: "Plan warn percent" })).toHaveValue(4);
  expect(screen.getByRole("spinbutton", { name: "Plan stop percent" })).toHaveValue(null);
  fireEvent.change(screen.getByRole("spinbutton", { name: "Plan stop percent" }), {
    target: { value: "0" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Save session choice" }));
  await screen.findByText("Saved session choice.");
  expect(JSON.parse(mocks.request.mock.calls[0][1].body).settings.limits.plan.reserve.stopPct).toBe(
    0,
  );
});

it.each(["no targets", "released", "stale"])("omits the cut notice with %s", async (condition) => {
  quotaFixture();
  if (condition === "no targets") mocks.lines.current.tiles.plan = { running: [] };
  if (condition === "released")
    mocks.meters.current.floors.plan = { until: "manual", active: true };
  if (condition === "stale") mocks.meters.current.meters[0].freshness = "old";
  mount();
  await screen.findByRole("switch", { name: "Session choice" });
  fireEvent.click(
    screen.getByText("Legacy pool limits and current readings", { selector: "summary" }),
  );
  fireEvent.change(screen.getByRole("spinbutton", { name: "Plan stop percent" }), {
    target: { value: "2" },
  });
  expect(screen.queryByText(/Saving cuts/)).not.toBeInTheDocument();
});

function scopedFixture() {
  const settings = {
    ...structuredClone(EMPTY_CHOICE),
    schemaVersion: 2,
    limitRules: [
      { id: "base", agent: "agent-a", maxRunning: 3 },
      { id: "model", agent: "agent-a", model: "model-x", maxRunning: null, reserve: { pct: null } },
    ],
    limits: { legacy: { maxRunning: 2 } },
    futureField: { keep: true },
  };
  mocks.document.current = {
    isPending: false,
    refetch: mocks.refetch,
    data: {
      revision: "scoped-a",
      settings,
      capabilities: { documentVersions: [1, 2], limitRules: true, effectiveLimits: true },
    },
  };
  return settings;
}

it("saves explicit unlimited and inherited fields after a visible reorder, retaining legacy data", async () => {
  const settings = scopedFixture();
  mocks.request.mockResolvedValue({ settings, revision: "scoped-b" });
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "Move rule 2 up" }));
  fireEvent.click(screen.getByRole("button", { name: "Save session choice" }));
  await waitFor(() => expect(mocks.request).toHaveBeenCalled());
  const body = JSON.parse(mocks.request.mock.calls[0][1].body);
  expect(body.expected_revision).toBe("scoped-a");
  expect(body.settings.limitRules[0]).toEqual(settings.limitRules[1]);
  expect(body.settings.limits).toEqual(settings.limits);
  expect(body.settings.futureField).toEqual({ keep: true });
});

it("edits null to inheritance and preserves dirty rules after a structured 422", async () => {
  scopedFixture();
  mocks.request.mockRejectedValue(
    new ChoiceRequestError(422, { code: "invalid_limits", fields: ["reserve.window"] }),
  );
  mount();
  fireEvent.click(await screen.findByText("Edit rule 2"));
  fireEvent.change(
    within(screen.getByRole("article", { name: "Rule 2" })).getByRole("combobox", {
      name: "Max running setting",
    }),
    { target: { value: "inherit" } },
  );
  fireEvent.click(screen.getByRole("button", { name: "Save session choice" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("reserve.window");
  const body = JSON.parse(mocks.request.mock.calls[0][1].body);
  expect(body.settings.limitRules[1]).not.toHaveProperty("maxRunning");
  expect(body.settings.limitRules[1].reserve).toEqual({ pct: null });
  expect(
    within(screen.getByRole("article", { name: "Rule 2" })).getByRole("combobox", {
      name: "Max running setting",
    }),
  ).toHaveValue("inherit");
});

it("drags a rule by keyboard with visible insertion feedback and saves that exact order", async () => {
  const settings = scopedFixture();
  mocks.request.mockResolvedValue({ settings, revision: "scoped-b" });
  mount();
  const handle = await screen.findByRole("button", { name: "Drag rule 2" });
  handle.focus();
  fireEvent.keyDown(handle, { key: " " });
  fireEvent.keyDown(handle, { key: "Home" });
  expect(screen.getByRole("status", { name: "Drag result" })).toHaveTextContent(
    "Move rule 2 before rule 1",
  );
  fireEvent.keyDown(handle, { key: "Enter" });
  await waitFor(() => expect(screen.getByRole("button", { name: "Drag rule 1" })).toHaveFocus());
  expect(screen.getByRole("article", { name: "Rule 1" })).toHaveTextContent("model-x");
  fireEvent.click(screen.getByRole("button", { name: "Save session choice" }));
  await waitFor(() => expect(mocks.request).toHaveBeenCalled());
  const body = JSON.parse(mocks.request.mock.calls[0][1].body);
  expect(body.settings.limitRules).toEqual([settings.limitRules[1], settings.limitRules[0]]);
  expect(body.settings.limits).toEqual(settings.limits);
});

it("preserves unsupported documents and disables writes", async () => {
  mocks.document.current = {
    isPending: false,
    refetch: mocks.refetch,
    data: { revision: "future", settings: { schemaVersion: 9 } },
  };
  mount();
  expect(await screen.findByRole("alert")).toHaveTextContent("Unsupported version: 9");
  expect(screen.queryByRole("button", { name: "Save session choice" })).not.toBeInTheDocument();
  expect(mocks.request).not.toHaveBeenCalled();
});

it("adds and removes explicit rules without deleting old pool constraints", async () => {
  const settings = scopedFixture();
  mocks.request.mockResolvedValue({ settings, revision: "scoped-b" });
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "Add rule" }));
  fireEvent.click(screen.getByRole("button", { name: "Remove rule 1" }));
  fireEvent.click(screen.getByRole("button", { name: "Save session choice" }));
  await waitFor(() => expect(mocks.request).toHaveBeenCalled());
  const body = JSON.parse(mocks.request.mock.calls[0][1].body);
  expect(body.settings.limitRules).toHaveLength(2);
  expect(body.settings.limitRules[0].id).toBe("model");
  expect(body.settings.limitRules[1]).toMatchObject({ agent: "agent-a" });
  expect(body.settings.limitRules[1]).not.toHaveProperty("maxRunning");
  expect(body.settings.limits).toEqual(settings.limits);
});

function previewFixture() {
  quotaFixture();
  const settings: SessionChoiceDocument = {
    ...scopedFixture(),
    limits: { plan: { reserve: { mode: "total", window: "7d", pct: 4, stopPct: 1 } } },
  };
  (mocks.document.current as { data: ChoiceEnvelope }).data.settings = settings;
  return settings;
}

function candidateResult(state: "blocked" | "passes_checks" = "blocked"): ChoiceCandidatePreview {
  return {
    mode: "candidate",
    candidate: { agent_id: "agent-a", host_id: "host-a", model: "model-x" },
    policyActive: true,
    configuredInTiers: true,
    effectiveLimits: {},
    legacyChecks: { pool: "plan", limits: null, linkMaxRunning: null },
    check: { state, reason: null },
    admission: false,
    savedRevision: "scoped-a",
    draftFingerprint: "draft-a",
  };
}

async function openPreview() {
  fireEvent.click(await screen.findByText("Check a combination"));
  fireEvent.change(screen.getByRole("combobox", { name: "Preview agent" }), {
    target: { value: "agent-a" },
  });
  fireEvent.change(screen.getByRole("combobox", { name: "Preview host" }), {
    target: { value: "host-a" },
  });
  fireEvent.click(
    screen.getByText("Legacy pool limits and current readings", { selector: "summary" }),
  );
}

it.each(["warn", "stop", "both"])(
  "previews and saves the same canonical document after clearing the last legacy %s threshold",
  async (which) => {
    const settings = previewFixture();
    if (which === "warn") settings.limits.plan.reserve!.stopPct = null;
    if (which === "stop") settings.limits.plan.reserve!.pct = null;
    mocks.request.mockImplementation(async (path: string) =>
      path === "/preview" ? candidateResult() : { settings, revision: "scoped-b" },
    );
    mount();
    await openPreview();
    for (const field of which === "both" ? ["warn", "stop"] : [which])
      fireEvent.change(screen.getByRole("spinbutton", { name: `Plan ${field} percent` }), {
        target: { value: "" },
      });
    fireEvent.click(screen.getByRole("button", { name: "Check draft" }));
    await screen.findByText(/model-x · Blocked/);
    fireEvent.click(screen.getByRole("button", { name: "Save session choice" }));
    await screen.findByText("Saved session choice.");
    const preview = JSON.parse(
      mocks.request.mock.calls.find(([path]) => path === "/preview")![1].body,
    );
    const save = JSON.parse(mocks.request.mock.calls.find(([path]) => path === "")![1].body);
    expect(save.settings.limits.plan.reserve).toBeNull();
    expect(preview.settings).toEqual(save.settings);
    expect(preview.settings.limitRules).toEqual(settings.limitRules);
    expect(preview.settings.limitRules[0]).not.toHaveProperty("reserve");
    expect(preview.settings.limitRules[1].reserve).toEqual({ pct: null });
  },
);

it.each(["release", "reenable"])(
  "invalidates a completed candidate on successful floor %s even if reading refresh fails",
  async (direction) => {
    previewFixture();
    if (direction === "reenable")
      mocks.meters.current.floors.plan = { active: true, until: "manual" };
    mocks.request.mockImplementation(async (path: string) =>
      path === "/preview"
        ? candidateResult(direction === "release" ? "blocked" : "passes_checks")
        : {},
    );
    mocks.metersRefetch.mockRejectedValueOnce(new Error("reading refresh failed"));
    mount();
    await openPreview();
    fireEvent.click(screen.getByRole("button", { name: "Check draft" }));
    const oldResult =
      direction === "release" ? /model-x · Blocked/ : /model-x · Passes current checks/;
    await screen.findByText(oldResult);
    fireEvent.change(screen.getByRole("combobox", { name: "Plan floor" }), {
      target: { value: direction === "release" ? "manual" : "kept" },
    });
    await screen.findByText(/reading refresh failed/);
    expect(screen.queryByText(oldResult)).not.toBeInTheDocument();
    expect(screen.getByText(/Check again/)).toBeInTheDocument();
  },
);

it("invalidates an in-flight candidate before floor refetch completes and ignores its late old result", async () => {
  previewFixture();
  let resolveOld!: (result: ChoiceCandidatePreview) => void;
  let resolveRefresh!: () => void;
  mocks.request.mockImplementation((path: string) =>
    path === "/preview"
      ? new Promise<ChoiceCandidatePreview>((resolve) => {
          resolveOld = resolve;
        })
      : Promise.resolve({}),
  );
  mocks.metersRefetch.mockImplementationOnce(
    () =>
      new Promise<void>((resolve) => {
        resolveRefresh = resolve;
      }),
  );
  mount();
  await openPreview();
  fireEvent.click(screen.getByRole("button", { name: "Check draft" }));
  await waitFor(() => expect(mocks.request).toHaveBeenCalledWith("/preview", expect.anything()));
  fireEvent.change(screen.getByRole("combobox", { name: "Plan floor" }), {
    target: { value: "manual" },
  });
  await waitFor(() => expect(mocks.metersRefetch).toHaveBeenCalledOnce());
  expect(screen.getByRole("button", { name: "Check draft" })).not.toBeDisabled();
  mocks.request.mockResolvedValueOnce(candidateResult("passes_checks"));
  fireEvent.click(screen.getByRole("button", { name: "Check draft" }));
  await screen.findByText(/model-x · Passes current checks/);
  await act(async () => {
    resolveOld(candidateResult("blocked"));
    resolveRefresh();
  });
  expect(screen.queryByText(/model-x · Blocked/)).not.toBeInTheDocument();
  expect(screen.getByText(/model-x · Passes current checks/)).toBeInTheDocument();
});

it("uses one window option set and readable values, disabling unreported windows per meter", async () => {
  quotaFixture();
  const second = structuredClone(mocks.meters.current.meters[0]);
  second.id = "other";
  second.display_name = "Other plan";
  second.windows = [{ ...second.windows[0], id: "5h", window_minutes: 300, label: "Short" }];
  mocks.meters.current.meters.push(second);
  const settings = (mocks.document.current as { data: ChoiceEnvelope }).data.settings!;
  settings.limits.other = {
    reserve: { mode: "total", window: "missing-window-with-long-name", pct: 2 },
  };
  mount();
  fireEvent.click(
    await screen.findByText("Legacy pool limits and current readings", { selector: "summary" }),
  );
  const first = screen.getByRole("combobox", { name: "Plan reserve window" });
  const other = screen.getByRole("combobox", { name: "Other plan reserve window" });
  const options = (element: HTMLElement) =>
    within(element)
      .getAllByRole("option")
      .map((item) => [item.getAttribute("value"), item.textContent]);
  expect(options(first)).toEqual(options(other));
  expect(within(first).getByRole("option", { name: "5h" })).toBeDisabled();
  expect(within(other).getByRole("option", { name: "7d" })).toBeDisabled();
  expect(other).toHaveAttribute("title", "missing-window-with-long-name");
  expect(screen.getByText(/This meter does not report the selected window/)).toHaveTextContent(
    "Choose an enabled quota window",
  );
});

it("opens a listed rule editor by clicking its title", async () => {
  scopedFixture();
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "Rule 2 · Specificity 2" }));
  expect(screen.getByText("Edit rule 2").closest("details")).toHaveAttribute("open");
  expect(
    within(screen.getByRole("article", { name: "Rule 2" })).getByRole("combobox", {
      name: "Max running setting",
    }),
  ).toBeVisible();
});

it("keeps configured offline pool limits visible and removes only the selected draft row", async () => {
  quotaFixture();
  const settings = (mocks.document.current as { data: ChoiceEnvelope }).data.settings!;
  settings.limits.offline = { maxRunning: 2 };
  mount();
  fireEvent.click(
    await screen.findByText("Legacy pool limits and current readings", { selector: "summary" }),
  );
  expect(screen.getByRole("spinbutton", { name: "offline max running" })).toHaveValue(2);
  fireEvent.click(screen.getByRole("button", { name: "Remove offline legacy limit" }));
  expect(screen.queryByRole("spinbutton", { name: "offline max running" })).not.toBeInTheDocument();
  expect(screen.getByRole("spinbutton", { name: "Plan max running" })).toBeInTheDocument();
  expect(mocks.request).not.toHaveBeenCalled();
});

it("offers only document-referenced meters and generated pools for new legacy rules", async () => {
  const tile = usageBoardData().accounts[0];
  const settings = (mocks.document.current as { data: ChoiceEnvelope }).data.settings!;
  settings.presets[0].links = [{ agent: "agent-a", model: "model-x" }];
  settings.hosts["host-b"] = {
    work: [[{ links: [{ agent: "agent-a", model: "model-y", meter: "shared" }] }]],
  };
  mocks.meters.current = {
    products: { codex: "codex", "codex-native": "codex" },
    meters: [
      { ...tile, id: "codex:model-x", display_name: "Model X", windows: [], freshness: "none" },
      { ...tile, id: "shared", display_name: "Shared plan", windows: [] },
      { ...tile, id: "gw-unrelated", display_name: "Unrelated account", windows: [] },
    ],
    running: {},
    floors: {},
  };
  mount();
  fireEvent.click(
    await screen.findByText("Legacy pool limits and current readings", { selector: "summary" }),
  );
  const picker = screen.getByRole("combobox", { name: "Add legacy pool limit" });
  expect(
    within(picker).queryByRole("option", { name: "Unrelated account" }),
  ).not.toBeInTheDocument();
  expect(within(picker).getByRole("option", { name: "Model X" })).toHaveValue("codex:model-x");
  expect(within(picker).getByRole("option", { name: "Shared plan" })).toHaveValue("shared");
  // A referenced generated pool stays selectable even before its first usage report.
  expect(within(picker).getByRole("option", { name: "codex:" })).toHaveValue("codex:");
  fireEvent.change(picker, { target: { value: "shared" } });
  expect(screen.getByRole("spinbutton", { name: "Shared plan max running" })).toBeVisible();
  expect(
    screen.queryByRole("spinbutton", { name: "Unrelated account max running" }),
  ).not.toBeInTheDocument();
});

it("retains saved unreferenced limits with a note and saves their values unchanged", async () => {
  quotaFixture();
  const settings = (mocks.document.current as { data: ChoiceEnvelope }).data.settings!;
  settings.limits.offline = { maxRunning: 2 };
  mount();
  fireEvent.click(
    await screen.findByText("Legacy pool limits and current readings", { selector: "summary" }),
  );
  for (const name of ["Plan", "offline"]) {
    const row = screen.getByRole("spinbutton", { name: `${name} max running` }).closest("tr")!;
    expect(
      within(row).getByText("Not referenced by current links. Saved limit retained."),
    ).toBeVisible();
  }
  fireEvent.click(screen.getByRole("button", { name: "Save session choice" }));
  await screen.findByText("Saved session choice.");
  expect(JSON.parse(mocks.request.mock.calls[0][1].body).settings.limits).toEqual(settings.limits);
});
