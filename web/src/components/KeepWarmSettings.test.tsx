import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import { KEEP_WARM_STORAGE_KEY } from "@/lib/keepWarmPreferences";
import { KeepWarmSettings } from "./KeepWarmSettings";

const { queuePatchMock, prefetchMock, mocks } = vi.hoisted(() => ({
  queuePatchMock: vi.fn(),
  prefetchMock: vi.fn(),
  mocks: {
    agents: [] as AvailableAgent[],
    isLoading: false,
    isPlaceholderData: false,
  },
}));

vi.mock("@/lib/userPreferencesSync", () => ({ queueUserPreferencePatch: queuePatchMock }));

vi.mock("@/hooks/useAvailableAgents", () => ({
  useAvailableAgents: () => ({
    data: mocks.agents,
    isLoading: mocks.isLoading,
    isPlaceholderData: mocks.isPlaceholderData,
  }),
  prefetchAvailableAgentDetails: prefetchMock,
}));

const claudeNative: AvailableAgent = {
  id: "claude-native-ui",
  name: "claude-native-ui",
  display_name: "Claude Code",
  description: null,
  harness: "claude-native",
  skills: [],
};

const codexNative: AvailableAgent = {
  id: "codex-native-ui",
  name: "codex-native-ui",
  display_name: "Codex",
  description: null,
  harness: "codex-native",
  skills: [],
};

const SUPPORTED_AGENTS: AvailableAgent[] = [
  claudeNative,
  codexNative,
  {
    id: "opencode-native-ui",
    name: "opencode-native-ui",
    display_name: "OpenCode",
    description: null,
    harness: "opencode-native",
    skills: [],
  },
];

function storedKeepWarm() {
  return JSON.parse(localStorage.getItem(KEEP_WARM_STORAGE_KEY) ?? "null");
}

function renderSettings() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity } },
  });
  const view = render(
    <QueryClientProvider client={client}>
      <KeepWarmSettings />
    </QueryClientProvider>,
  );
  const rerenderSettings = () =>
    view.rerender(
      <QueryClientProvider client={client}>
        <KeepWarmSettings />
      </QueryClientProvider>,
    );
  return { ...view, client, rerenderSettings };
}

beforeEach(() => {
  localStorage.clear();
  mocks.agents = [...SUPPORTED_AGENTS];
  mocks.isLoading = false;
  mocks.isPlaceholderData = false;
});
afterEach(() => {
  cleanup();
  queuePatchMock.mockReset();
  prefetchMock.mockReset();
  vi.unstubAllGlobals();
});

