import { useCallback, useRef, useState } from "react";
import { taskIdentity, type BoardTask } from "./taskBoardData";
import { DAY } from "./taskBoardRouteData";
import { routeState } from "./taskBoardRouteModel";

export const ROUTE_VIEW_KEY = "omnigent.taskBoard.routeView";
export interface RoutePins {
  groups: string[];
  tasks: string[];
}
export interface RouteViewSettings {
  /** "attention" shows only rows with something to look at; "all" shows every row. */
  scope: "attention" | "all";
  rows: "compact" | "final";
  pins: RoutePins;
}
export const routeViewDefaults: RouteViewSettings = {
  scope: "attention",
  rows: "compact",
  pins: { groups: [], tasks: [] },
};
const strings = (value: unknown) =>
  Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
export function readRouteView(): RouteViewSettings {
  try {
    const value = JSON.parse(localStorage.getItem(ROUTE_VIEW_KEY) ?? "{}");
    return {
      scope: value?.scope === "all" ? "all" : "attention",
      rows: value?.rows === "final" ? "final" : "compact",
      pins: { groups: strings(value?.pins?.groups), tasks: strings(value?.pins?.tasks) },
    };
  } catch {
    return routeViewDefaults;
  }
}
/** Browser-local route view choices; a storage failure keeps them for this page only. */
export function useRouteViewSettings() {
  const [settings, setSettings] = useState(readRouteView);
  const update = useCallback((patch: Partial<RouteViewSettings>) => {
    setSettings((current) => {
      const next = { ...current, ...patch };
      try {
        localStorage.setItem(ROUTE_VIEW_KEY, JSON.stringify(next));
      } catch {
        // Private windows and full storage keep the choice in memory.
      }
      return next;
    });
  }, []);
  return [settings, update] as const;
}

/** Latest time the card itself reports a change. */
export function routeChanged(task: BoardTask): number {
  const display = task["omnigent.ai:sourceDisplay"];
  return Math.max(
    0,
    ...[
      task.updated,
      task.progressUpdated,
      task["omnigent.ai:closedSeen"],
      display?.lastActivity,
      display?.historyState?.end ?? display?.historyState?.last,
    ].map((value) => (value ? Date.parse(value) || 0 : 0)),
  );
}
export const ROUTE_RECENT = 3 * DAY;
/** Cards worth a station in the attention view: live, waiting, blocked, failed, recent or pinned. */
export function routeWorthShowing(task: BoardTask, now: number, pins: RoutePins): boolean {
  return (
    ["running", "user", "blocked", "failed"].includes(routeState(task)) ||
    routeChanged(task) >= now - ROUTE_RECENT ||
    pins.tasks.includes(taskIdentity(task))
  );
}

/** Keep the previous array while every element is the same object, so memoized work survives. */
export function useStableList<T>(list: T[]): T[] {
  const previous = useRef(list);
  const last = previous.current;
  if (last !== list && (last.length !== list.length || list.some((item, i) => item !== last[i])))
    previous.current = list;
  return previous.current;
}
