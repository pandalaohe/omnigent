import { useState } from "react";
import type { RouteMode } from "./taskBoardRoutes";

export const BOARD_PREFERENCES_KEY = "omnigent.taskBoard.display";
export interface BoardPreferences {
  view: "sessions" | "routes";
  mode: RouteMode;
  focus: string[];
  aliases: Record<string, string>;
  hiddenSources: string[];
  pinnedQuota: string[];
}
const defaults: BoardPreferences = {
  view: "sessions",
  mode: "order",
  focus: [],
  aliases: {},
  hiddenSources: [],
  pinnedQuota: [],
};
function readPreferences(): BoardPreferences {
  try {
    const value = JSON.parse(localStorage.getItem(BOARD_PREFERENCES_KEY) ?? "null");
    if (!value || typeof value !== "object") return defaults;
    return {
      view: value.view === "routes" ? "routes" : "sessions",
      mode: value.mode === "time" ? "time" : "order",
      focus: Array.isArray(value.focus)
        ? value.focus.filter((item: unknown) => typeof item === "string")
        : [],
      hiddenSources: Array.isArray(value.hiddenSources)
        ? value.hiddenSources.filter((item: unknown) => typeof item === "string")
        : [],
      pinnedQuota: Array.isArray(value.pinnedQuota)
        ? value.pinnedQuota.filter((item: unknown) => typeof item === "string").slice(-4)
        : [],
      aliases:
        value.aliases && typeof value.aliases === "object"
          ? Object.fromEntries(
              Object.entries(value.aliases).filter(
                (entry): entry is [string, string] => typeof entry[1] === "string",
              ),
            )
          : {},
    };
  } catch {
    return defaults;
  }
}
export function useBoardPreferences() {
  const [preferences, setPreferences] = useState(readPreferences);
  const update = (change: Partial<BoardPreferences>) =>
    setPreferences((current) => {
      const next = { ...current, ...change };
      try {
        localStorage.setItem(BOARD_PREFERENCES_KEY, JSON.stringify(next));
      } catch {
        /* Display preferences remain usable when storage is disabled. */
      }
      return next;
    });
  return [preferences, update] as const;
}
