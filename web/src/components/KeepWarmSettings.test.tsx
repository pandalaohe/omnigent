import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
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
  return { ...view, rerenderSettings };
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

  it("resolves a session-discovered harness and shows its row once resolved", () => {
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
    const { rerenderSettings } = renderSettings();

    expect(prefetchMock).toHaveBeenCalledWith(uploaded, expect.anything());
    expect(screen.queryByText("Uploaded")).toBeNull();
    expect(screen.getAllByTestId("keep-warm-agent-row")).toHaveLength(2);

    mocks.agents = [...SUPPORTED_AGENTS, { ...uploaded, harness: "claude-native" }];
    rerenderSettings();

    expect(screen.getByText("Uploaded")).toBeInTheDocument();
    expect(screen.getAllByTestId("keep-warm-agent-row")).toHaveLength(3);
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
});
