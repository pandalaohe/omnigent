import { describe, expect, it } from "vitest";

import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import type { Host } from "@/hooks/useHosts";
import { rankHarnessRows, type RankHarnessRowsInput } from "@/lib/harnessRanking";

function agent(name: string, harness: string, displayName: string): AvailableAgent {
  return {
    id: name,
    name,
    display_name: displayName,
    description: null,
    harness,
    skills: [],
  };
}

const claude = agent("claude-native-ui", "claude-native", "Claude Code");
const cursor = agent("cursor-native-ui", "cursor-native", "Cursor");
const codex = agent("codex-native-ui", "codex-native", "Codex");
const opencode = agent("opencode-native-ui", "opencode-native", "OpenCode");
const pi = agent("pi-native-ui", "pi-native", "Pi");
const kiro = agent("kiro-native-ui", "kiro-native", "Kiro");
const grok = agent("grok", "grok", "Grok Build");

function host(configured: Record<string, boolean | string> | null, status: Host["status"] = "online"): Host {
  return {
    host_id: "host_1",
    name: "laptop",
    owner: "alice",
    status,
    configured_harnesses: configured,
  };
}

function rank(overrides: Partial<RankHarnessRowsInput> = {}) {
  return rankHarnessRows({
    entries: [claude, cursor, codex, opencode, pi],
    host: host({
      "claude-native": true,
      "cursor-native": true,
      "codex-native": true,
      "opencode-native": true,
      "pi-native": true,
    }),
    recentHarnesses: [],
    hideUnconfigured: false,
    selectedId: null,
    promotedId: null,
    ...overrides,
  });
}

const names = (agents: AvailableAgent[]) => agents.map((entry) => entry.display_name);

describe("rankHarnessRows", () => {
  it("ranks ready recent harnesses first, then fills from the fixed order", () => {
    const result = rank({
      host: host({
        "claude-native": true,
        "cursor-native": "binary-missing",
        "codex-native": true,
        "opencode-native": true,
        "pi-native": true,
      }),
      recentHarnesses: ["opencode-native", "codex-native"],
    });
    expect(names(result.primary)).toEqual(["OpenCode", "Codex", "Claude Code"]);
    expect(names(result.more)).toContain("Cursor");
  });

  it("fills empty recency from the fixed order, then native display rank", () => {
    const result = rank({ entries: [pi, opencode, codex, claude] });
    expect(names(result.primary)).toEqual(["Claude Code", "Codex", "OpenCode"]);
    expect(names(result.more)).toEqual(["Pi"]);
  });

  it("leaves an unreported recent harness under Other", () => {
    const result = rank({
      host: host({ "claude-native": true, "codex-native": true }),
      recentHarnesses: ["pi-native"],
    });
    expect(names(result.primary)).toEqual(["Claude Code", "Codex"]);
    expect(names(result.more)).toContain("Pi");
  });

  it("leaves every row under Other when the host has no readiness map", () => {
    const result = rank({ host: host(null) });
    expect(result.primary).toEqual([]);
    expect(result.more).toHaveLength(5);
  });

  it("keeps an offline selected row inline without promoting the others", () => {
    const result = rank({
      host: host({ "claude-native": true }, "offline"),
      selectedId: claude.id,
      promotedId: claude.id,
    });
    expect(names(result.primary)).toEqual(["Claude Code"]);
    expect(result.more).toHaveLength(4);
  });

  it("keeps an unusable promoted row without consuming one of three slots", () => {
    const result = rank({
      entries: [kiro, claude, codex, opencode],
      host: host({
        "kiro-native": "needs-auth",
        "claude-native": true,
        "codex-native": true,
        "opencode-native": true,
      }),
      recentHarnesses: ["opencode-native", "codex-native", "claude-native"],
      selectedId: kiro.id,
      promotedId: kiro.id,
    });
    expect(names(result.primary)).toEqual(["OpenCode", "Codex", "Claude Code", "Kiro"]);
  });

  it("hides an unconfigured row except when it is selected", () => {
    const unavailableHost = host({
      "claude-native": true,
      "cursor-native": "needs-auth",
      "codex-native": true,
    });
    const hidden = rank({ entries: [claude, cursor, codex], host: unavailableHost, hideUnconfigured: true });
    expect([...hidden.primary, ...hidden.more]).not.toContain(cursor);

    const selected = rank({
      entries: [claude, cursor, codex],
      host: unavailableHost,
      hideUnconfigured: true,
      selectedId: cursor.id,
    });
    expect(names(selected.primary)).toEqual(["Claude Code", "Codex"]);
    expect(names(selected.more)).toEqual(["Cursor"]);
  });

  it("matches ACP ids directly and native alias spellings by product", () => {
    const result = rank({
      entries: [grok, claude, pi],
      host: host({ grok: true, "claude-native": true, "pi-native": true }),
      recentHarnesses: ["grok", "claude", "native-pi"],
    });
    expect(names(result.primary)).toEqual(["Grok Build", "Claude Code", "Pi"]);
  });
});
