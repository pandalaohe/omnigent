import type { Host } from "@/hooks/useHosts";
import { SANDBOX_HOST_CHOICE } from "@/lib/hostPreferences";
import type { ProjectHostRoots } from "@/lib/projectsApi";

type ProjectPrefillPhase = "location" | "settled";

/** Project config supplies placement hints; host roots supply real-host placement. */
export interface ProjectPrefillConfig {
  hostId?: string;
  workspace?: string;
  /** Opt-in worktree default; only `true` is meaningful (absent = no worktree). */
  useWorktree?: boolean;
}

export interface ProjectPrefillState {
  /** Project this machine is seeding for; "" = plain visit (starts done). */
  project: string;
  /** Location track: seed host/workspace from roots, then settle. The agent /
   *  model / effort seeds come from the server calling-defaults chain once the
   *  host is known (see `callingDefaultsSeed`), not from this machine. */
  phase: ProjectPrefillPhase;
}

export function initialPrefillState(project: string): ProjectPrefillState {
  return {
    project,
    phase: project === "" ? "settled" : "location",
  };
}

/** True once the location track is done and stepping is a no-op. */
export function prefillDone(state: ProjectPrefillState): boolean {
  return state.phase === "settled";
}

interface ProjectPrefillInputs {
  hosts: Host[] | undefined;
  sandboxSelected: boolean;
  /** Whether the server offers managed sandbox hosts. Gates seeding a stored
   *  sandbox default — an OSS server that no longer offers it drops the hint. */
  managedSandboxesEnabled: boolean;
  /** Live host pick; a mid-flight manual switch aborts the default host seeding
   *  so a stored host can't clobber the user's own choice. */
  selectedHostId: string | null;
  /** undefined waits for the config query; {} has no config hints. */
  config: ProjectPrefillConfig | undefined;
  /** undefined waits for a first-class project's roots; null is a label-only folder. */
  roots: ProjectHostRoots | null | undefined;
}

interface ProjectPrefillWrites {
  hostId?: string;
  workspace?: string;
  /** Select the managed sandbox as the target (config.hostId was the sandbox
   *  sentinel). Distinct from `hostId` — the sandbox is not a real host id. */
  selectSandbox?: boolean;
}

/**
 * One transition. null = keep waiting for data; otherwise the next state
 * plus slot writes to apply (fill-empty-only; the component enforces that).
 */
export function projectPrefillStep(
  state: ProjectPrefillState,
  inputs: ProjectPrefillInputs,
): { state: ProjectPrefillState; writes: ProjectPrefillWrites } | null {
  const writes: ProjectPrefillWrites = {};

  // Wait for config before the location track can choose the sandbox branch.
  if (inputs.config === undefined) return null;

  const next = locationStep(state, inputs, writes);
  if (next === null) return null;
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
