// The Monitor settings panel on /system: the workspace's default load lines and
// the child-session queue switch, the ops health check, and the monitor's own
// cost as a table.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/hooks/useAgents", () => ({
  useAgents: () => ({
    data: [{ id: "ca_joint_1", name: "joint", display_name: "Ops joint" }],
  }),
}));

import { MonitorSettingsDialog } from "./MonitorSettings";

const MB = 1024 * 1024;

const fetchMock = vi.fn();

function mockResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? "OK" : "Error",
    json: async () => body,
  } as unknown as Response;
}

const SETTINGS = {
  cpu_pct: 85,
  cpu_sustain_min: 10,
  mem_pct: 90,
  disk_pct: 90,
  server_5xx_pct: 5,
  health_check: { project_id: null, host_id: null, prompt: null },
  load_lines: {
    default: { heavy: 4, new_session: 5 },
    hosts: { host_1: { heavy: 3, new_session: 6 } },
    queue_enabled: true,
  },
  default_health_check_prompt: "Default prompt.",
};

const PROJECTS = [{ id: "project_1", name: "Ops" }];

const HOSTS = {
  hosts: [
    { host_id: "host_1", name: "Worker", owner: "alice", status: "online" },
    { host_id: "host_2", name: "Old Mac", owner: "alice", status: "offline" },
  ],
};

const RESOLVE = {
  agent_id: "ca_joint_1",
  harness: "claude-native",
  model: "claude-sonnet",
  effort: null,
  sources: {},
  problems: [
    {
      field: "agent",
      setting: "default_agent",
      message: "A saved joint agent cannot be launched without an agent id.",
    },
  ],
};

const OVERHEAD = {
  server: { cpu_pct: 0.1, mem_estimate_bytes: 6 * MB, estimated: true },
  hosts: { host_1: { cpu_pct: 0.2, rss_delta: 18 * MB } },
};

function defaultFetch(input: RequestInfo | URL, _init?: RequestInit): Response {
  const url = String(input);
  if (url === "/v1/system/settings") return mockResponse(SETTINGS);
  if (url === "/v1/sessions/projects") return mockResponse(PROJECTS);
  if (url === "/v1/hosts") return mockResponse(HOSTS);
  return mockResponse({}, 404);
}

function renderPanel(open = true, onOpenChange = vi.fn()) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <MonitorSettingsDialog
          open={open}
          onOpenChange={onOpenChange}
          overhead={OVERHEAD}
          hostNames={new Map([["host_1", "Worker"]])}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function openSelect(testId: string) {
  const trigger = screen.getByTestId(testId);
  fireEvent.pointerDown(trigger, new MouseEvent("pointerdown", { bubbles: true, button: 0 }));
  fireEvent.click(trigger);
}

function settingsPutCall() {
  return fetchMock.mock.calls.find(
    ([input, init]) =>
      String(input) === "/v1/system/settings" &&
      (init as RequestInit | undefined)?.method === "PUT",
  );
}

