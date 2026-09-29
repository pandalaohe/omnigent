import { afterEach, describe, expect, it, vi } from "vitest";

const { queuePatchMock } = vi.hoisted(() => ({ queuePatchMock: vi.fn() }));
vi.mock("./userPreferencesSync", () => ({ queueUserPreferencePatch: queuePatchMock }));

import {
  readSessionCollabPreferences,
  SESSION_COLLAB_DEFAULTS,
  SESSION_COLLAB_STORAGE_KEY,
  writeSessionCollabPreferences,
} from "./sessionCollabPreferences";

afterEach(() => {
  localStorage.clear();
  queuePatchMock.mockReset();
});

describe("session collab preferences", () => {
  it("defaults to the server defaults with nothing stored", () => {
    expect(readSessionCollabPreferences()).toEqual(SESSION_COLLAB_DEFAULTS);
    expect(localStorage.getItem(SESSION_COLLAB_STORAGE_KEY)).toBeNull();
  });

  it("treats a non-object payload as all-defaults", () => {
    localStorage.setItem(SESSION_COLLAB_STORAGE_KEY, '"nope"');
    expect(readSessionCollabPreferences()).toEqual(SESSION_COLLAB_DEFAULTS);
  });

  it("normalizes invalid fields back to their defaults", () => {
    localStorage.setItem(
      SESSION_COLLAB_STORAGE_KEY,
      JSON.stringify({
        enabled: "yes",
        openRateCount: 7,
        openRateWindowS: 90.5,
        relayDepthMax: 101,
        pairRateCount: 1001,
        pairRateWindowS: 101,
        senderRateCount: "60",
        senderRateWindowS: 0,
        duplicateWindowS: 0,
        undeliveredTtlS: 7200,
        defaultInbound: "bogus",
        flowTimerEnabled: 1,
      }),
    );

    expect(readSessionCollabPreferences()).toEqual({
      ...SESSION_COLLAB_DEFAULTS,
      openRateCount: 7,
      pairRateWindowS: 101,
      duplicateWindowS: 0,
      undeliveredTtlS: 7200,
    });
  });

  it("keeps a zero duplicate window and non-accept inbound policy", () => {
    writeSessionCollabPreferences({
      ...SESSION_COLLAB_DEFAULTS,
      duplicateWindowS: 0,
      defaultInbound: "refuse",
    });

    expect(readSessionCollabPreferences()).toEqual({
      ...SESSION_COLLAB_DEFAULTS,
      duplicateWindowS: 0,
      defaultInbound: "refuse",
    });
  });

  it("writes a changed relay depth and patches the namespace", () => {
    writeSessionCollabPreferences({ ...SESSION_COLLAB_DEFAULTS, relayDepthMax: 3 });

    const expected = { ...SESSION_COLLAB_DEFAULTS, relayDepthMax: 3 };
    expect(JSON.parse(localStorage.getItem(SESSION_COLLAB_STORAGE_KEY) ?? "null")).toEqual(
      expected,
    );
    expect(queuePatchMock).toHaveBeenLastCalledWith("session_collab", expected);
  });

  it("clears storage and patches null when every value is back at its default", () => {
    writeSessionCollabPreferences({ ...SESSION_COLLAB_DEFAULTS, relayDepthMax: 3 });
    expect(localStorage.getItem(SESSION_COLLAB_STORAGE_KEY)).not.toBeNull();

    writeSessionCollabPreferences(SESSION_COLLAB_DEFAULTS);

    expect(localStorage.getItem(SESSION_COLLAB_STORAGE_KEY)).toBeNull();
    expect(queuePatchMock).toHaveBeenLastCalledWith("session_collab", null);
  });

  it("rejects an out-of-range patch by normalizing it to the default", () => {
    writeSessionCollabPreferences({ ...SESSION_COLLAB_DEFAULTS, relayDepthMax: 0 });

    expect(localStorage.getItem(SESSION_COLLAB_STORAGE_KEY)).toBeNull();
    expect(queuePatchMock).toHaveBeenLastCalledWith("session_collab", null);
  });
});
