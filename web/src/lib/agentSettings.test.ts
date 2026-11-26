import { describe, expect, it } from "vitest";
import type { Host } from "@/hooks/useHosts";
import {
  agentPresentOnHost,
  agentSettingsPath,
  groupAgentHarnesses,
  harnessSettingsBadgeId,
} from "./agentSettings";

const host: Host = {
  host_id: "desktop",
  name: "Desktop",
  owner: "local",
  status: "online",
  platform: "darwin",
  configured_harnesses: { "claude-native": true, "codex-native": "binary-missing" },
};

describe("agent settings rows", () => {
  it("uses cached inventory on offline hosts and keeps unknown inventory visible", () => {
    expect(agentPresentOnHost("claude-native", { ...host, status: "offline" })).toBe(true);
    expect(agentPresentOnHost("codex-native", host)).toBe(false);
    expect(agentPresentOnHost("claude-native", { ...host, platform: "win32" })).toBe(false);
    expect(agentPresentOnHost("claude-native", { ...host, configured_harnesses: null })).toBe(true);
    expect(
      agentPresentOnHost("claude-native", {
        ...host,
        configured_harnesses: { "claude-native": "needs-auth" },
      }),
    ).toBe(true);
  });

  it("places each dependent mode immediately after its product without reserved rows", () => {
    expect(
      groupAgentHarnesses([
        "claude-sdk",
        "codex-native",
        "claude-native",
        "codex",
        "*",
        "codex-native",
      ]),
    ).toEqual(["claude-native", "claude-sdk", "codex-native", "codex"]);
    expect(
      groupAgentHarnesses(["pi", "pi-native"], {
        pi: { calling_defaults: { model_options: true, inherits_from: "pi-native" } },
      }),
    ).toEqual(["pi-native", "pi"]);
    expect(groupAgentHarnesses(["ag-acp", "codex-native", "antigravity-native"])).toEqual([
      "antigravity-native",
      "ag-acp",
      "codex-native",
    ]);
  });

  it("encodes a configured agent and the chosen host once", () => {
    expect(agentSettingsPath("acp:research/one", "host & two")).toBe(
      "/settings/agents/acp%3Aresearch%2Fone?host=host%20%26%20two",
    );
  });

  it("resolves deterministic badge ids for ag-acp and remote acp:<slug> built-ins", () => {
    const products = {
      "58137a60fc3308392a40de7d8dbc55d8": {
        key: "58137a60fc3308392a40de7d8dbc55d8",
        label: "AG ACP",
        name: "ag-acp",
        section: "acp" as const,
        harness: "ag-acp",
        description: null,
        source: "builtin" as const,
        deployed: true,
      },
      "8001677c98097148edfa65fb5c334189": {
        key: "8001677c98097148edfa65fb5c334189",
        label: "Hermes-fn",
        name: "hermes-fn",
        section: "acp" as const,
        harness: "acp:hermes-fn",
        description: null,
        source: "builtin" as const,
        deployed: true,
      },
    };
    expect(harnessSettingsBadgeId("ag-acp", products)).toBe("58137a60fc3308392a40de7d8dbc55d8");
    expect(harnessSettingsBadgeId("acp:hermes-fn", products)).toBe(
      "8001677c98097148edfa65fb5c334189",
    );
  });
});
