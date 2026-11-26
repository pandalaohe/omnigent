// Every searchable Settings row, by section. A new section is a type error here until it
// lists its rows, even if it has none yet.

import type { SettingsSectionId } from "@/shell/settingsNav";

import { IMPORT_SETTINGS } from "./import";
import type { SettingsEntry } from "./types";

export const SECTION_CATALOGS: Readonly<Record<SettingsSectionId, readonly SettingsEntry[]>> = {
  appearance: [],
  agents: [],
  general: [],
  system: [],
  git: [],
  integrations: [],
  mcp: [],
  shortcuts: [],
  import: IMPORT_SETTINGS,
  "api-keys": [],
  account: [],
  admin: [],
  members: [],
  policies: [],
  instructions: [],
  sharing: [],
  archived: [],
  cli: [],
  updates: [],
  "model-providers": [],
  browser: [],
};

export const SETTINGS_ENTRIES: readonly SettingsEntry[] = Object.values(SECTION_CATALOGS).flat();

const ENTRIES_BY_ID = new Map(SETTINGS_ENTRIES.map((entry) => [entry.id, entry]));

export function settingsEntryById(id: string): SettingsEntry | undefined {
  return ENTRIES_BY_ID.get(id);
}
