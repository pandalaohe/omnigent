import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { writeAgentBadgePreferences } from "@/lib/agentBadgePreferences";
import { AgentBadge } from "./AgentBadge";

vi.mock("@/lib/userPreferencesSync", () => ({ queueUserPreferencePatch: vi.fn() }));

beforeEach(() => localStorage.clear());
afterEach(cleanup);

const configured = {
  version: 1 as const,
  enabled: true,
  entries: {
    "agent-a": { label: "研", borderColor: "#8b5cf6", textColor: "#e9d5ff" },
  },
};

describe("AgentBadge", () => {
  it("uses the active theme foreground when configured to follow the theme", () => {
    writeAgentBadgePreferences({
      ...configured,
      entries: {
        "agent-a": { ...configured.entries["agent-a"], textColor: "theme" },
      },
    });
    render(<AgentBadge agentId="agent-a" />);
    expect(screen.getByTestId("agent-badge").style.color).toBe("var(--foreground)");
  });
  it("renders the configured 20px badge for the exact Agent id", () => {
    writeAgentBadgePreferences(configured);
    render(<AgentBadge agentId="agent-a" className="test-extra" />);

    const badge = screen.getByTestId("agent-badge");
    expect(badge).toHaveTextContent("研");
    expect(badge).toHaveClass("size-5", "test-extra");
    expect(badge).toHaveStyle({ borderColor: "#8b5cf6", color: "#e9d5ff" });
  });

  it("renders nothing for null, an unconfigured Agent, or when globally disabled", () => {
    writeAgentBadgePreferences(configured);
    const { rerender } = render(<AgentBadge agentId={null} />);
    expect(screen.queryByTestId("agent-badge")).toBeNull();

    rerender(<AgentBadge agentId="agent-b" />);
    expect(screen.queryByTestId("agent-badge")).toBeNull();

    writeAgentBadgePreferences({ ...configured, enabled: false });
    rerender(<AgentBadge agentId="agent-a" />);
    expect(screen.queryByTestId("agent-badge")).toBeNull();
  });

  it("shares one pair of DOM event listeners across repeated badge rows", () => {
    const add = vi.spyOn(window, "addEventListener");
    writeAgentBadgePreferences(configured);
    render(
      <>
        <AgentBadge agentId="agent-a" />
        <AgentBadge agentId="agent-a" />
      </>,
    );

    expect(
      add.mock.calls.filter(
        ([eventName]) => eventName === "omnigent:agent-badge-preferences-changed",
      ),
    ).toHaveLength(1);
    expect(add.mock.calls.filter(([eventName]) => eventName === "storage")).toHaveLength(1);
    add.mockRestore();
  });
});
