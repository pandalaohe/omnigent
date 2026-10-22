import { describe, expect, it } from "vitest";

import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import type { HarnessCatalog } from "@/lib/agentLabels";
import type { CustomAgent } from "@/lib/customAgentsApi";
import type { Host } from "@/hooks/useHosts";
import {
  buildLegacyProductRegistry,
  buildRegistry,
  registryHarnessOptions,
  registrySourceLabels,
} from "./useAgentRegistry";

function agent(id: string, name: string, harness: string, product_of?: string): AvailableAgent {
  return {
    id,
    name,
    display_name: name,
    description: null,
    harness,
    skills: [],
    builtin: true,
    product_of,
  };
}

const harnesses: HarnessCatalog = {
  rows: [
    { id: "cursor-native", label: "Cursor", capabilities: { integration_mode: "native-tui" } },
    { id: "cursor", label: "Cursor", capabilities: { integration_mode: "sdk-in-process" } },
    { id: "codex", label: "Codex SDK", capabilities: { integration_mode: "cli-subprocess" } },
  ],
  capabilitiesByHarness: {
    "cursor-native": { integration_mode: "native-tui" },
    cursor: { integration_mode: "sdk-in-process" },
    codex: { integration_mode: "cli-subprocess" },
    "claude-native": { integration_mode: "native-tui" },
  },
  labels: {
    "cursor-native": "Cursor",
    cursor: "Cursor",
    codex: "Codex",
    "claude-native": "Claude Code",
  },
  setupSteps: {},
  acpHarnesses: new Set(),
  effortFamilies: {},
};

const hosts: Host[] = [
  {
    host_id: "sleeping",
    name: "Laptop",
    owner: "owner",
    status: "offline",
    configured_harnesses: {
      "cursor-native": true,
      cursor: "needs-auth",
      codex: "needs-auth",
      "claude-native": true,
    },
  },
  {
    host_id: "unknown",
    name: "Server",
    owner: "owner",
    status: "online",
    configured_harnesses: {},
  },
];

function library(id: string, name: string, harness: string): CustomAgent {
  return {
    id,
    name,
    harness,
    description: null,
    model: null,
    members: null,
    version: 1,
    created_at: 1,
    updated_at: null,
  };
}

