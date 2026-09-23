// Pure state-machine tests for the project-prefill config seeding. The
// component-level rules live in NewChatDialog.projectPrefill.test.tsx; these
// pin the transitions that need mid-flight timing (a user acting between the
// config loading and resolving), which the rendered harness can't sequence.
import { describe, expect, it } from "vitest";

import type { Host } from "@/hooks/useHosts";
import { SANDBOX_HOST_CHOICE } from "@/lib/hostPreferences";
import type { ProjectHostRoots } from "@/lib/projectsApi";
import { initialPrefillState, projectPrefillStep } from "./projectPrefill";

const hosts: Host[] = [
  { host_id: "host_1", name: "laptop", owner: "corey", status: "online" },
  { host_id: "host_2", name: "desktop", owner: "corey", status: "online" },
];

function inputs(
  overrides: Partial<Parameters<typeof projectPrefillStep>[1]> = {},
): Parameters<typeof projectPrefillStep>[1] {
  return {
    hosts,
    agents: [{ id: "ag_hello" }],
    sandboxSelected: false,
    managedSandboxesEnabled: false,
    selectedHostId: null,
    lastAgentId: null,
    config: {},
    roots: null as ProjectHostRoots | null | undefined,
    ...overrides,
  };
}

/** Run the machine from the start until it settles (or stalls). */
function runToDone(stepInputs: ReturnType<typeof inputs>) {
  let state = initialPrefillState("Alpha");
  const writes: Record<string, string | boolean> = {};
  for (let i = 0; i < 10; i++) {
    const step = projectPrefillStep(state, stepInputs);
    if (step === null) break;
    state = step.state;
    Object.assign(writes, step.writes);
    if (state.phase === "settled" && state.agentSeeded) break;
  }
  return { state, writes };
}

const roots: ProjectHostRoots = {
  roots: [
    { host_id: "host_1", workspace: "/repo/alpha", source: "binding" },
    { host_id: "host_2", workspace: "/repo/beta", source: "config" },
  ],
  default_host_id: "host_2",
  default_host_reason: "config",
};

describe("projectPrefill location seeding", () => {
  it("waits while the config is still loading", () => {
    const step = projectPrefillStep(initialPrefillState("Alpha"), inputs({ config: undefined }));
    expect(step).toBeNull();
  });

  it("keeps the location phase open while the host list is still loading", () => {
    const step = projectPrefillStep(
      initialPrefillState("Alpha"),
      inputs({ hosts: undefined, roots }),
    );
    // The agent track can settle independently, but the host seed must wait
    // for the host list, so the location phase stays open (no host write).
    expect(step).not.toBeNull();
    expect(step!.state.phase).toBe("location");
    expect(step!.writes.hostId).toBeUndefined();
  });

  it("waits for project roots before choosing a host", () => {
    const step = projectPrefillStep(initialPrefillState("Alpha"), inputs({ roots: undefined }));
    expect(step!.state.phase).toBe("location");
    expect(step!.writes.hostId).toBeUndefined();
  });

  it("seeds the default host and its root", () => {
    const { state, writes } = runToDone(inputs({ roots }));
    expect(writes.hostId).toBe("host_2");
    expect(writes.workspace).toBe("/repo/beta");
    expect(state.phase).toBe("settled");
    expect(state.agentSeeded).toBe(true);
  });

  it("settles a label-only folder without a host root", () => {
    const { state, writes } = runToDone(inputs({ config: {} }));
    expect(state.phase).toBe("settled");
    expect(writes.hostId).toBeUndefined();
    expect(writes.workspace).toBeUndefined();
  });

  it("keeps an offline config host and its own root", () => {
    const { writes } = runToDone(
      inputs({ hosts: [hosts[0], { ...hosts[1], status: "offline" }], roots }),
    );
    expect(writes.hostId).toBe("host_2");
    expect(writes.workspace).toBe("/repo/beta");
  });

  it("leaves an unavailable default host and its directory empty", () => {
    const { writes } = runToDone(inputs({ hosts: [hosts[0]], roots }));
    expect(writes.hostId).toBeUndefined();
    expect(writes.workspace).toBeUndefined();
  });

  it("leaves an ambiguous project without a host", () => {
    const { writes } = runToDone(
      inputs({ roots: { ...roots, default_host_id: null, default_host_reason: "ambiguous" } }),
    );
    expect(writes.hostId).toBeUndefined();
    expect(writes.workspace).toBeUndefined();
  });

  it("uses an explicitly selected host's root", () => {
    const { writes } = runToDone(inputs({ roots, selectedHostId: "host_1" }));
    expect(writes.hostId).toBeUndefined();
    expect(writes.workspace).toBe("/repo/alpha");
  });

  it("does not seed a directory when the selected host has no root", () => {
    const { writes } = runToDone(
      inputs({ roots: { ...roots, roots: [roots.roots[1]] }, selectedHostId: "host_1" }),
    );
    expect(writes.workspace).toBeUndefined();
  });

  it("does not replace a user's sandbox pick with the project host root", () => {
    const { writes } = runToDone(inputs({ roots, sandboxSelected: true }));
    expect(writes.hostId).toBeUndefined();
    expect(writes.workspace).toBeUndefined();
  });

  it("selects the sandbox from a stored sandbox default", () => {
    const { state, writes } = runToDone(
      inputs({ config: { hostId: SANDBOX_HOST_CHOICE }, roots, managedSandboxesEnabled: true }),
    );
    // The sandbox sentinel is not a real host id, so it seeds via selectSandbox.
    expect(writes.selectSandbox).toBe(true);
    expect(writes.hostId).toBeUndefined();
    expect(state.phase).toBe("settled");
  });

  it("drops a stored sandbox default when the server no longer offers sandboxes", () => {
    const { writes } = runToDone(
      inputs({ config: { hostId: SANDBOX_HOST_CHOICE }, roots, managedSandboxesEnabled: false }),
    );
    expect(writes.selectSandbox).toBeUndefined();
    expect(writes.hostId).toBeUndefined();
  });

  it("does not re-select the sandbox once it is already selected", () => {
    const { writes } = runToDone(
      inputs({
        config: { hostId: SANDBOX_HOST_CHOICE },
        roots,
        managedSandboxesEnabled: true,
        sandboxSelected: true,
      }),
    );
    expect(writes.selectSandbox).toBeUndefined();
  });

  it("does not select the sandbox when the user already picked a host", () => {
    const { writes } = runToDone(
      inputs({
        config: { hostId: SANDBOX_HOST_CHOICE },
        roots,
        managedSandboxesEnabled: true,
        selectedHostId: "host_1",
      }),
    );
    expect(writes.selectSandbox).toBeUndefined();
  });
});

