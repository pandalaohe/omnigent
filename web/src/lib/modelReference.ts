import { comparableModelId, findNativeModelOption } from "./codexNativeModels";
import type { NativeModelOption } from "./types";

export interface ModelIdentity {
  provider: string | null;
  model: string;
}

export function splitModelId(id: string): ModelIdentity {
  const slash = id.indexOf("/");
  return slash < 0
    ? { provider: null, model: id }
    : { provider: id.slice(0, slash), model: id.slice(slash + 1) };
}

export interface ModelResolutionNote {
  setting: string;
  saved: string;
  model: string;
  outcome: string;
  host: string;
}

export function modelResolutionNotes(labels: Record<string, string> = {}): ModelResolutionNote[] {
  const key = "omnigent.model_resolution";
  try {
    let record = JSON.parse(labels[key] ?? "null");
    if (record && typeof record === "object" && !Array.isArray(record)) {
      const count = record.chunks;
      if (!Number.isSafeInteger(count) || count < 1 || count > Object.keys(labels).length)
        return [];
      let text = "";
      for (let index = 0; index < count; index++) {
        if (typeof labels[`${key}.${index}`] !== "string") return [];
        text += labels[`${key}.${index}`];
      }
      record = JSON.parse(text);
    }
    const notes = Array.isArray(record)
      ? record.filter(
          (note): note is ModelResolutionNote =>
            note &&
            typeof note.saved === "string" &&
            typeof note.model === "string" &&
            typeof note.outcome === "string" &&
            typeof note.host === "string" &&
            typeof note.setting === "string",
        )
      : [];
    const seen = new Set<string>();
    return notes.filter((note) => {
      const dedupeKey = JSON.stringify([note.host, note.saved, note.model, note.outcome]);
      if (seen.has(dedupeKey)) return false;
      seen.add(dedupeKey);
      return true;
    });
  } catch {
    return [];
  }
}

export interface SessionHeaderModelState {
  requested: string | null;
  launched: string | null;
  running: string | null;
  requestedDiffers: boolean;
  runningDiffers: boolean;
  hasDifference: boolean;
}

function trimOrNull(value: string | null | undefined): string | null {
  const trimmed = typeof value === "string" ? value.trim() : "";
  return trimmed ? trimmed : null;
}

function antigravityModelIdentity(value: string): string {
  return value
    .trim()
    .replace(/\s*\((?:low|medium|high|xhigh|max)\)$/i, "")
    .replace(/[-_](?:low|medium|high|xhigh|max)$/i, "")
    .toLowerCase()
    .replace(/[^a-z0-9]/g, "");
}

function codexModelsEquivalent(
  launched: string,
  running: string,
  options?: readonly NativeModelOption[] | null,
): boolean {
  if (launched === running) return true;
  // Keep provider substitutions visible even when the model suffix matches.
  if (splitModelId(launched).provider !== splitModelId(running).provider) return false;
  const left = comparableModelId(launched);
  const right = comparableModelId(running);
  if (left !== "" && left === right) return true;
  if (options && options.length > 0) {
    const launchedOption = findNativeModelOption(options, launched);
    const runningOption = findNativeModelOption(options, running);
    if (launchedOption !== null && launchedOption === runningOption) return true;
  }
  return false;
}

export interface StreamModelDisplayRevisions {
  requested: number;
  reported: number;
  launched: number;
}

const streamRequestedModels = new Map<string, { revision: number; requested: string | null }>();
const streamReportedModels = new Map<string, { revision: number; model: string }>();
const streamLaunchedModels = new Map<string, { revision: number; launched: string | null }>();

export function streamModelDisplayRevisions(conversationId: string): StreamModelDisplayRevisions {
  return {
    requested: streamRequestedModels.get(conversationId)?.revision ?? 0,
    reported: streamReportedModels.get(conversationId)?.revision ?? 0,
    launched: streamLaunchedModels.get(conversationId)?.revision ?? 0,
  };
}

export function recordStreamRequestedModel(conversationId: string, requested: string | null): void {
  streamRequestedModels.set(conversationId, {
    revision: (streamRequestedModels.get(conversationId)?.revision ?? 0) + 1,
    requested,
  });
}

export function recordStreamReportedModel(conversationId: string, model: string): void {
  streamReportedModels.set(conversationId, {
    revision: (streamReportedModels.get(conversationId)?.revision ?? 0) + 1,
    model,
  });
}

export function recordStreamLaunchedModel(conversationId: string, launched: string | null): void {
  streamLaunchedModels.set(conversationId, {
    revision: (streamLaunchedModels.get(conversationId)?.revision ?? 0) + 1,
    launched,
  });
}

export function clearStreamModelDisplay(conversationId: string): void {
  streamRequestedModels.delete(conversationId);
  streamReportedModels.delete(conversationId);
  streamLaunchedModels.delete(conversationId);
}

/** Preserve newer confirmed live requested/running/launched updates over an older in-flight snapshot. */
export function preserveStreamModelDisplay<
  T extends {
    id: string;
    requested?: string | null;
    running?: string | null;
    launched?: string | null;
  },
>(session: T, before: StreamModelDisplayRevisions | undefined): T {
  if (before === undefined) return session;
  let next = session;
  const latestRequested = streamRequestedModels.get(session.id);
  if (latestRequested !== undefined && latestRequested.revision !== before.requested) {
    next = { ...next, requested: latestRequested.requested };
  }
  const latestReported = streamReportedModels.get(session.id);
  if (latestReported !== undefined && latestReported.revision !== before.reported) {
    next = { ...next, running: latestReported.model };
  }
  const latestLaunched = streamLaunchedModels.get(session.id);
  if (latestLaunched !== undefined && latestLaunched.revision !== before.launched) {
    next = { ...next, launched: latestLaunched.launched };
  }
  return next;
}

export function sessionHeaderModelState(
  session:
    | {
        harness?: string | null;
        requested?: string | null;
        launched?: string | null;
        running?: string | null;
        codexModelOptions?: readonly NativeModelOption[] | null;
      }
    | null
    | undefined,
): SessionHeaderModelState {
  const requested = trimOrNull(session?.requested);
  const launched = trimOrNull(session?.launched);
  const running = trimOrNull(session?.running);
  const requestedDiffers = Boolean(requested && launched && requested !== launched);
  let runningDiffers = false;
  if (launched && running) {
    runningDiffers =
      session?.harness === "codex-native" || session?.harness === "codex"
        ? !codexModelsEquivalent(launched, running, session.codexModelOptions)
        : running !== launched;
  } else if (!launched && session?.harness === "antigravity-native" && requested && running) {
    runningDiffers = antigravityModelIdentity(requested) !== antigravityModelIdentity(running);
  }
  return {
    requested,
    launched,
    running,
    requestedDiffers,
    runningDiffers,
    hasDifference: requestedDiffers || runningDiffers,
  };
}
