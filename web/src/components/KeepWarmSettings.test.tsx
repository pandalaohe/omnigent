import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { queuePatchMock } = vi.hoisted(() => ({ queuePatchMock: vi.fn() }));
vi.mock("@/lib/userPreferencesSync", () => ({ queueUserPreferencePatch: queuePatchMock }));

vi.mock("@/hooks/useAvailableAgents", () => ({
  useAvailableAgents: () => ({
    data: [
      {
        id: "claude-native-ui",
        name: "claude-native-ui",
        display_name: "Claude Code",
        harness: "claude-native",
      },
      {
        id: "codex-native-ui",
        name: "codex-native-ui",
        display_name: "Codex",
        harness: "codex-native",
      },
      {
        id: "opencode-native-ui",
        name: "opencode-native-ui",
        display_name: "OpenCode",
        harness: "opencode-native",
      },
    ],
    isLoading: false,
  }),
}));

import { KEEP_WARM_STORAGE_KEY } from "@/lib/keepWarmPreferences";
import { KeepWarmSettings } from "./KeepWarmSettings";

function storedKeepWarm() {
  return JSON.parse(localStorage.getItem(KEEP_WARM_STORAGE_KEY) ?? "null");
}

beforeEach(() => localStorage.clear());
afterEach(() => {
  cleanup();
  queuePatchMock.mockReset();
});

describe("KeepWarmSettings", () => {
  it("renders one row per supported agent and skips unsupported harnesses", () => {
    render(<KeepWarmSettings />);

    expect(screen.getAllByTestId("keep-warm-agent-row")).toHaveLength(2);
    expect(screen.getByText("Claude Code")).toBeInTheDocument();
    expect(screen.getByText("Codex")).toBeInTheDocument();
    expect(screen.queryByText("OpenCode")).toBeNull();
  });

  it("writes a main-session row with the family default when switched on", () => {
    render(<KeepWarmSettings />);

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
    render(<KeepWarmSettings />);

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
    render(<KeepWarmSettings />);
    const archive = screen.getByLabelText("Archive children of an offline host after in hours");
    expect(archive).toHaveValue(4);

    fireEvent.change(archive, { target: { value: "0" } });

    expect(storedKeepWarm().hostOfflineArchiveSeconds).toBe(0);
    expect(queuePatchMock).toHaveBeenLastCalledWith("keep_warm", storedKeepWarm());
  });

  it("disables the interval and cap until an agent is switched on", () => {
    render(<KeepWarmSettings />);
    expect(screen.getByLabelText("Keep-warm interval for Claude Code in minutes")).toBeDisabled();
    expect(screen.getByLabelText("Longest keep-warm run for Claude Code in hours")).toBeDisabled();

    fireEvent.click(screen.getByRole("switch", { name: "Keep children warm for Claude Code" }));

    expect(screen.getByLabelText("Keep-warm interval for Claude Code in minutes")).toBeEnabled();
    expect(screen.getByLabelText("Longest keep-warm run for Claude Code in hours")).toBeEnabled();
  });
});
