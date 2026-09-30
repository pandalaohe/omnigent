import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  writeAgentBadgePreferences,
  type AgentBadgePreferences,
  type AgentBadgeValue,
} from "@/lib/agentBadgePreferences";
import { AGENT_TEMPLATE_LABEL } from "@/lib/customAgentsApi";
import { childAgentBadge, childAgentDisplay, RailAgentBadge } from "./RailAgentBadge";

const CONFIGURED: AgentBadgeValue = { label: "RV", borderColor: "#123456", textColor: "theme" };

function preferences(entries: Record<string, AgentBadgeValue>): AgentBadgePreferences {
  return { version: 1, enabled: true, entries };
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

describe("childAgentBadge", () => {
  const prefs = preferences({
    tmpl_1: { label: "T1", borderColor: "#111111", textColor: "theme" },
    tmpl_2: { label: "T2", borderColor: "#222222", textColor: "theme" },
    ag_1: { label: "A1", borderColor: "#333333", textColor: "theme" },
  });

  it("keys a directly bound agent by template label, then template id, then agent id", () => {
    const bound = {
      id: "x",
      labels: { [AGENT_TEMPLATE_LABEL]: "tmpl_1" },
      agent_template_id: "tmpl_2",
      agent_id: "ag_1",
    };

    expect(childAgentBadge(bound, prefs)?.label).toBe("T1");
    expect(childAgentBadge({ ...bound, labels: {} }, prefs)?.label).toBe("T2");
    expect(childAgentBadge({ ...bound, labels: {}, agent_template_id: null }, prefs)?.label).toBe(
      "A1",
    );
  });

  it("returns null for an unconfigured or absent agent", () => {
    expect(childAgentBadge({ id: "x", agent_id: "ag_other" }, prefs)).toBeNull();
    expect(childAgentBadge({ id: "x" }, prefs)).toBeNull();
  });

  it("returns null for a bundled member even when its bound row is configured", () => {
    expect(
      childAgentBadge({ id: "x", agent_id: "ag_1", sub_agent_name: "researcher" }, prefs),
    ).toBeNull();
  });
});

describe("RailAgentBadge", () => {
  it("renders the configured badge exactly as AgentBadge draws it", () => {
    writeAgentBadgePreferences(
      preferences({ ag_1: { label: "RV", borderColor: "#123456", textColor: "#e9d5ff" } }),
    );
    render(<RailAgentBadge child={{ id: "x", tool: "reviewer", agent_id: "ag_1" }} />);

    const badge = screen.getByTestId("rail-agent-badge");
    expect(badge).toHaveTextContent("RV");
    expect(badge).toHaveAttribute("title", "reviewer");
    expect(badge).toHaveClass("border-2", "size-5", "rounded-[5px]");
    expect(badge).not.toHaveClass("host-color");
    expect(badge).toHaveStyle({ borderColor: "#123456", color: "#e9d5ff" });
    expect(badge.style.getPropertyValue("--host-color-light")).toBe("");
    expect(badge.style.backgroundColor).toBe("");
  });

  it("follows the theme foreground for a theme text colour", () => {
    writeAgentBadgePreferences(preferences({ ag_1: CONFIGURED }));
    render(<RailAgentBadge child={{ id: "x", agent_id: "ag_1" }} />);

    expect(screen.getByTestId("rail-agent-badge").style.color).toBe("var(--foreground)");
  });

  it("renders nothing when the child's agent has no configured badge", () => {
    render(
      <>
        <RailAgentBadge
          child={{ id: "claude", labels: { "omnigent.wrapper": "claude-code-native-ui-subagent" } }}
        />
        <RailAgentBadge child={{ id: "codex", harness: "codex-native" }} />
        <RailAgentBadge child={{ id: "tool", tool: "reviewer" }} />
      </>,
    );

    expect(screen.queryByTestId("rail-agent-badge")).toBeNull();
  });

  it("renders nothing for a bundled member even when its bound row is configured", () => {
    writeAgentBadgePreferences(preferences({ ag_1: CONFIGURED }));
    render(<RailAgentBadge child={{ id: "x", agent_id: "ag_1", sub_agent_name: "researcher" }} />);

    expect(screen.queryByTestId("rail-agent-badge")).toBeNull();
  });

  it("renders nothing for a harness sub-agent mirror even when its bound row is configured", () => {
    const prefs = preferences({ ag_1: CONFIGURED });
    const mirror = {
      id: "claude",
      agent_id: "ag_1",
      labels: { "omnigent.wrapper": "claude-code-native-ui-subagent" },
    };
    writeAgentBadgePreferences(prefs);

    expect(childAgentBadge(mirror, prefs)).toBeNull();
    render(<RailAgentBadge child={mirror} />);

    expect(screen.queryByTestId("rail-agent-badge")).toBeNull();
  });
});
