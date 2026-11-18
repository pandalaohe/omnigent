import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { authenticatedFetch } from "@/lib/identity";
import type * as Identity from "@/lib/identity";
import { SessionChoiceSettings } from "./SessionChoiceSettings";
import { EMPTY_CHOICE, type ChoiceEnvelope, type SessionChoiceDocument } from "./sessionChoiceData";

vi.mock("@/lib/identity", async (original) => ({
  ...(await original<typeof Identity>()),
  authenticatedFetch: vi.fn(),
}));
vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({ usage_board_enabled: false }),
}));
vi.mock("@/hooks/useHosts", () => ({
  useHosts: () => ({
    data: [{ host_id: "host-a", name: "Desktop", configured_harnesses: { codex: true } }],
  }),
  useHostModelOptions: () => ({ data: [] }),
}));
vi.mock("@/hooks/useAvailableAgents", () => ({
  useAvailableAgents: () => ({
    data: [{ id: "agent-a", name: "agent-a", display_name: "Agent A", harness: "codex" }],
  }),
}));
vi.mock("@/lib/callingDefaultsApi", () => ({ listCallingDefaultCatalogs: async () => [] }));
vi.mock("./QuotaPreferencesSettings", () => ({ QuotaPreferencesSettings: () => null }));

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

async function tick(ms = 20) {
  await act(async () => vi.advanceTimersByTimeAsync(ms));
}

it("follows the reordered draft row on each runtime poll and saves against the draft's settings revision", async () => {
  vi.useFakeTimers();
  vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
  const fallback = {
    agent: "agent-a",
    model: "réserve-a",
    maxRunning: 2,
    fallbackMode: "drain",
  } as const;
  const settings: SessionChoiceDocument = {
    ...structuredClone(EMPTY_CHOICE),
    enabled: true,
    defaultWorkType: "work",
    workTypes: [{ id: "work", label: "Work" }],
    hosts: {
      "host-a": {
        work: [
          [
            { name: "Primary", links: [{ agent: "agent-a", model: "head-a" }, fallback] },
            { name: "Other", links: [{ agent: "agent-a", model: "head-b" }, fallback] },
          ],
        ],
      },
    },
  };
  const lock = {
    hostId: "host-a",
    workType: "work",
    tierIndex: 0,
    rowPosition: 0,
    savedLink: fallback,
    // Produced by the server's sorted-key UTF-8 link digests and sorted row digest.
    rowIdentity: "71188c91fd0546921a8253009116227b6bb3e91fa06297f52cf7457163c60c27",
    settingsRevision: "rev-a",
    since: new Date(2026, 0, 1, 14, 20).getTime() / 1000,
  };
  let saved: ChoiceEnvelope = {
    settings,
    revision: "rev-a",
    runtime: { settingsRevision: "rev-a", drainLocks: [lock] },
  };
  const writes: { settings: SessionChoiceDocument; expected_revision: string }[] = [];
  vi.mocked(authenticatedFetch).mockImplementation(async (path, init) => {
    if (path === "/v1/me/session-choice") {
      if (init?.method !== "PUT") return Response.json(saved);
      const write = JSON.parse(String(init.body)) as (typeof writes)[number];
      writes.push(write);
      if (write.expected_revision !== saved.revision) {
        return Response.json(
          { detail: "Session choice changed. Read it again before saving." },
          { status: 409 },
        );
      }
      saved = { settings: write.settings, revision: "rev-b" };
      return Response.json(saved);
    }
    if (String(path).startsWith("/v1/me/session-choice/meters?")) {
      return Response.json({ meters: [], running: {}, floors: {} });
    }
    if (path === "/v1/me/usage/lines?running=1") return Response.json({ tiles: {} });
    throw new Error(`Unexpected request ${path}`);
  });

  const client = new QueryClient();
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <SessionChoiceSettings />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  await tick();

  const openRow = (name: "Primary" | "Other") =>
    fireEvent.click(screen.getByRole("button", { name: new RegExp(`^${name}.*Agent A`) }));
  openRow("Primary");
  expect(screen.getByText("Draining since 14:20")).toBeVisible();
  const handle = screen.getByRole("button", { name: "Drag Primary in Tier 1" });
  fireEvent.keyDown(handle, { key: " " });
  fireEvent.keyDown(handle, { key: "End" });
  fireEvent.keyDown(handle, { key: "Enter" });
  openRow("Other");
  expect(screen.queryByText(/Draining since/)).toBeNull();
  openRow("Primary");
  expect(screen.getByText("Draining since 14:20")).toBeVisible();

  saved = {
    ...saved,
    runtime: { settingsRevision: "rev-a", drainLocks: [{ ...lock, since: lock.since + 60 }] },
  };
  await tick(60_000);
  expect(screen.getByText("Draining since 14:21")).toBeVisible();
  expect(writes).toHaveLength(0);

  saved = {
    ...saved,
    runtime: { settingsRevision: "rev-a", drainLocks: [{ ...lock, rowPosition: 1 }] },
  };
  await tick(60_000);
  expect(screen.queryByText(/Draining since/)).toBeNull();

  const save = () => fireEvent.click(screen.getByRole("button", { name: "Save session choice" }));
  save();
  await tick();
  expect(screen.getByText("Saved session choice.")).toBeVisible();
  expect(writes[0]).toMatchObject({ expected_revision: "rev-a" });
  expect(writes[0].settings.hosts["host-a"].work[0].map((row) => row.name)).toEqual([
    "Other",
    "Primary",
  ]);
  expect(writes[0]).not.toHaveProperty("runtime");

  fireEvent.click(
    within(screen.getByRole("radiogroup", { name: "Fallback 1" })).getByRole("radio", {
      name: "Return",
    }),
  );
  saved = { settings: { ...saved.settings!, strategy: "balance" }, revision: "rev-c" };
  await tick(60_000);
  save();
  await tick();
  expect(writes[1]).toMatchObject({ expected_revision: "rev-b" });
  expect(writes[1].settings.hosts["host-a"].work[0][1].links[1]).not.toHaveProperty("fallbackMode");
  expect(screen.getByRole("alert")).toHaveTextContent(
    "Session choice changed. Read it again before saving.",
  );
  client.clear();
});