describe("projectPrefill agent seeding", () => {
  it("seeds the agent from config when it is a pickable agent", () => {
    const { writes } = runToDone(
      inputs({ agents: [{ id: "ag_hello" }, { id: "ag_cfg" }], config: { agentId: "ag_cfg" } }),
    );
    expect(writes.agentId).toBe("ag_cfg");
  });

  it("falls back to the last-used agent when config sets none", () => {
    const { writes } = runToDone(
      inputs({ agents: [{ id: "ag_hello" }], lastAgentId: "ag_hello", config: {} }),
    );
    expect(writes.agentId).toBe("ag_hello");
  });

  it("seeds a config agent missing from the picker list verbatim — never the last-used", () => {
    // The create API accepts session-scoped agents the caller can read, so a
    // configured agent absent from the list is seeded as-is; if it's truly
    // unresolvable the composer surfaces an explicit unavailable state.
    // Substituting last-agent-id here silently rebound project sessions to
    // whatever agent the user last touched.
    const { writes } = runToDone(
      inputs({
        agents: [{ id: "ag_hello" }],
        lastAgentId: "ag_hello",
        config: { agentId: "ag_gone" },
      }),
    );
    expect(writes.agentId).toBe("ag_gone");
  });

  it("seeds a config agent before the picker list has loaded", () => {
    const step = projectPrefillStep(
      initialPrefillState("Alpha"),
      inputs({ agents: undefined, config: { agentId: "ag_cfg" } }),
    );
    expect(step).not.toBeNull();
    expect(step!.state.agentSeeded).toBe(true);
    expect(step!.writes.agentId).toBe("ag_cfg");
  });

  it("treats a blank config agent id as absent and falls back to the last-used agent", () => {
    // An empty or whitespace-only stored id is "not configured" — seeding it
    // verbatim would trip the composer's "agent unavailable" state for a
    // config that names no agent at all.
    for (const agentId of ["", "   "]) {
      const { writes } = runToDone(
        inputs({ agents: [{ id: "ag_hello" }], lastAgentId: "ag_hello", config: { agentId } }),
      );
      expect(writes.agentId).toBe("ag_hello");
    }
  });

  it("seeds no agent when neither config nor last-used is available", () => {
    const { state, writes } = runToDone(inputs({ lastAgentId: null, config: {} }));
    expect(writes.agentId).toBeUndefined();
    // Still marks the track done so the generic defaults can proceed.
    expect(state.agentSeeded).toBe(true);
  });

  it("waits for the agents list before settling the agent track", () => {
    const step = projectPrefillStep(
      initialPrefillState("Alpha"),
      inputs({ agents: undefined, config: { hostId: "host_1" } }),
    );
    // Host track can still settle, but the agent track stays open.
    expect(step).not.toBeNull();
    expect(step!.state.agentSeeded).toBe(false);
  });
});
