import { afterEach, describe, expect, it } from "vitest";
import {
  readPanelSharePreference,
  readPanelSizePreference,
  readPanelSizePreferences,
  readInlinePanelChatReserveWidth,
  writePanelSharePreference,
  writePanelSizePreference,
} from "./panelSizePreferences";

const STORAGE_KEY = "omnigent:panel-size-preferences";

afterEach(() => {
  localStorage.clear();
});

describe("panelSizePreferences", () => {
  it("validates the browser chat reserve without changing remembered widths", () => {
    expect(readInlinePanelChatReserveWidth()).toBe(600);
    writePanelSizePreference("inlinePanelWideWidthPx", 840);
    writePanelSizePreference("inlinePanelChatReserveWidthPx", 800);
    expect(readInlinePanelChatReserveWidth()).toBe(800);
    for (const invalid of [0, 479, 1201, 600.5, NaN]) {
      writePanelSizePreference("inlinePanelChatReserveWidthPx", invalid);
      expect(readInlinePanelChatReserveWidth()).toBe(800);
    }
    expect(readPanelSizePreference("inlinePanelWideWidthPx")).toBe(840);
    writePanelSizePreference("inlinePanelChatReserveWidthPx", null);
    expect(readInlinePanelChatReserveWidth()).toBe(600);
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ inlinePanelChatReserveWidthPx: 2000 }));
    expect(readInlinePanelChatReserveWidth()).toBe(600);
  });
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

  it("keeps the browser target independent of file and normal widths", () => {
    writePanelSizePreference("inlinePanelWidthPx", 420);
    writePanelSizePreference("inlinePanelWideWidthPx", 840);
    writePanelSizePreference("inlinePanelBrowserWidthPx", 1024);
    expect(readPanelSizePreferences()).toEqual({
      inlinePanelWidthPx: 420,
      inlinePanelWideWidthPx: 840,
      inlinePanelBrowserWidthPx: 1024,
    });
    writePanelSizePreference("inlinePanelBrowserWidthPx", null);
    expect(readPanelSizePreference("inlinePanelBrowserWidthPx")).toBeNull();
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ inlinePanelBrowserWidthPx: "wide", inlinePanelWidthPx: 420 }),
    );
    expect(readPanelSizePreferences()).toEqual({ inlinePanelWidthPx: 420 });
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
        filesTreeWidthPx: 300,
        unknownWidthPx: 9,
      }),
    );

    // The valid widths are retained while invalid and unknown sibling fields are
    // dropped, proving one bad field does not poison the whole record.
    expect(readPanelSizePreferences()).toEqual({ pushPanelWidthPx: 700, filesTreeWidthPx: 300 });
    expect(readPanelSizePreference("inlinePanelWidthPx")).toBeNull();
    expect(readPanelSizePreference("filesTreeWidthPx")).toBe(300);
  });

  it("keeps a tree's share of its page apart from pixel widths, each kept by the other's writes", () => {
    writePanelSharePreference("filesTreeShare", 0.3);
    writePanelSizePreference("pushPanelWidthPx", 840);
    writePanelSharePreference("pullRequestReviewTreeShare", 0.25);

    expect(readPanelSizePreferences()).toEqual({
      filesTreeShare: 0.3,
      pushPanelWidthPx: 840,
      pullRequestReviewTreeShare: 0.25,
    });
    expect(readPanelSharePreference("changesReviewTreeShare")).toBeNull();
    writePanelSharePreference("filesTreeShare", null);
    expect(readPanelSharePreference("filesTreeShare")).toBeNull();
    expect(readPanelSharePreference("pullRequestReviewTreeShare")).toBe(0.25);
  });

  it("drops a share outside 0 to 1, on read and on write", () => {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        filesTreeShare: 1,
        changesReviewTreeShare: 0,
        pullRequestReviewTreeShare: 0.4,
      }),
    );
    expect(readPanelSizePreferences()).toEqual({ pullRequestReviewTreeShare: 0.4 });
    writePanelSharePreference("filesTreeShare", 1.5);
    writePanelSharePreference("changesReviewTreeShare", Number.NaN);
    expect(readPanelSharePreference("filesTreeShare")).toBeNull();
    expect(readPanelSharePreference("changesReviewTreeShare")).toBeNull();
  });
});
