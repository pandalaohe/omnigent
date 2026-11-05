import { describe, expect, it } from "vitest";
import type { Host } from "@/hooks/useHosts";
import { agentPresentOnHost, agentSettingsPath, groupAgentHarnesses } from "./agentSettings";

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
  });

  it("encodes a configured agent and the chosen host once", () => {
    expect(agentSettingsPath("acp:research/one", "host & two")).toBe(
      "/settings/agents/acp%3Aresearch%2Fone?host=host%20%26%20two",
    );
  });
});