beforeEach(() => {
  fetchMock.mockReset();
  fetchMock.mockImplementation(defaultFetch);
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("MonitorSettingsDialog", () => {
  it("reads nothing until the panel opens", () => {
    renderPanel(false);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("loads the workspace default lines and the queue switch", async () => {
    renderPanel();

    const form = await screen.findByTestId("load-lines-form");
    expect(within(form).getByLabelText("Heavy work (per core)")).toHaveValue(4);
    expect(within(form).getByLabelText("No new session (per core)")).toHaveValue(5);
    expect(within(form).getByRole("switch", { name: "Queue new child sessions" })).toBeChecked();
    expect(within(form).getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("saves the default pair and the queue switch without touching host overrides", async () => {
    renderPanel();

    const form = await screen.findByTestId("load-lines-form");
    fireEvent.change(within(form).getByLabelText("Heavy work (per core)"), {
      target: { value: "3" },
    });
    fireEvent.change(within(form).getByLabelText("No new session (per core)"), {
      target: { value: "4.5" },
    });
    fireEvent.click(within(form).getByRole("switch", { name: "Queue new child sessions" }));
    fireEvent.click(within(form).getByRole("button", { name: "Save" }));

    await waitFor(() => expect(settingsPutCall()).toBeDefined());
    const putInit = settingsPutCall()?.[1] as RequestInit;
    expect(JSON.parse(String(putInit.body))).toEqual({
      load_lines: { default: { heavy: 3, new_session: 4.5 }, queue_enabled: false },
    });
  });

  it.each([
    ["5", "5", "The heavy-work line must be below the new-session line."],
    ["6", "5", "The heavy-work line must be below the new-session line."],
    ["0", "5", "The heavy-work line must be above 0."],
    ["", "5", "Enter both lines as numbers."],
  ])("refuses heavy %s / no-new-session %s before sending ", async (heavy, newSession, message) => {
    renderPanel();

    const form = await screen.findByTestId("load-lines-form");
    fireEvent.change(within(form).getByLabelText("Heavy work (per core)"), {
      target: { value: heavy },
    });
    fireEvent.change(within(form).getByLabelText("No new session (per core)"), {
      target: { value: newSession },
    });
    fireEvent.click(within(form).getByRole("button", { name: "Save" }));

    expect(await within(form).findByRole("alert")).toHaveTextContent(message);
    expect(settingsPutCall()).toBeUndefined();
  });

  it("leaves the load-line form out when the server predates load lines", async () => {
    const { load_lines: _omitted, ...olderServer } = SETTINGS;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) =>
      String(input) === "/v1/system/settings"
        ? mockResponse(olderServer)
        : defaultFetch(input, init),
    );
    renderPanel();

    expect(await screen.findByTestId("health-check-form")).toBeInTheDocument();
    expect(screen.queryByTestId("load-lines-form")).toBeNull();
  });

  it("shows the monitor's own cost as a table, with no bare dash", async () => {
    renderPanel();

    const table = await screen.findByTestId("monitor-overhead-table");
    expect(within(table).getByText("Server")).toBeInTheDocument();
    expect(within(table).getByText("Worker")).toBeInTheDocument();
    expect(within(table).getByText("6.0 MB (estimate)")).toBeInTheDocument();
    expect(within(table).getByText("18 MB below start")).toBeInTheDocument();
    expect(table).not.toHaveTextContent("—");
  });

  it("resolves the agent, lists resolve problems, and saves a reset prompt as null", async () => {
    const stored = {
      ...SETTINGS,
      health_check: { project_id: null, host_id: null, prompt: "Old custom prompt" },
    };
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/v1/system/settings" && init?.method !== "PUT") return mockResponse(stored);
      if (url.startsWith("/v1/calling-defaults/resolve")) return mockResponse(RESOLVE);
      return defaultFetch(input, init);
    });
    renderPanel();

    expect(await screen.findByLabelText("Prompt")).toHaveValue("Old custom prompt");

    openSelect("health-check-project-trigger");
    fireEvent.click(await screen.findByRole("option", { name: "Ops" }));
    openSelect("health-check-host-trigger");
    fireEvent.click(await screen.findByRole("option", { name: "Worker" }));

    expect(await screen.findByText("Ops joint")).toBeInTheDocument();
    expect(
      screen.getByText("A saved joint agent cannot be launched without an agent id."),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Reset to default" }));
    const form = screen.getByTestId("health-check-form");
    fireEvent.click(within(form).getByRole("button", { name: "Save" }));

    await waitFor(() => expect(settingsPutCall()).toBeDefined());
    const putInit = settingsPutCall()?.[1] as RequestInit;
    expect(JSON.parse(String(putInit.body))).toEqual({
      health_check: { project_id: "project_1", host_id: "host_1", prompt: null },
    });
  });
});
