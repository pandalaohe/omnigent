// Sidebar layout model: the ordered sections a user arranges in the sidebar.
// Pure functions only; localStorage is the synchronous read path and
// userPreferencesSync mirrors the whole value to the account. Nothing is ever
// pruned automatically: refs that no longer resolve are kept and skipped at
// render until an explicit user action removes them.

import { nanoid } from "nanoid";

export type SectionKind = "projects" | "favorites" | "recent" | "other_projects" | "other_sessions";

export interface FavoriteRef {
  type: "session" | "project";
  id: string;
}

export interface SidebarSectionDef {
  id: string;
  kind: SectionKind;
  name: string;
  maxRows: number | null;
  projectIds?: string[];
  items?: FavoriteRef[];
  count?: number;
  /** The default-layout favorites section stays implicit until the user edits. */
  implicit?: true;
}

export interface SidebarLayout {
  version: 1;
  sections: SidebarSectionDef[];
}

const MAX_SECTIONS = 50;
const MAX_SECTION_ENTRIES = 200;
const MIN_RECENT_COUNT = 1;
const MAX_RECENT_COUNT = 20;
const DEFAULT_RECENT_COUNT = 5;
const MAX_ROWS_VALUES = new Set([5, 10, 15, 20, 30]);
const SECTION_KINDS = new Set<SectionKind>([
  "projects",
  "favorites",
  "recent",
  "other_projects",
  "other_sessions",
]);

// Stable ids of the default sections. Exported so the legacy title-keyed
// collapse preference can be migrated onto them.
export const DEFAULT_FAVORITES_SECTION_ID = "default-favorites";
export const DEFAULT_OTHER_PROJECTS_SECTION_ID = "default-other-projects";
export const DEFAULT_OTHER_SESSIONS_SECTION_ID = "default-other-sessions";

/** Today's sidebar: implicit Pinned, Projects, Sessions — all uncapped. */
export function defaultLayout(): SidebarLayout {
  return {
    version: 1,
    sections: [
      {
        id: DEFAULT_FAVORITES_SECTION_ID,
        kind: "favorites",
        name: "Pinned",
        maxRows: null,
        items: [],
        implicit: true,
      },
      {
        id: DEFAULT_OTHER_PROJECTS_SECTION_ID,
        kind: "other_projects",
        name: "Projects",
        maxRows: null,
      },
      {
        id: DEFAULT_OTHER_SESSIONS_SECTION_ID,
        kind: "other_sessions",
        name: "Sessions",
        maxRows: null,
      },
    ],
  };
}

