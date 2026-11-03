import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { writeAgentBadgePreferences } from "@/lib/agentBadgePreferences";
import { AgentNameTag } from "./AgentNameTag";

vi.mock("@/lib/userPreferencesSync", () => ({ queueUserPreferencePatch: vi.fn() }));

beforeEach(() => localStorage.clear());
afterEach(cleanup);

const preferences = {
  version: 1 as const,
  enabled: true,
  entries: {
    "agent-a": { label: "研", borderColor: "#8b5cf6", textColor: "#e9d5ff" },
  },
};

describe("AgentNameTag", () => {
  it("shows the configured badge beside the readable agent name", () => {
    writeAgentBadgePreferences(preferences);
    const { container } = render(
      <AgentNameTag agentId="agent-a" name="Research" harness="claude-sdk" className="extra" />,
    );
    expect(screen.getByTestId("agent-name-badge")).toHaveTextContent("研");
    expect(screen.getByText("Research")).toBeVisible();
    expect(container.firstChild).toHaveClass("extra");
  });

  it("falls back to the plain mark when badges are disabled", () => {
    writeAgentBadgePreferences({ ...preferences, enabled: false });
    render(<AgentNameTag agentId="agent-a" name="Research" harness="claude-sdk" />);
    expect(screen.queryByTestId("agent-name-badge")).toBeNull();
    expect(screen.getByTestId("agent-name-mark")).toHaveTextContent("SDK");
  });

  it.each([
    ["claude-sdk", "SDK"],
    ["codex-native", "Native"],
    ["acp:hermes", "ACP"],
    ["pi-rpc", "RPC"],
  ])("maps %s to %s", (harness, mark) => {
    render(<AgentNameTag agentId={null} name="Agent" harness={harness} />);
    expect(screen.getByTestId("agent-name-mark")).toHaveTextContent(mark);
  });

  it("can render only the mark", () => {
    render(<AgentNameTag agentId={null} name="Research" harness="pi-rpc" markOnly />);
    expect(screen.getByTestId("agent-name-mark")).toHaveTextContent("RPC");
    expect(screen.queryByText("Research")).toBeNull();
  });
});