describe("KeepWarmSettings", () => {
  it("renders one row per supported agent and skips unsupported harnesses", () => {
    renderSettings();

    expect(screen.getAllByTestId("keep-warm-agent-row")).toHaveLength(2);
    expect(screen.getByText("Claude Code")).toBeInTheDocument();
    expect(screen.getByText("Codex")).toBeInTheDocument();
    expect(screen.queryByText("OpenCode")).toBeNull();
  });

  it("writes a main-session row with the family default when switched on", () => {
    renderSettings();

    fireEvent.click(
      screen.getByRole("switch", { name: "Keep main sessions warm for Claude Code" }),
    );

    expect(storedKeepWarm().agents["claude-native-ui"]).toEqual({
      main: true,
      child: false,
      intervalSeconds: 3300,
      maxSeconds: 14400,
    });
    expect(queuePatchMock).toHaveBeenLastCalledWith("keep_warm", storedKeepWarm());
  });

  it("writes children and interval edits and mirrors the legacy switch", () => {
    renderSettings();

    fireEvent.click(screen.getByRole("switch", { name: "Keep children warm for Codex" }));
    const interval = screen.getByLabelText("Keep-warm interval for Codex in minutes");
    expect(interval).toHaveValue(25);

    fireEvent.change(interval, { target: { value: "10" } });

    expect(storedKeepWarm().agents["codex-native-ui"].intervalSeconds).toBe(600);
    expect(queuePatchMock).toHaveBeenCalledWith(
      "session_collab",
      expect.objectContaining({ childKeepWarmEnabled: true }),
    );
    expect(queuePatchMock).toHaveBeenLastCalledWith("keep_warm", storedKeepWarm());
  });

  it("writes 0 for the offline-archive Off choice", () => {
    renderSettings();
    const archive = screen.getByLabelText("Archive children of an offline host after in hours");
    expect(archive).toHaveValue(4);

    fireEvent.change(archive, { target: { value: "0" } });

    expect(storedKeepWarm().hostOfflineArchiveSeconds).toBe(0);
    expect(queuePatchMock).toHaveBeenLastCalledWith("keep_warm", storedKeepWarm());
  });

  it("disables the interval and cap until an agent is switched on", () => {
    renderSettings();
    expect(screen.getByLabelText("Keep-warm interval for Claude Code in minutes")).toBeDisabled();
    expect(screen.getByLabelText("Longest keep-warm run for Claude Code in hours")).toBeDisabled();

    fireEvent.click(screen.getByRole("switch", { name: "Keep children warm for Claude Code" }));

    expect(screen.getByLabelText("Keep-warm interval for Claude Code in minutes")).toBeEnabled();
    expect(screen.getByLabelText("Longest keep-warm run for Claude Code in hours")).toBeEnabled();
  });

  it("shows the Cold after field per agent, enabled while both switches are off", () => {
    renderSettings();

    const claude = screen.getByLabelText("Cold after for Claude Code in minutes");
    expect(claude).toBeEnabled();
    expect(claude).toHaveValue(null);
    expect(claude).toHaveAttribute("placeholder", "auto");
    expect(screen.getByLabelText("Cold after for Codex in minutes")).toBeInTheDocument();
  });

  it("writes cold-after minutes as seconds and clearing removes the field", () => {
    renderSettings();
    const field = screen.getByLabelText("Cold after for Claude Code in minutes");

    fireEvent.change(field, { target: { value: "0" } });
    expect(storedKeepWarm().agents["claude-native-ui"]).toEqual({
      main: false,
      child: false,
      intervalSeconds: 3300,
      maxSeconds: 14400,
      coldAfterSeconds: 0,
    });

    fireEvent.change(field, { target: { value: "120" } });
    expect(storedKeepWarm().agents["claude-native-ui"].coldAfterSeconds).toBe(7200);

    fireEvent.change(field, { target: { value: "" } });
    expect(storedKeepWarm().agents["claude-native-ui"]).toEqual({
      main: false,
      child: false,
      intervalSeconds: 3300,
      maxSeconds: 14400,
    });
    expect(field).toHaveValue(null);
  });

  it("resolves a session-discovered harness and shows its row once resolved", async () => {
    const uploaded: AvailableAgent = {
      id: "ag_uploaded",
      name: "uploaded",
      display_name: "Uploaded",
      description: null,
      harness: null,
      skills: [],
      sessionId: "sess_uploaded",
    };
    mocks.agents = [...SUPPORTED_AGENTS, uploaded];
    const fetchStub = vi.fn((url: string) => {
      if (url === "/v1/sessions/sess_uploaded/agent") {
        return Promise.resolve({
          ok: true,
          status: 200,
          statusText: "OK",
          json: async () => ({
            id: "ag_uploaded",
            object: "agent",
            name: "uploaded",
            harness: "claude-sdk",
            skills: [],
          }),
        } as Response);
      }
      return Promise.reject(new Error(`unrouted fetch in test: ${url}`));
    });
    vi.stubGlobal("fetch", fetchStub);
    renderSettings();

    // The unresolved harness keeps the row out until the detail query lands.
    expect(screen.queryByText("Uploaded")).toBeNull();
    expect(screen.getAllByTestId("keep-warm-agent-row")).toHaveLength(2);

    await waitFor(() => expect(screen.getAllByTestId("keep-warm-agent-row")).toHaveLength(3));

    expect(screen.getByText("Uploaded")).toBeInTheDocument();
  });

  it("renders no agent rows while the list is a catalog-only placeholder", () => {
    mocks.isPlaceholderData = true;
    renderSettings();

    expect(screen.queryAllByTestId("keep-warm-agent-row")).toHaveLength(0);
    expect(screen.getByText("Loading agents…")).toBeInTheDocument();
    expect(screen.queryByText("No agents on this server support keep-warm.")).toBeNull();
    expect(
      screen.getByLabelText("Archive children of an offline host after in hours"),
    ).toBeDisabled();
  });

  it("keeps an uploaded agent that resolves to a native harness next to the built-in", async () => {
    // Regression: Settings resolves harnesses locally through the per-session
    // agent query and must not patch the shared ["available-agents"] cache —
    // a resolved harness there would make the pickers' own prefetch return
    // early and skip the native-duplicate removal.
    const uploaded: AvailableAgent = {
      id: "ag_uploaded",
      name: "my-claude-upload",
      display_name: "My-claude-upload",
      description: null,
      harness: null,
      skills: [],
      sessionId: "sess_uploaded",
    };
    mocks.agents = [claudeNative, uploaded];
    const fetchStub = vi.fn((url: string) => {
      if (url === "/v1/sessions/sess_uploaded/agent") {
        return Promise.resolve({
          ok: true,
          status: 200,
          statusText: "OK",
          json: async () => ({
            id: "ag_uploaded",
            object: "agent",
            name: "my-claude-upload",
            harness: "claude-native",
            skills: [],
          }),
        } as Response);
      }
      return Promise.reject(new Error(`unrouted fetch in test: ${url}`));
    });
    vi.stubGlobal("fetch", fetchStub);

    const { client } = renderSettings();
    client.setQueryData<AvailableAgent[]>(["available-agents"], [claudeNative, uploaded]);

    // Only the built-in has a resolved harness until the detail fetch lands.
    expect(screen.getAllByTestId("keep-warm-agent-row")).toHaveLength(1);

    await waitFor(() => expect(screen.getAllByTestId("keep-warm-agent-row")).toHaveLength(2));

    expect(screen.getAllByTestId("keep-warm-agent-row").map((row) => row.dataset.agentId)).toEqual([
      "claude-native-ui",
      "ag_uploaded",
    ]);
    // The shared cache entry for the uploaded agent still has harness: null —
    // Settings resolved it read-only, and the pickers' prefetch stays live.
    expect(
      client
        .getQueryData<AvailableAgent[]>(["available-agents"])
        ?.find((a) => a.id === "ag_uploaded")?.harness,
    ).toBeNull();
    expect(prefetchMock).not.toHaveBeenCalled();
    expect(
      fetchStub.mock.calls.filter(([url]) => url === "/v1/sessions/sess_uploaded/agent"),
    ).toHaveLength(1);
  });

  it("issues exactly one detail request per unresolved agent when a sibling resolves first", async () => {
    // A resolving while B is still pending re-renders the list; React Query's
    // per-key sharing must reuse B's in-flight request rather than refetch.
    const agentA: AvailableAgent = {
      id: "ag_a",
      name: "agent-a",
      display_name: "Agent A",
      description: null,
      harness: null,
      skills: [],
      sessionId: "sess_a",
    };
    const agentB: AvailableAgent = {
      id: "ag_b",
      name: "agent-b",
      display_name: "Agent B",
      description: null,
      harness: null,
      skills: [],
      sessionId: "sess_b",
    };
    mocks.agents = [agentA, agentB];

    let resolveA!: (r: Response) => void;
    let resolveB!: (r: Response) => void;
    const fetchStub = vi.fn((url: string) => {
      if (url === "/v1/sessions/sess_a/agent")
        return new Promise<Response>((resolve) => {
          resolveA = resolve;
        });
      if (url === "/v1/sessions/sess_b/agent")
        return new Promise<Response>((resolve) => {
          resolveB = resolve;
        });
      return Promise.reject(new Error(`unrouted fetch in test: ${url}`));
    });
    vi.stubGlobal("fetch", fetchStub);

    renderSettings();
    expect(screen.queryAllByTestId("keep-warm-agent-row")).toHaveLength(0);

    resolveA({
      ok: true,
      status: 200,
      statusText: "OK",
      json: async () => ({ id: "ag_a", object: "agent", name: "agent-a", harness: "claude-sdk" }),
    } as Response);
    await waitFor(() => expect(screen.getAllByTestId("keep-warm-agent-row")).toHaveLength(1));
    expect(screen.getAllByTestId("keep-warm-agent-row")[0].dataset.agentId).toBe("ag_a");

    resolveB({
      ok: true,
      status: 200,
      statusText: "OK",
      json: async () => ({ id: "ag_b", object: "agent", name: "agent-b", harness: "codex" }),
    } as Response);
    await waitFor(() => expect(screen.getAllByTestId("keep-warm-agent-row")).toHaveLength(2));

    const requested = fetchStub.mock.calls.map(([url]) => url);
    expect(requested.filter((url) => url === "/v1/sessions/sess_a/agent")).toHaveLength(1);
    expect(requested.filter((url) => url === "/v1/sessions/sess_b/agent")).toHaveLength(1);
  });
});