export function newSectionId(): string {
  return `sec_${nanoid(10)}`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function refKey(ref: FavoriteRef): string {
  return `${ref.type}:${ref.id}`;
}

function normalizeMaxRows(raw: unknown): number | null {
  return typeof raw === "number" && MAX_ROWS_VALUES.has(raw) ? raw : null;
}

function normalizeRecentCount(raw: unknown): number {
  if (typeof raw !== "number" || !Number.isFinite(raw)) return DEFAULT_RECENT_COUNT;
  return Math.min(MAX_RECENT_COUNT, Math.max(MIN_RECENT_COUNT, Math.floor(raw)));
}

function favoriteRef(raw: unknown): FavoriteRef | null {
  if (!isRecord(raw)) return null;
  if (raw.type !== "session" && raw.type !== "project") return null;
  if (typeof raw.id !== "string" || raw.id === "") return null;
  return { type: raw.type, id: raw.id };
}

function normalizeFavoriteRefs(raw: unknown): FavoriteRef[] {
  if (!Array.isArray(raw)) return [];
  const refs: FavoriteRef[] = [];
  const seen = new Set<string>();
  for (const entry of raw) {
    if (refs.length === MAX_SECTION_ENTRIES) break;
    const ref = favoriteRef(entry);
    if (ref === null || seen.has(refKey(ref))) continue;
    seen.add(refKey(ref));
    refs.push(ref);
  }
  return refs;
}

function normalizeProjectIds(raw: unknown): string[] {
  if (!Array.isArray(raw)) return [];
  const ids: string[] = [];
  const seen = new Set<string>();
  for (const entry of raw) {
    if (ids.length === MAX_SECTION_ENTRIES) break;
    if (typeof entry !== "string" || entry === "" || seen.has(entry)) continue;
    seen.add(entry);
    ids.push(entry);
  }
  return ids;
}

function normalizeSectionEntry(raw: unknown): SidebarSectionDef | null {
  if (!isRecord(raw)) return null;
  const { id, kind, name } = raw;
  if (typeof id !== "string" || id === "") return null;
  if (typeof kind !== "string" || !SECTION_KINDS.has(kind as SectionKind)) return null;
  if (typeof name !== "string") return null;
  const section: SidebarSectionDef = {
    id,
    kind: kind as SectionKind,
    name,
    maxRows: normalizeMaxRows(raw.maxRows),
  };
  if (raw.implicit === true) section.implicit = true;
  switch (section.kind) {
    case "projects":
      section.projectIds = normalizeProjectIds(raw.projectIds);
      break;
    case "favorites":
      section.items = normalizeFavoriteRefs(raw.items);
      break;
    case "recent":
      section.count = normalizeRecentCount(raw.count);
      break;
    default:
      break;
  }
  return section;
}

function mergeFavoriteItems(target: SidebarSectionDef, incoming: FavoriteRef[]): void {
  const items = target.items ?? [];
  const seen = new Set(items.map(refKey));
  for (const ref of incoming) {
    if (items.length === MAX_SECTION_ENTRIES) break;
    if (seen.has(refKey(ref))) continue;
    seen.add(refKey(ref));
    items.push(ref);
  }
  target.items = items;
}

/**
 * Coerce an untrusted stored value into a valid layout: unknown kinds and
 * malformed sections are dropped, ids and refs are deduped, counts and caps
 * are bounded, and a project id survives only in its first projects section.
 * An absent or unusable value falls back to the default layout; an explicit
 * empty section list stays empty.
 */
export function normalizeLayout(raw: unknown): SidebarLayout {
  if (!isRecord(raw) || raw.version !== 1 || !Array.isArray(raw.sections)) {
    return defaultLayout();
  }

  const sections: SidebarSectionDef[] = [];
  const seenIds = new Set<string>();
  const seenSingletonKinds = new Set<SectionKind>();
  const claimedProjectIds = new Set<string>();
  let favoritesIndex = -1;

  for (const rawSection of raw.sections) {
    if (!isRecord(rawSection)) continue;
    if (typeof rawSection.id === "string" && seenIds.has(rawSection.id)) continue;
    const section = normalizeSectionEntry(rawSection);
    if (section === null) continue;
    seenIds.add(section.id);

    if (section.kind === "projects") {
      const projectIds: string[] = [];
      for (const projectId of section.projectIds ?? []) {
        if (claimedProjectIds.has(projectId)) continue;
        claimedProjectIds.add(projectId);
        projectIds.push(projectId);
      }
      section.projectIds = projectIds;
    } else if (section.kind === "favorites") {
      const kept = favoritesIndex >= 0 ? sections[favoritesIndex] : undefined;
      if (kept !== undefined) {
        mergeFavoriteItems(kept, section.items ?? []);
        continue;
      }
      favoritesIndex = sections.length;
    } else if (seenSingletonKinds.has(section.kind)) {
      continue;
    } else {
      seenSingletonKinds.add(section.kind);
    }
    sections.push(section);
  }

  return { version: 1, sections: sections.slice(0, MAX_SECTIONS) };
}

export function sectionOfProject(layout: SidebarLayout, projectId: string): string | null {
  for (const section of layout.sections) {
    if (section.kind === "projects" && section.projectIds?.includes(projectId)) {
      return section.id;
    }
  }
  return null;
}

/** Move a project into one projects section, or out of every one (null). */
export function moveProjectToSection(
  layout: SidebarLayout,
  projectId: string,
  sectionId: string | null,
): SidebarLayout {
  const target =
    sectionId === null ? undefined : layout.sections.find((section) => section.id === sectionId);
  if (sectionId !== null && (target === undefined || target.kind !== "projects")) return layout;
  let changed = false;
  const sections = layout.sections.map((section) => {
    if (section.kind !== "projects") return section;
    const projectIds = section.projectIds ?? [];
    const remaining = projectIds.filter((id) => id !== projectId);
    const nextIds =
      target !== undefined && section.id === target.id ? [...remaining, projectId] : remaining;
    if (nextIds.length === projectIds.length) return section;
    changed = true;
    return { ...section, projectIds: nextIds };
  });
  return changed ? { ...layout, sections } : layout;
}

/** Insert a section at *index*; a second favorites / recent / system kind is refused. */
export function insertSection(
  layout: SidebarLayout,
  def: SidebarSectionDef,
  index = 0,
): SidebarLayout {
  if (def.kind !== "projects" && layout.sections.some((section) => section.kind === def.kind)) {
    return layout;
  }
  const at = Math.max(0, Math.min(index, layout.sections.length));
  const sections = [...layout.sections];
  sections.splice(at, 0, def);
  return { ...layout, sections };
}

export function moveSection(
  layout: SidebarLayout,
  sectionId: string,
  to: "up" | "down" | "top" | "bottom" | number,
): SidebarLayout {
  const from = layout.sections.findIndex((section) => section.id === sectionId);
  if (from < 0) return layout;
  let target: number;
  switch (to) {
    case "up":
      target = from - 1;
      break;
    case "down":
      target = from + 1;
      break;
    case "top":
      target = 0;
      break;
    case "bottom":
      target = layout.sections.length - 1;
      break;
    default:
      target = Math.min(Math.max(Math.trunc(to), 0), layout.sections.length - 1);
  }
  if (target === from || target < 0 || target >= layout.sections.length) return layout;
  const sections = [...layout.sections];
  const [moved] = sections.splice(from, 1);
  sections.splice(target, 0, moved);
  return { ...layout, sections };
}

export function removeSection(layout: SidebarLayout, sectionId: string): SidebarLayout {
  const sections = layout.sections.filter((section) => section.id !== sectionId);
  return sections.length === layout.sections.length ? layout : { ...layout, sections };
}

/** Kinds the create dialog may offer: projects always, every other kind once. */
export function kindsAvailableToCreate(layout: SidebarLayout): SectionKind[] {
  const present = new Set(layout.sections.map((section) => section.kind));
  const kinds: SectionKind[] = ["projects"];
  if (!present.has("favorites")) kinds.push("favorites");
  if (!present.has("recent")) kinds.push("recent");
  if (!present.has("other_projects")) kinds.push("other_projects");
  if (!present.has("other_sessions")) kinds.push("other_sessions");
  return kinds;
}

/**
 * The rendered rows of a favorites section. Session order is always pin order
 * (old clients reorder pins; the new UI follows): the section's session refs
 * are slots, and those slots are filled, in order, by every still-pinned
 * session sorted by pin time — referenced or not. Pinned sessions past the slot
 * count append after the last row, so a section with fewer refs than pins still
 * renders every pin in order. Project refs keep their places among the slots
 * and render only while the project is known; a ref to a session that was
 * unpinned elsewhere holds no slot and is skipped.
 */
export function favoritesRows(
  section: SidebarSectionDef,
  pinnedByTime: string[],
  knownProjectIds: Set<string>,
): FavoriteRef[] {
  if (section.kind !== "favorites") return [];
  const items = section.items ?? [];
  const pinned = new Set(pinnedByTime);
  // One slot per ref to a still-pinned session; the slots take the first pins
  // in pin order, and the remaining pins append after every row.
  const slots = items.filter((ref) => ref.type === "session" && pinned.has(ref.id)).length;
  const slotted = pinnedByTime.slice(0, slots);
  const rows: FavoriteRef[] = [];
  let slot = 0;
  for (const ref of items) {
    if (ref.type === "project") {
      if (knownProjectIds.has(ref.id)) rows.push(ref);
    } else if (pinned.has(ref.id)) {
      rows.push({ type: "session", id: slotted[slot] });
      slot += 1;
    }
  }
  const consumed = new Set(slotted);
  for (const id of pinnedByTime) {
    if (!consumed.has(id)) rows.push({ type: "session", id });
  }
  return rows;
}

/**
 * Split a reordered favorites list: `items` is the new slot pattern (sessions
 * keep their places among the project refs) and `sessionOrder` is the session
 * subsequence to write back into pin timestamps.
 */
export function splitFavoritesReorder(next: FavoriteRef[]): {
  items: FavoriteRef[];
  sessionOrder: string[];
} {
  const items: FavoriteRef[] = [];
  const sessionOrder: string[] = [];
  const seen = new Set<string>();
  for (const ref of next) {
    if (seen.has(refKey(ref))) continue;
    seen.add(refKey(ref));
    items.push({ type: ref.type, id: ref.id });
    if (ref.type === "session") sessionOrder.push(ref.id);
  }
  return { items, sessionOrder };
}

/** Append a favorite, creating the one favorites section at the top if none exists. */
export function addFavorite(layout: SidebarLayout, ref: FavoriteRef): SidebarLayout {
  const index = layout.sections.findIndex((section) => section.kind === "favorites");
  if (index < 0) {
    const section: SidebarSectionDef = {
      id: newSectionId(),
      kind: "favorites",
      name: "Favorites",
      maxRows: 10,
      items: [ref],
    };
    return { version: 1, sections: [section, ...layout.sections] };
  }
  const section = layout.sections[index];
  const items = section.items ?? [];
  if (items.some((item) => refKey(item) === refKey(ref))) return layout;
  const sections = [...layout.sections];
  sections[index] = { ...section, items: [...items, ref] };
  return { ...layout, sections };
}

export function removeFavorite(layout: SidebarLayout, ref: FavoriteRef): SidebarLayout {
  let changed = false;
  const sections = layout.sections.map((section) => {
    if (section.kind !== "favorites") return section;
    const items = section.items ?? [];
    const remaining = items.filter((item) => refKey(item) !== refKey(ref));
    if (remaining.length === items.length) return section;
    changed = true;
    return { ...section, items: remaining };
  });
  return changed ? { ...layout, sections } : layout;
}
