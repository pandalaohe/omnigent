// New Chat seeding from the server calling-defaults chain (K8).
//
// A project visit resolves its agent / model / effort through
// `GET /v1/calling-defaults/resolve` once the host is known, so the chain
// stays server-side. Carry-over ON prefers `calling_last`'s last agent for
// this project × host, and that agent's model / effort, but only while the
// host still offers them; each field falls back to the resolve result on its
// own. Touched-field rules (D28, D30): a user pick survives a host switch
// only when the new host still offers it — otherwise the resolved value
// replaces it with a notice the composer shows. Nothing here mutates a
// session; the dialog sends the values it shows.

import {
  listCallingDefaultCatalogs,
  resolveCallingDefaults,
  type CallingDefaultsCatalogRow,
  type CallingDefaultsProblem,
  type CallingDefaultsResolution,
} from "./callingDefaultsApi";
import { readCallingLast, type CallingLastHostEntry } from "./callingDefaults";
import { effortLevelsFor } from "./modelEffortOptions";

export interface CallingSeedTouched {
  agent: boolean;
  model: boolean;
  effort: boolean;
}

/** The composer's current picks; "" means "no override". */
export interface CallingSeedCurrent {
  agentId: string | null;
  model: string;
  effort: string;
}

export interface CallingSeed {
  agentId: string | null;
  model: string | null;
  effort: string | null;
  problems: CallingDefaultsProblem[];
  notices: string[];
}

export interface CallingSeedInput {
  projectId: string;
  /** `null` on a hostless target (a managed sandbox): project layers only. */
  hostId: string | null;
  hostLabel: string;
  current: CallingSeedCurrent;
  touched: CallingSeedTouched;
  /** Owner's carry-over switch; off = resolve only. */
  carryEnabled: boolean;
  /** Last-used entry for this project × host, when carry-over is on. */
  carry: CallingLastHostEntry | null;
  /** Whether an agent is selectable and ready on this host. */
  isAgentUsable: (agentId: string) => boolean;
  /** Display label for an agent id; `null` names the resolved default. */
  agentLabel: (agentId: string | null) => string;
}

/** The carry-over switch and this project × host's entry, read synchronously. */
export function callingLastContext(
  projectId: string,
  hostId: string,
): { enabled: boolean; carry: CallingLastHostEntry | null } {
  const last = readCallingLast();
  return {
    enabled: last.enabled,
    carry: last.projects[`p:${projectId}`]?.[hostId] ?? null,
  };
}

/**
 * A missing / stale / empty catalog cannot judge a value (matching the
 * server's `check_offered`), so an unverifiable value counts as offered.
 */
function isModelOffered(catalog: CallingDefaultsCatalogRow | undefined, model: string): boolean {
  if (!catalog || catalog.error || catalog.models.length === 0) return true;
  return catalog.models.some((row) => row.id === model || row.model === model);
}

function isEffortOffered(
  catalog: CallingDefaultsCatalogRow | undefined,
  harness: string | null,
  model: string | null,
  effort: string,
): boolean {
  if (!catalog || catalog.error || catalog.models.length === 0) return true;
  const ladder = effortLevelsFor(harness, catalog.models, model);
  if (ladder === null || ladder.length === 0) return true;
  return ladder.includes(effort);
}

function quoted(value: string | null): string {
  return value === null ? "the default" : `"${value}"`;
}

function modelNotice(oldValue: string, next: string | null, hostLabel: string): string {
  return `Model "${oldValue}" is not available on ${hostLabel}; using ${quoted(next)}.`;
}

function effortNotice(oldValue: string, next: string | null, hostLabel: string): string {
  return `Effort "${oldValue}" is not available on ${hostLabel}; using ${quoted(next)}.`;
}

/**
 * Resolve one seed for (project, host, effective agent).
 *
 * Fetches the base resolution (no agent), keeps a touched agent that is still
 * usable, then fetches the resolution for the effective agent when it differs
 * so model / effort come from that agent's harness. The catalog is read only
 * when an offered check is actually needed (carry-over values or touched
 * fields); a failed catalog read degrades to "unverifiable" rather than
 * blocking the seed.
 */
