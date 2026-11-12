import { createContext, useContext, useState } from "react";
import { isThemePalette, type ThemePalette } from "@/lib/themePalette";
import type { RouteMode } from "./taskBoardRouteData";

export const BOARD_PREFERENCES_KEY = "omnigent.taskBoard.display";
export const boardViews = ["sessions", "routes", "log", "history", "today"] as const;
export type BoardView = (typeof boardViews)[number];
export interface BoardPreferences {
  view: BoardView | "settings";
  mode: RouteMode;
  defaultView: BoardView;
  defaultMode: RouteMode;
  theme: "system" | "light" | "dark";
  palette: ThemePalette;
  refresh: number;
  lodStation: number;
  lodPhase: number;
  boardHours: number;
  watch: number;
  focus: string[];
  aliases: Record<string, string>;
  hiddenSources: string[];
  pinnedQuota: string[];
}
export const boardDefaults: BoardPreferences = {
  view: "sessions",
  mode: "order",
  focus: [],
  aliases: {},
  hiddenSources: [],
  pinnedQuota: [],
  defaultView: "sessions",
  defaultMode: "order",
  theme: "system",
  palette: "omni",
  refresh: 60,
  lodStation: 2,
  lodPhase: 14,
  boardHours: 24,
  watch: 80,
};
const choice = (value: unknown, options: number[], fallback: number) =>
  typeof value === "number" && options.includes(value) ? value : fallback;
type StorageState = "local" | "memory" | "invalid";
function readPreferences(): { preferences: BoardPreferences; storage: StorageState } {
  try {
    const value = JSON.parse(localStorage.getItem(BOARD_PREFERENCES_KEY) ?? "{}");
    if (!value || typeof value !== "object" || Array.isArray(value))
      return { preferences: boardDefaults, storage: "invalid" };
    const defaultView = boardViews.includes(value.defaultView) ? value.defaultView : "sessions";
    const defaultMode = value.defaultMode === "time" ? "time" : "order";
    return {
      storage: "local",
      preferences: {
        ...boardDefaults,
        defaultView,
        defaultMode,
        view: defaultView,
        mode: defaultMode,
        theme: ["light", "dark"].includes(value.theme) ? value.theme : "system",
        palette: isThemePalette(value.palette) ? value.palette : "omni",
        refresh: choice(value.refresh, [0, 30, 60, 300], 60),
        lodStation: choice(value.lodStation, [1, 2, 3], 2),
        lodPhase: choice(value.lodPhase, [7, 14, 30], 14),
        boardHours: choice(value.boardHours, [12, 24, 48, 72], 24),
        watch: choice(value.watch, [60, 70, 75, 80, 85, 90], 80),
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
      },
    };
  } catch (error) {
    return {
      preferences: boardDefaults,
      storage: error instanceof SyntaxError ? "invalid" : "memory",
    };
  }
}
export function useBoardPreferences() {
  const [state, setState] = useState(readPreferences);
  const update = (change: Partial<BoardPreferences>): boolean => {
    const next = { ...state.preferences, ...change };
    let storage: StorageState = "local";
    try {
      localStorage.setItem(BOARD_PREFERENCES_KEY, JSON.stringify(next));
    } catch {
      storage = "memory";
    }
    setState({ preferences: next, storage });
    return storage === "local";
  };
  return [state.preferences, update, state.storage] as const;
}

export const BoardRefreshContext = createContext(60);
export function useBoardCadence() {
  const seconds = useContext(BoardRefreshContext);
  return () =>
    document.visibilityState === "visible" && seconds > 0 ? seconds * 1000 : (false as const);
}
