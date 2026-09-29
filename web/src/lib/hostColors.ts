import type { Host } from "@/hooks/useHosts";
import { sandboxOptionLabel } from "./capabilities";

/** Keys are stable storage values; the hexes are mid-tone so a badge reads on both themes. */
export type HostColorKey =
  "purple" | "blue" | "teal" | "green" | "orange" | "red" | "pink" | "gray";

export interface HostColorPaletteEntry {
  key: HostColorKey;
  hex: string;
}

export const HOST_COLORS: readonly HostColorPaletteEntry[] = [
  { key: "purple", hex: "#8250df" },
  { key: "blue", hex: "#0969da" },
  { key: "teal", hex: "#0f766e" },
  { key: "green", hex: "#1a7f37" },
  { key: "orange", hex: "#bc4c00" },
  { key: "red", hex: "#cf222e" },
  { key: "pink", hex: "#bf3989" },
  { key: "gray", hex: "#57606a" },
];

const BY_KEY = new Map<string, HostColorPaletteEntry>(
  HOST_COLORS.map((entry) => [entry.key, entry]),
);

export function isHostColorKey(value: unknown): value is HostColorKey {
  return typeof value === "string" && BY_KEY.has(value);
}

/** User picks keyed by host id; absent entries fall back to the automatic colour. */
export type HostColorPreferences = Record<string, HostColorKey>;

export const LOCAL_HOST_LABEL = "Local machine";

/**
 * Display name for a host id, mirroring ``resolveHostBadge``'s naming:
 * sandbox provider label, else the host's own name, else the raw id
 * (shared sessions / not-yet-loaded hosts). No id means the local machine.
 */
export function hostDisplayName(hostId: string | null | undefined, host: Host | undefined): string {
  if (!hostId || hostId === "local") return LOCAL_HOST_LABEL;
  if (!host) return hostId;
  return host.sandbox_provider ? sandboxOptionLabel(host.sandbox_provider) : host.name;
}

/** FNV-1a 32-bit — a stable index for a host's automatic colour. */
function fnv1a(value: string): number {
  let hash = 0x811c9dc5;
  for (let index = 0; index < value.length; index++) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193);
  }
  return hash >>> 0;
}

/**
 * Resolve a host's badge colour: the user's palette pick when valid, else a
 * colour derived from a stable hash of the host name (so it never changes
 * between renders for the same host).
 */
export function hostColor(
  hostId: string | null | undefined,
  hostName: string | null | undefined,
  preferences: HostColorPreferences,
): HostColorPaletteEntry {
  const picked = hostId ? preferences[hostId] : undefined;
  if (picked && BY_KEY.has(picked)) return BY_KEY.get(picked) as HostColorPaletteEntry;
  const fallbackIndex = fnv1a(hostName ?? hostId ?? "local") % HOST_COLORS.length;
  return HOST_COLORS[fallbackIndex];
}
