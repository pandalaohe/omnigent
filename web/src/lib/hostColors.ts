import type { CSSProperties } from "react";

import type { Host } from "@/hooks/useHosts";
import { sandboxOptionLabel } from "./capabilities";

/**
 * Keys are stable storage values. ``hex`` is the light-theme tone and
 * ``darkHex`` the dark-theme tone, so a host colour reads on both.
 */
export type HostColorKey =
  "purple" | "blue" | "teal" | "green" | "orange" | "red" | "pink" | "gray";

export interface HostColorPaletteEntry {
  key: HostColorKey;
  hex: string;
  darkHex: string;
}

export const HOST_COLORS: readonly HostColorPaletteEntry[] = [
  { key: "purple", hex: "#8250df", darkHex: "#a371f7" },
  { key: "blue", hex: "#0969da", darkHex: "#4493f8" },
  { key: "teal", hex: "#0f766e", darkHex: "#2dd4bf" },
  { key: "green", hex: "#1a7f37", darkHex: "#3fb950" },
  { key: "orange", hex: "#bc4c00", darkHex: "#f0883e" },
  { key: "red", hex: "#cf222e", darkHex: "#f85149" },
  { key: "pink", hex: "#bf3989", darkHex: "#db61a2" },
  { key: "gray", hex: "#57606a", darkHex: "#8b949e" },
];

const BY_KEY = new Map<string, HostColorPaletteEntry>(
  HOST_COLORS.map((entry) => [entry.key, entry]),
);

/**
 * Palette entries the automatic colour may pick from. ``gray`` is
 * excluded: it is the lowest-contrast tone, and a whole rail of
 * host-hashed grey badges reads as "no colour at all". Gray remains
 * available when the user picks it explicitly.
 */
const AUTOMATIC_COLORS: readonly HostColorPaletteEntry[] = HOST_COLORS.filter(
  (entry) => entry.key !== "gray",
);

/**
 * React style setting the per-theme host colour variables for an entry.
 * Consumers must also carry the ``host-color`` class — index.css resolves
 * ``--host-color`` from these two vars per theme.
 */
export function hostColorStyle(
  entry: HostColorPaletteEntry,
): CSSProperties & Record<"--host-color-light" | "--host-color-dark", string> {
  return { "--host-color-light": entry.hex, "--host-color-dark": entry.darkHex };
}

export function isHostColorKey(value: unknown): value is HostColorKey {
  return typeof value === "string" && BY_KEY.has(value);
}

/**
 * Stored tombstone for "reset to automatic"; read exactly like an absent
 * key. A reset must write it because preference patches shallow-merge and
 * cannot delete a host's key.
 */
export const AUTO_HOST_COLOR = "auto";

/** User picks keyed by host id; absent and ``"auto"`` entries fall back to the automatic colour. */
export type HostColorPreferences = Record<string, HostColorKey | typeof AUTO_HOST_COLOR>;

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
 * between renders for the same host). The automatic path never picks gray.
 */
export function hostColor(
  hostId: string | null | undefined,
  hostName: string | null | undefined,
  preferences: HostColorPreferences,
): HostColorPaletteEntry {
  const picked = hostId ? preferences[hostId] : undefined;
  if (picked && BY_KEY.has(picked)) return BY_KEY.get(picked) as HostColorPaletteEntry;
  const fallbackIndex = fnv1a(hostName ?? hostId ?? "local") % AUTOMATIC_COLORS.length;
  return AUTOMATIC_COLORS[fallbackIndex];
}
