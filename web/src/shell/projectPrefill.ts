import type { Host } from "@/hooks/useHosts";
import { SANDBOX_HOST_CHOICE } from "@/lib/hostPreferences";
import type { ProjectHostRoots } from "@/lib/projectsApi";

type ProjectPrefillPhase = "location" | "settled";

/** Project config supplies agent and sandbox hints; host roots supply real-host placement. */
export interface ProjectPrefillConfig {
  hostId?: string;
  workspace?: string;
  agentId?: string;
  /** Opt-in worktree default; only `true` is meaningful (absent = no worktree). */
  useWorktree?: boolean;
  /** Default model for the configured agent's harness. Not seeded by this
   *  machine — the composer's harness-seed effect consumes it directly (it owns
   *  the model slot and would otherwise clobber a machine write). */
  model?: string;
}

export interface ProjectPrefillState {
  /** Project this machine is seeding for; "" = plain visit (starts done). */
  project: string;
  /** Location track: seed host/workspace from roots, then settle. */
  phase: ProjectPrefillPhase;
  /** Agent track, independent so a slow agents fetch can't hold up the
   *  location seeding (or the generic defaults gated on "settled"). */
  agentSeeded: boolean;
}

export function initialPrefillState(project: string): ProjectPrefillState {
  const plain = project === "";
  return {
    project,
    phase: plain ? "settled" : "location",
    agentSeeded: plain,
  };
}

/** True once both tracks are done and stepping is a no-op. */
export function prefillDone(state: ProjectPrefillState): boolean {
  return state.phase === "settled" && state.agentSeeded;
}

interface ProjectPrefillInputs {
  hosts: Host[] | undefined;
  /** Pickable agents; undefined = still loading. */
  agents: { id: string }[] | undefined;
  sandboxSelected: boolean;
  /** Whether the server offers managed sandbox hosts. Gates seeding a stored
   *  sandbox default — an OSS server that no longer offers it drops the hint. */
  managedSandboxesEnabled: boolean;
  /** Live host pick; a mid-flight manual switch aborts the default host seeding
   *  so a stored host can't clobber the user's own choice. */
  selectedHostId: string | null;
  /** Last-used agent id from localStorage (readLastAgentId()) — the generic
   *  agent fallback when the config sets none. */
  lastAgentId: string | null;
  /** undefined waits for the config query; {} has no config hints. */
  config: ProjectPrefillConfig | undefined;
  /** undefined waits for a first-class project's roots; null is a label-only folder. */
  roots: ProjectHostRoots | null | undefined;
}

interface ProjectPrefillWrites {
  hostId?: string;
  agentId?: string;
  workspace?: string;
  /** Select the managed sandbox as the target (config.hostId was the sandbox
   *  sentinel). Distinct from `hostId` — the sandbox is not a real host id. */
  selectSandbox?: boolean;
}

/**
 * One transition. null = keep waiting for data; otherwise the next state
 * plus slot writes to apply (fill-empty-only; the component enforces that).
 * The agent seed and the location phase advance independently, mirroring
 * the data they wait on: agents can lag the host list.
 */
export function projectPrefillStep(
  state: ProjectPrefillState,
  inputs: ProjectPrefillInputs,
): { state: ProjectPrefillState; writes: ProjectPrefillWrites } | null {
  const writes: ProjectPrefillWrites = {};
  let next = state;

  // Wait for config before the location track can choose the sandbox branch.
  if (inputs.config === undefined) return null;

  if (!state.agentSeeded) {
    const { agents, lastAgentId, config } = inputs;
    // A blank/whitespace-only stored id is "not configured", not a real agent
    // — fall through to the generic default instead of seeding a value the
    // composer could only surface as "agent unavailable".
    if (config?.agentId != null && config.agentId.trim() !== "") {
      // A configured agent seeds verbatim, without waiting for (or checking)
      // the picker list — the create API accepts session-scoped agents the
      // caller can read, so absence from the list doesn't mean unusable.
      // Never substitute the last-used agent for a configured one; if the id
      // truly can't be resolved the composer surfaces an explicit
      // "configured agent unavailable" state instead.
      next = { ...next, agentSeeded: true };
      writes.agentId = config.agentId;
    } else if (agents !== undefined) {
      // No configured agent: need the pickable agents to judge whether the
      // last-used agent is still selectable; wait for them.
      next = { ...next, agentSeeded: true };
      if (lastAgentId && agents.some((a) => a.id === lastAgentId)) {
        writes.agentId = lastAgentId;
      }
    }
  }

  const location = locationStep(next, inputs, writes);
  if (location !== null) next = location;

  if (next === state) return null; // both tracks waiting on data
  return { state: next, writes };
}

/** The location track's terminal phase. */
function settled(state: ProjectPrefillState): ProjectPrefillState {
  return { ...state, phase: "settled" };
}

/** Seed host (+ workspace) from roots, then settle. null = waiting on data. */
function locationStep(
  state: ProjectPrefillState,
  inputs: ProjectPrefillInputs,
  writes: ProjectPrefillWrites,
): ProjectPrefillState | null {
  if (state.phase !== "location") return null;
  const { hosts, selectedHostId, config, roots } = inputs;

  if (roots === undefined) return null;
  if (roots === null) return settled(state);

  // A stored sandbox default: select the sandbox (gated on the server still
  // offering it), unless the user already picked a host / the sandbox. The
  // sandbox sentinel is never a real host id, so it's handled before the
  // host-list lookup below. Mirrors the composer's last-choice sandbox path.
  if (config?.hostId === SANDBOX_HOST_CHOICE) {
    if (inputs.managedSandboxesEnabled && !inputs.sandboxSelected && selectedHostId === null) {
      writes.selectSandbox = true;
    }
    return settled(state);
  }

  if (hosts === undefined) return null;
  const reason = roots.default_host_reason;
  const defaultHostId = roots.default_host_id;
  const hostId =
    selectedHostId ??
    ((reason === "config" || reason === "single_root") &&
    defaultHostId &&
    hosts.some((host) => host.host_id === defaultHostId)
      ? defaultHostId
      : null);
  if (hostId !== null && !inputs.sandboxSelected) {
    if (selectedHostId === null) writes.hostId = hostId;
    writes.workspace = roots.roots.find((root) => root.host_id === hostId)?.workspace;
  }
  return settled(state);
}