describe("settings agent registry", () => {
  it("uses the ACP row capability for configured harnesses and agents", () => {
    const acpCatalog: HarnessCatalog = {
      ...harnesses,
      rows: [
        {
          id: "acp:example",
          label: "Example ACP",
          capabilities: { integration_mode: "acp-subprocess" },
        },
      ],
      capabilitiesByHarness: { acp: { integration_mode: "acp-subprocess" } },
      labels: { "acp:example": "Example ACP" },
    };
    const ownAgent = { ...agent("installed-id", "Example agent", "acp:example"), builtin: false };
    const candidates = [
      ["harness:acp:example", "acp"],
      ["installed-id", "yours"],
      ["ca_example", "yours"],
    ] as const;
    for (const ready of [false, true]) {
      const result = buildRegistry(
        [],
        [ownAgent],
        [library("ca_example", "Saved agent", "acp:example")],
        acpCatalog,
        [{ ...hosts[0], configured_harnesses: { "acp:example": ready } }],
      );
      for (const [key, section] of candidates) {
        expect(result.find((item) => item.key === key)).toMatchObject({ section, deployed: ready });
      }
    }
  });

  it("recognizes older native, SDK and ACP products without folding explicit composed or user rows", () => {
    const oldCatalog: HarnessCatalog = {
      ...harnesses,
      rows: [
        {
          id: "claude-native",
          label: "Claude Code",
          capabilities: { integration_mode: "native-tui" },
        },
        { id: "codex", label: "Codex SDK", capabilities: { integration_mode: "cli-subprocess" } },
        { id: "grok", label: "Grok", capabilities: { integration_mode: "acp-subprocess" } },
      ],
    };
    const oldProducts = [
      agent("claude-id", "claude-native-ui", "claude-native"),
      agent("sdk-id", "codex-sdk", "codex"),
      agent("grok-id", "grok", "grok"),
    ];
    const composed = { ...agent("composed-id", "codex-sdk", "codex"), product_of: null };
    const installed = { ...agent("installed-id", "codex-sdk", "codex"), builtin: false };
    const own = { ...agent("user-id", "codex-sdk", "codex"), mine: true as const };
    const result = buildRegistry(
      [...oldProducts, composed, installed],
      [own],
      [],
      oldCatalog,
      hosts,
    );
    for (const harness of ["claude-native", "codex", "grok"]) {
      expect(result.filter((item) => item.key === `harness:${harness}`)).toHaveLength(1);
    }
    for (const raw of ["claude-id", "sdk-id", "grok-id"]) {
      expect(result.some((item) => item.key === raw)).toBe(false);
    }
    for (const raw of ["composed-id", "installed-id", "user-id"]) {
      expect(result.some((item) => item.key === raw)).toBe(true);
    }
  });

  it("keeps same-name harness forms and declares SDK products once", () => {
    const catalog = [
      agent("cursor-agent", "Cursor", "cursor-native", "cursor-native"),
      agent("sdk-agent", "Codex SDK", "codex", "codex"),
      agent("polly-agent", "Polly", "claude-native"),
    ];
    const items = buildRegistry(catalog, [], [], harnesses, hosts);
    expect(
      items.filter((item) => item.label === "Cursor").map((item) => [item.key, item.section]),
    ).toEqual([
      ["harness:cursor-native", "native"],
      ["harness:cursor", "sdk"],
    ]);
    expect(items.filter((item) => item.key === "harness:codex")).toHaveLength(1);
    expect(items.find((item) => item.key === "polly-agent")?.section).toBe("builtin");
    expect(items.some((item) => item.key === "sdk-agent")).toBe(false);
    const savedProducts = buildLegacyProductRegistry(catalog, [], items);
    expect(savedProducts["cursor-agent"]).toMatchObject({
      key: "cursor-agent",
      harness: "cursor-native",
      section: "native",
      deployed: true,
    });
    expect(savedProducts["sdk-agent"]).toMatchObject({ harness: "codex", section: "sdk" });
    expect(savedProducts["polly-agent"]).toBeUndefined();
  });

  it("uses offline reports, SDK auth state, unknown maps and distinct library ids", () => {
    const items = buildRegistry(
      [],
      [],
      [library("ca_1", "Orion", "claude-native"), library("ca_2", "Orion", "codex")],
      harnesses,
      hosts,
    );
    expect(items.find((item) => item.key === "harness:cursor-native")?.deployed).toBe(true);
    expect(items.find((item) => item.key === "harness:cursor")?.deployed).toBe(false);
    expect(items.find((item) => item.key === "harness:codex")?.deployed).toBe(true);
    expect(items.filter((item) => item.section === "yours").map((item) => item.label)).toEqual([
      "Orion · Claude Code · library",
      "Orion · Codex · library",
    ]);
    expect(items.find((item) => item.key === "ca_1")?.deployed).toBe(true);
    const newlyReady = buildRegistry([], [], [], harnesses, [
      { ...hosts[0], configured_harnesses: { ...hosts[0].configured_harnesses, cursor: true } },
    ]);
    expect(newlyReady.find((item) => item.key === "harness:cursor")?.deployed).toBe(true);
    expect(
      buildRegistry([], [], [], harnesses, [{ ...hosts[1], configured_harnesses: null }]).every(
        (item) => item.deployed,
      ),
    ).toBe(true);
  });

  it("keeps brain harness membership while grouping dynamic worker and source labels", () => {
    const items = buildRegistry(
      [agent("native-product", "Native X", "native-x", "native-x")],
      [],
      [],
      {
        ...harnesses,
        rows: [
          {
            id: "plugin-sdk",
            label: "Plugin SDK",
            capabilities: { integration_mode: "sdk-in-process" },
          },
        ],
        capabilitiesByHarness: {
          "native-x": { integration_mode: "native-tui" },
          "plugin-sdk": { integration_mode: "sdk-in-process" },
        },
      },
      hosts,
    );
    expect(
      registryHarnessOptions(items, { "plugin-sdk": "Plugin SDK" }, ["plugin-sdk", "native-x"]),
    ).toEqual([{ id: "plugin-sdk", label: "Plugin SDK", section: "sdk", deployed: false }]);
    expect(registryHarnessOptions(items).map((item) => item.id)).toContain("native-x");
    expect(registrySourceLabels(items, "Native X", "native-x").harnessLabel).toBe("Native X");
  });

  it("carries deployment status into harness menus and fails open for missing registry rows", () => {
    const items = buildRegistry(
      [],
      [],
      [],
      {
        ...harnesses,
        rows: [{ id: "plugin-sdk", label: "Plugin SDK" }],
        capabilitiesByHarness: { "plugin-sdk": { integration_mode: "sdk-in-process" } },
      },
      [
        {
          host_id: "unconfigured-host",
          name: "Laptop",
          owner: "me",
          status: "offline",
          configured_harnesses: { "plugin-sdk": false },
        },
      ],
    );
    expect(registryHarnessOptions(items, { absent: "Absent" }, ["plugin-sdk", "absent"])).toEqual([
      { id: "plugin-sdk", label: "Plugin SDK", section: "sdk", deployed: false },
      { id: "absent", label: "Absent", section: "other", deployed: true },
    ]);
  });
});
