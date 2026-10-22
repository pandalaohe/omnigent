import { useCallback, useEffect, useState } from "react";
import { queueUserPreferencePatch } from "./userPreferencesSync";

const STORAGE_KEY = "omnigent:instruction-grid";
const CHANGED_EVENT = "omnigent:instruction-grid-changed";

export interface InstructionGridLayout {
  hosts: string[];
  agents: string[];
}

export function normalizeInstructionGridPreferences(value: unknown): InstructionGridLayout {
  const list = (values: unknown): string[] =>
    Array.isArray(values)
      ? [
          ...new Set(
            values.filter(
              (id): id is string => typeof id === "string" && id.length > 0 && id.length <= 512,
            ),
          ),
        ].slice(0, 256)
      : [];
  if (!value || typeof value !== "object" || Array.isArray(value)) return { hosts: [], agents: [] };
  const layout = value as Record<string, unknown>;
  return { hosts: list(layout.hosts), agents: list(layout.agents) };
}

export function readInstructionGridPreferences(): InstructionGridLayout {
  try {
    return normalizeInstructionGridPreferences(
      JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? "null"),
    );
  } catch {
    return { hosts: [], agents: [] };
  }
}

export function writeInstructionGridPreferences(value: InstructionGridLayout): void {
  if (typeof window === "undefined") return;
  const layout = normalizeInstructionGridPreferences(value);
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(layout));
  } catch {
    // Storage denial or quota exhaustion must not break the grid.
  }
  window.dispatchEvent(new Event(CHANGED_EVENT));
  try {
    queueUserPreferencePatch("instruction_grid", layout);
  } catch {
    // Sync also uses localStorage, which may be unavailable.
  }
}

export function useInstructionGridPreferences() {
  const [layout, setLayout] = useState(readInstructionGridPreferences);
  useEffect(() => {
    const sync = () => setLayout(readInstructionGridPreferences());
    window.addEventListener(CHANGED_EVENT, sync);
    window.addEventListener("storage", sync);
    return () => {
      window.removeEventListener(CHANGED_EVENT, sync);
      window.removeEventListener("storage", sync);
    };
  }, []);
  const write = useCallback(
    (value: InstructionGridLayout) => writeInstructionGridPreferences(value),
    [],
  );
  return [layout, write] as const;
}
