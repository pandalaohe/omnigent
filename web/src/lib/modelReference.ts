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
    return Array.isArray(record)
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
  } catch {
    return [];
  }
}
