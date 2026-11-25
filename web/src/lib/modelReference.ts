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

export function sessionHeaderModelState(
  session:
    | {
        harness?: string | null;
        requested?: string | null;
        launched?: string | null;
        running?: string | null;
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
    runningDiffers = running !== launched;
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
