import type { ProjectConfig } from "./projectsApi";

export const ALL_HOSTS_BLOCK_KEY = "*";

/** Return blocked catalog agent ids for one host: `"*"` union `[hostId]`. */
export function projectBlockedAgents(
  config: ProjectConfig | undefined,
  hostId: string | null,
): Set<string> {
  const blocked = new Set<string>();
  const raw = config?.blocked_agents;
  if (!raw || typeof raw !== "object") return blocked;
  const keys =
    hostId && hostId !== ALL_HOSTS_BLOCK_KEY
      ? [ALL_HOSTS_BLOCK_KEY, hostId]
      : [ALL_HOSTS_BLOCK_KEY];
  for (const key of keys) {
    const list = raw[key];
    if (!Array.isArray(list)) continue;
    for (const item of list) {
      if (typeof item === "string" && item.trim() !== "") blocked.add(item);
    }
  }
  return blocked;
}

/** Drop empty host lists and duplicates; return `undefined` when nothing is blocked. */
export function normalizeBlockedAgents(
  raw: Record<string, string[]> | undefined,
): Record<string, string[]> | undefined {
  if (!raw) return undefined;
  const normalized: Record<string, string[]> = {};
  for (const [hostKey, list] of Object.entries(raw)) {
    const trimmedKey = hostKey.trim();
    if (!trimmedKey || !Array.isArray(list)) continue;
    const seen = new Set<string>();
    const clean: string[] = [];
    for (const item of list) {
      if (typeof item !== "string" || item.trim() === "" || seen.has(item)) continue;
      seen.add(item);
      clean.push(item);
    }
    if (clean.length > 0) normalized[trimmedKey] = clean;
  }
  return Object.keys(normalized).length > 0 ? normalized : undefined;
}