export async function resolveCallingSeed(input: CallingSeedInput): Promise<CallingSeed> {
  const { projectId, hostId, hostLabel, current, touched } = input;
  // A hostless target has no `calling_last` entry to prefer and no catalog to
  // judge against; resolve alone seeds it.
  const carry = hostId === null ? null : input.carry;
  const base = await resolveCallingDefaults({ projectId, hostId });
  const notices: string[] = [];

  let agentId: string | null;
  if (touched.agent && current.agentId !== null) {
    if (input.isAgentUsable(current.agentId)) {
      agentId = current.agentId;
    } else {
      agentId = base.agent_id;
      if (agentId !== current.agentId) {
        notices.push(
          `${input.agentLabel(current.agentId)} is not available on ${hostLabel}; using ${input.agentLabel(agentId)}.`,
        );
      }
    }
  } else if (
    input.carryEnabled &&
    carry?.last_agent_id &&
    input.isAgentUsable(carry.last_agent_id)
  ) {
    agentId = carry.last_agent_id;
  } else {
    agentId = base.agent_id;
  }

  let resolution: CallingDefaultsResolution = base;
  if (agentId !== null && agentId !== base.agent_id) {
    resolution = await resolveCallingDefaults({ projectId, hostId, agentId });
  }
  // The switch gates carry-over; a remembered entry is only a preference while
  // the toggle is on, even when it names the resolved default.
  const carriedEntry =
    input.carryEnabled && agentId !== null && carry?.last_agent_id === agentId
      ? carry.agents[agentId]
      : undefined;
  const carryModel = carriedEntry?.model ?? null;
  const carryEffort = carriedEntry?.effort ?? null;

  let catalog: CallingDefaultsCatalogRow | undefined;
  if (touched.model || touched.effort || carryModel !== null || carryEffort !== null) {
    let rows: CallingDefaultsCatalogRow[];
    try {
      rows = await listCallingDefaultCatalogs();
    } catch {
      // The offered check is a courtesy; resolve alone still seeds.
      rows = [];
    }
    catalog =
      hostId === null
        ? undefined
        : rows.find((row) => row.host_id === hostId && row.harness === resolution.harness);
  }

  let model: string | null;
  let modelFromResolution = false;
  if (touched.model) {
    if (current.model === "" || isModelOffered(catalog, current.model)) {
      model = current.model === "" ? null : current.model;
    } else {
      model = resolution.model;
      modelFromResolution = true;
      notices.push(modelNotice(current.model, model, hostLabel));
    }
  } else if (carryModel !== null && isModelOffered(catalog, carryModel)) {
    model = carryModel;
  } else {
    model = resolution.model;
    modelFromResolution = true;
  }

  let effort: string | null;
  let effortFromResolution = false;
  if (touched.effort) {
    if (
      current.effort === "" ||
      isEffortOffered(catalog, resolution.harness, model, current.effort)
    ) {
      effort = current.effort === "" ? null : current.effort;
    } else {
      effort = resolution.effort;
      effortFromResolution = true;
      notices.push(effortNotice(current.effort, effort, hostLabel));
    }
  } else if (
    carryEffort !== null &&
    isEffortOffered(catalog, resolution.harness, model, carryEffort)
  ) {
    effort = carryEffort;
  } else {
    effort = resolution.effort;
    effortFromResolution = true;
  }

  const problems = (resolution.problems ?? []).filter(
    (problem) =>
      (problem.field !== "model" || modelFromResolution) &&
      (problem.field !== "effort" || effortFromResolution) &&
      // New Chat launches a saved joint agent through its library path, so
      // the resolve preview's "server-side creates cannot launch it" does not
      // apply to this surface.
      !(problem.field === "agent" && (agentId?.startsWith("ca_") ?? false)),
  );

  return { agentId, model, effort, problems, notices };
}
