import { afterEach, describe, expect, it, vi } from "vitest";
import {
  normalizeWorkspacePanelDefault,
  readDefaultWorkspacePanelOpen,
  readWidenWorkspaceForContent,
  readWorkspacePanelDefault,
  subscribeWidenWorkspaceForContent,
  WORKSPACE_PANEL_DEFAULT,
  writeDefaultWorkspacePanelOpen,
  writeWidenWorkspaceForContent,
  writeWorkspacePanelDefault,
} from "./workspacePanelPreferences";

const STORAGE_KEY = "omnigent:default-workspace-panel";
const WIDEN_KEY = "omnigent:workspace-panel-widen-for-content";

afterEach(() => {
  localStorage.clear();
});

describe("workspacePanelPreferences — read/write", () => {
  it("returns collapsed when nothing is stored", () => {
    expect(readWorkspacePanelDefault()).toBe(WORKSPACE_PANEL_DEFAULT);
    expect(readDefaultWorkspacePanelOpen()).toBe(false);
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("records the rail's visibility from the collapse/expand toggle", () => {
    writeDefaultWorkspacePanelOpen(false);
    expect(readDefaultWorkspacePanelOpen()).toBe(false);
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();

    writeDefaultWorkspacePanelOpen(true);
    expect(readDefaultWorkspacePanelOpen()).toBe(true);
    expect(localStorage.getItem(STORAGE_KEY)).toBe("open");
  });

  it("clears the key for collapsed and stores open", () => {
    writeWorkspacePanelDefault("collapsed");
    expect(readWorkspacePanelDefault()).toBe("collapsed");
    expect(readDefaultWorkspacePanelOpen()).toBe(false);
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();

    writeWorkspacePanelDefault("open");
    expect(readWorkspacePanelDefault()).toBe("open");
    expect(readDefaultWorkspacePanelOpen()).toBe(true);
    expect(localStorage.getItem(STORAGE_KEY)).toBe("open");
  });
});

describe("workspacePanelPreferences — widen for content", () => {
  it("defaults on and round-trips through storage", () => {
    expect(readWidenWorkspaceForContent()).toBe(true);
    expect(localStorage.getItem(WIDEN_KEY)).toBeNull();

    writeWidenWorkspaceForContent(false);
    expect(readWidenWorkspaceForContent()).toBe(false);
    expect(localStorage.getItem(WIDEN_KEY)).toBe("0");

    writeWidenWorkspaceForContent(true);
    expect(readWidenWorkspaceForContent()).toBe(true);
    expect(localStorage.getItem(WIDEN_KEY)).toBeNull();
  });

  it("notifies subscribers on write and stops after unsubscribe", () => {
    const listener = vi.fn();
    const unsubscribe = subscribeWidenWorkspaceForContent(listener);

    writeWidenWorkspaceForContent(false);
    expect(listener).toHaveBeenCalledTimes(1);

    unsubscribe();
    writeWidenWorkspaceForContent(true);
    expect(listener).toHaveBeenCalledTimes(1);
  });
});

describe("normalizeWorkspacePanelDefault", () => {
  it("passes through valid values", () => {
    expect(normalizeWorkspacePanelDefault("open")).toBe("open");
    expect(normalizeWorkspacePanelDefault("collapsed")).toBe("collapsed");
  });

  it("maps unknown, null, and garbage to collapsed", () => {
    expect(normalizeWorkspacePanelDefault("closed")).toBe("collapsed");
    expect(normalizeWorkspacePanelDefault("bogus")).toBe("collapsed");
    expect(normalizeWorkspacePanelDefault(null)).toBe("collapsed");
    expect(normalizeWorkspacePanelDefault(undefined)).toBe("collapsed");
  });
});
