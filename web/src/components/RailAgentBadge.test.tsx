import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  DEFAULT_AGENT_BADGE_PREFERENCES,
  type AgentBadgePreferences,
} from "@/lib/agentBadgePreferences";
import { HOST_COLORS } from "@/lib/hostColors";
import { childAgentBadgeLetters, childAgentDisplay, RailAgentBadge } from "./RailAgentBadge";

function badgePreferences(label: string): AgentBadgePreferences {
  return {
    version: 1,
    enabled: true,
    entries: { ag_1: { label, borderColor: "#123456", textColor: "theme" } },
  };
}

beforeEach(() => localStorage.clear());
afterEach(cleanup);

describe("childAgentDisplay", () => {
  it("resolves native vendors by subagent wrapper, then harness, then agent name", () => {
    expect(
      childAgentDisplay({
        id: "x",
        labels: { "omnigent.wrapper": "claude-code-native-ui-subagent" },
      }),
    ).toBe("Claude Code");
    expect(childAgentDisplay({ id: "x", harness: "codex-native" })).toBe("Codex");
    expect(childAgentDisplay({ id: "x", agent_name: "claude-native-ui" })).toBe("Claude Code");
  });

  it("prefers the bundled member's name over the bundle's agent row", () => {
    expect(childAgentDisplay({ id: "x", sub_agent_name: "researcher", agent_name: "team" })).toBe(
      "researcher",
    );
    expect(childAgentDisplay({ id: "x", agent_name: "team" })).toBe("team");
    expect(childAgentDisplay({ id: "x", tool: "reviewer" })).toBe("reviewer");
    expect(childAgentDisplay({ id: "x" })).toBeNull();
  });
});

describe("childAgentBadgeLetters", () => {
  it("uses a configured badge label for a directly bound agent", () => {
    expect(childAgentBadgeLetters({ id: "x", agent_id: "ag_1" }, badgePreferences("RV"))).toBe(
      "RV",
    );
  });

  it("does not apply the bundle's configured badge to a bundled member", () => {
    expect(
      childAgentBadgeLetters(
        { id: "x", agent_id: "ag_1", sub_agent_name: "researcher" },
        badgePreferences("RV"),
      ),
    ).toBe("RE");
  });

  it("falls back to product initials, then the display name's initials", () => {
    const claude = childAgentBadgeLetters(
      { id: "x", labels: { "omnigent.wrapper": "claude-code-native-ui-subagent" } },
      DEFAULT_AGENT_BADGE_PREFERENCES,
    );
    expect(claude).toBe("CC");
    expect(
      childAgentBadgeLetters({ id: "x", harness: "codex-native" }, DEFAULT_AGENT_BADGE_PREFERENCES),
    ).toBe("CX");
    expect(
      childAgentBadgeLetters({ id: "x", tool: "reviewer" }, DEFAULT_AGENT_BADGE_PREFERENCES),
    ).toBe("RE");
    expect(childAgentBadgeLetters({ id: "x" }, DEFAULT_AGENT_BADGE_PREFERENCES)).toBe("?");
  });
});

describe("RailAgentBadge", () => {
  it("renders the letters, the display title and the automatic host colour", () => {
    render(<RailAgentBadge child={{ id: "x", tool: "reviewer" }} hostName="TMB" />);

    const badge = screen.getByTestId("rail-agent-badge");
    expect(badge).toHaveTextContent("RE");
    expect(badge).toHaveAttribute("title", "reviewer");
    const automatic = HOST_COLORS.find(
      (entry) => entry.hex === badge.style.getPropertyValue("--host-color-light"),
    );
    expect(automatic).toBeDefined();
    expect(badge.style.getPropertyValue("--host-color-dark")).toBe(automatic!.darkHex);
  });

  it("prefers the user's host colour pick", () => {
    localStorage.setItem("omnigent:host-colors", JSON.stringify({ host_1: "purple" }));
    render(<RailAgentBadge child={{ id: "x", tool: "reviewer", host_id: "host_1" }} />);

    const badge = screen.getByTestId("rail-agent-badge");
    expect(badge.style.getPropertyValue("--host-color-light")).toBe("#8250df");
    expect(badge.style.getPropertyValue("--host-color-dark")).toBe("#a371f7");
  });
});
