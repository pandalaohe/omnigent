import { afterEach, describe, expect, it } from "vitest";
import {
  readPanelSizePreference,
  readPanelSizePreferences,
  writePanelSizePreference,
} from "./panelSizePreferences";

const STORAGE_KEY = "omnigent:panel-size-preferences";

afterEach(() => {
  localStorage.clear();
});

describe("panelSizePreferences", () => {
  it("returns an empty object when nothing is stored", () => {
    // Empty storage must be a clean "no preferences yet" state, not an error.
    expect(readPanelSizePreferences()).toEqual({});
  });

  it("round-trips valid widths and preserves unrelated fields", () => {
    writePanelSizePreference("pushPanelWidthPx", 840);
    writePanelSizePreference("inlinePanelWidthPx", 420);

    // Both values must survive separate writes; a write for one panel must not
    // erase the other panel's preference.
    expect(readPanelSizePreferences()).toEqual({
      pushPanelWidthPx: 840,
      inlinePanelWidthPx: 420,
    });
  });

  it("round-trips the wide inline panel width alongside the others", () => {
    writePanelSizePreference("inlinePanelWidthPx", 420);
    writePanelSizePreference("inlinePanelWideWidthPx", 840);

    // The browser/file width is its own field; writing it must not disturb the
    // normal inline width.
    expect(readPanelSizePreferences()).toEqual({
      inlinePanelWidthPx: 420,
      inlinePanelWideWidthPx: 840,
    });
  });

  it("drops an invalid wide width while valid siblings survive", () => {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ pushPanelWidthPx: 700, inlinePanelWideWidthPx: -5 }),
    );
    expect(readPanelSizePreferences()).toEqual({ pushPanelWidthPx: 700 });
    expect(readPanelSizePreference("inlinePanelWideWidthPx")).toBeNull();

    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ inlinePanelWidthPx: 500, inlinePanelWideWidthPx: "x" }),
    );
    expect(readPanelSizePreferences()).toEqual({ inlinePanelWidthPx: 500 });
  });

  it("ignores malformed JSON", () => {
    // Corrupt localStorage should not break app boot.
    localStorage.setItem(STORAGE_KEY, "}{not json");
    expect(readPanelSizePreferences()).toEqual({});
  });

  it("validates each field independently", () => {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        pushPanelWidthPx: 700,
        inlinePanelWidthPx: -1,
        commentsPanelWidthPx: "wide",
      }),
    );

    // The valid push-panel width is retained while invalid sibling fields are
    // dropped, proving one bad field does not poison the whole record.
    expect(readPanelSizePreferences()).toEqual({ pushPanelWidthPx: 700 });
    expect(readPanelSizePreference("inlinePanelWidthPx")).toBeNull();
  });
});
