// Sidebar layout follows the account: localStorage is the synchronous read
// path and userPreferencesSync mirrors the whole value. A rejected server
// patch restores the stored value through the sync module.

import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";

import { defaultLayout, normalizeLayout, type SidebarLayout } from "@/lib/sidebarLayout";
import {
  queueUserPreferencePatch,
  USER_PREFERENCES_PATCH_REJECTED_EVENT,
} from "@/lib/userPreferencesSync";

const STORAGE_KEY = "omnigent:sidebar-layout";
const CHANGED_EVENT = "omnigent:sidebar-layout-changed";

/** Read and normalize the stored layout; a missing or malformed value defaults. */
function readSidebarLayout(): SidebarLayout {
  if (typeof window === "undefined") return defaultLayout();
  let raw: string | null;
  try {
    raw = window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return defaultLayout();
  }
  if (raw === null) return defaultLayout();
  let parsed: unknown = null;
  try {
    parsed = JSON.parse(raw);
  } catch {
    // Malformed JSON falls back to the default layout.
  }
  return normalizeLayout(parsed);
}

/** Drop the render-only `implicit` marker before persisting or syncing. */
function persistableLayout(layout: SidebarLayout): SidebarLayout {
  return {
    version: 1,
    sections: layout.sections.map((section) => {
      if (section.implicit !== true) return section;
      const persisted = { ...section };
      delete persisted.implicit;
      return persisted;
    }),
  };
}

function writeSidebarLayout(next: SidebarLayout): void {
  const value = persistableLayout(normalizeLayout(next));
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(value));
  } catch {
    // Storage quota / access errors must not break a layout edit.
  }
  window.dispatchEvent(new Event(CHANGED_EVENT));
  queueUserPreferencePatch("sidebar_layout", value);
}

export function useSidebarLayout(): {
  layout: SidebarLayout;
  saveLayout: (next: SidebarLayout) => void;
} {
  const [layout, setLayout] = useState<SidebarLayout>(readSidebarLayout);

  useEffect(() => {
    const sync = () => setLayout(readSidebarLayout());
    window.addEventListener(CHANGED_EVENT, sync);
    window.addEventListener("storage", sync);
    return () => {
      window.removeEventListener(CHANGED_EVENT, sync);
      window.removeEventListener("storage", sync);
    };
  }, []);

  useEffect(() => {
    const onRejected = (event: Event) => {
      const detail = (event as CustomEvent<{ namespace?: unknown }>).detail;
      if (detail?.namespace === "sidebar_layout") toast.error("Sidebar layout not saved");
    };
    window.addEventListener(USER_PREFERENCES_PATCH_REJECTED_EVENT, onRejected);
    return () => window.removeEventListener(USER_PREFERENCES_PATCH_REJECTED_EVENT, onRejected);
  }, []);

  const saveLayout = useCallback((next: SidebarLayout) => writeSidebarLayout(next), []);

  return { layout, saveLayout };
}
