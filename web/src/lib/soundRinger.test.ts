import { describe, expect, it } from "vitest";

import type {
  SoundAlertDevicePreferences,
  SoundAlertPreferences,
  SoundLevel,
} from "./soundAlertPreferences";
import { createSoundRinger, type RingerContext } from "./soundRinger";
import type { SoundAlert } from "./soundAlertTransitions";

const NOON = new Date(2026, 0, 15, 12, 0, 0).getTime();

function account(overrides: Partial<SoundAlertPreferences> = {}): SoundAlertPreferences {
  return {
    levels: {
      done: { enabled: true, sound: "chime" },
      error: { enabled: true, sound: "alert" },
      needs_response: { enabled: true, sound: "ping" },
    },
    quietHours: { enabled: false, start: "23:00", end: "08:00" },
    primaryDeviceId: null,
    mutedSessionIds: [],
    ...overrides,
  };
}

function device(overrides: Partial<SoundAlertDevicePreferences> = {}): SoundAlertDevicePreferences {
  return { enabled: true, volume: 0.7, systemSounds: {}, legacySoundMigrated: false, ...overrides };
}

function alert(level: SoundLevel, sessionId = "conv_a"): SoundAlert {
  return { sessionId, level, alertId: `${sessionId}:${level}:1` };
}

function createHarness(overrides: Partial<RingerContext> = {}) {
  let now = NOON;
  let contextOverrides: Partial<RingerContext> = { ...overrides };
  const played: { level: SoundLevel; at: number }[] = [];
  let queue: { fn: () => void; at: number; cancelled: boolean }[] = [];
  const ringer = createSoundRinger({
    play: (level) => played.push({ level, at: now }),
    getContext: () => ({
      account: account(),
      device: device(),
      windowFocused: false,
      activeConversationId: undefined,
      userStoppedRecently: () => false,
      now: new Date(now),
      ...contextOverrides,
    }),
    nowMs: () => now,
    schedule: (fn, ms) => {
      const entry = { fn, at: now + ms, cancelled: false };
      queue.push(entry);
      return () => {
        entry.cancelled = true;
      };
    },
  });
  function advance(ms: number): void {
    const target = now + ms;
    for (;;) {
      queue = queue.filter((entry) => !entry.cancelled);
      queue.sort((a, b) => a.at - b.at);
      const next = queue[0];
      if (next === undefined || next.at > target) break;
      queue = queue.slice(1);
      now = next.at;
      next.fn();
    }
    now = target;
  }
  function setTime(date: Date): void {
    now = date.getTime();
  }
  function setContext(next: Partial<RingerContext>): void {
    contextOverrides = { ...contextOverrides, ...next };
  }
  return { ringer, played, advance, setTime, setContext };
}

describe("sound ringer", () => {
  it("plays an accepted alert", () => {
    const h = createHarness();

    h.ringer.ring(alert("needs_response"));

    expect(h.played).toEqual([{ level: "needs_response", at: NOON }]);
  });

  it("drops when the device master switch is off", () => {
    const h = createHarness({ device: device({ enabled: false }) });

    h.ringer.ring(alert("needs_response"));

    expect(h.played).toEqual([]);
  });

  it("drops during quiet hours and plays outside them", () => {
    const h = createHarness({
      account: account({ quietHours: { enabled: true, start: "23:00", end: "08:00" } }),
    });

    h.setTime(new Date(2026, 0, 15, 2, 30));
    h.ringer.ring(alert("needs_response"));
    expect(h.played).toEqual([]);

    h.setTime(new Date(2026, 0, 15, 12, 0));
    h.ringer.ring(alert("needs_response", "conv_b"));
    expect(h.played.map((entry) => entry.level)).toEqual(["needs_response"]);
  });

  it("drops a muted session", () => {
    const h = createHarness({ account: account({ mutedSessionIds: ["conv_a"] }) });

    h.ringer.ring(alert("needs_response"));

    expect(h.played).toEqual([]);
  });

  it("drops a disabled level", () => {
    const levels = account().levels;
    const h = createHarness({
      account: account({ levels: { ...levels, done: { enabled: false, sound: "chime" } } }),
    });

    h.ringer.ring(alert("done"));

    expect(h.played).toEqual([]);
  });

  it("drops an alert id it has already rung", () => {
    const h = createHarness();

    h.ringer.ring(alert("needs_response"));
    h.advance(2_001);
    h.ringer.ring(alert("needs_response"));

    expect(h.played).toHaveLength(1);
  });

  it("keeps only the last 500 alert ids", () => {
    const h = createHarness();
    for (let i = 0; i <= 500; i += 1) {
      h.advance(2_001);
      h.ringer.ring({ sessionId: `conv_${i}`, level: "needs_response", alertId: `id_${i}` });
    }

    h.advance(2_001);
    h.ringer.ring({ sessionId: "conv_0", level: "needs_response", alertId: "id_0" });
    h.ringer.ring({ sessionId: "conv_500", level: "needs_response", alertId: "id_500" });

    expect(h.played).toHaveLength(502);
  });

  it("still plays needs_response while the user views the session", () => {
    const h = createHarness({ windowFocused: true, activeConversationId: "conv_a" });

    h.ringer.ring(alert("needs_response"));

    expect(h.played.map((entry) => entry.level)).toEqual(["needs_response"]);
  });

  it("drops done and error while the user views the session", () => {
    const h = createHarness({ windowFocused: true, activeConversationId: "conv_a" });

    h.ringer.ring(alert("done"));
    h.advance(2_001);
    h.ringer.ring(alert("error"));

    expect(h.played).toEqual([]);
  });

  it("drops done and error for a session the user just stopped", () => {
    const h = createHarness({ userStoppedRecently: (id) => id === "conv_a" });

    h.ringer.ring(alert("done"));
    h.advance(2_001);
    h.ringer.ring(alert("error"));
    h.advance(2_001);

    expect(h.played).toEqual([]);
  });

  it("still plays needs_response for a session the user just stopped", () => {
    const h = createHarness({ userStoppedRecently: (id) => id === "conv_a" });

    h.ringer.ring(alert("needs_response"));

    expect(h.played.map((entry) => entry.level)).toEqual(["needs_response"]);
  });

  it("plays a done for another session while one was just stopped", () => {
    const h = createHarness({ userStoppedRecently: (id) => id === "conv_a" });

    h.ringer.ring(alert("done", "conv_b"));
    h.advance(2_000);

    expect(h.played).toEqual([{ level: "done", at: NOON + 2_000 }]);
  });

  it("spaces the other cue 400 ms after a needs_response cue that precedes its window close", () => {
    const h = createHarness();

    h.ringer.ring(alert("done", "conv_1"));
    h.advance(1_700);
    h.ringer.ring(alert("needs_response"));
    h.advance(1_000);

    expect(h.played.map((entry) => entry.level)).toEqual(["needs_response", "done"]);
    expect(h.played[1].at - h.played[0].at).toBe(400);
  });

  it("spaces a needs_response cue 400 ms after an other cue that just played", () => {
    const h = createHarness();

    h.ringer.ring(alert("done", "conv_1"));
    h.advance(2_100);
    h.ringer.ring(alert("needs_response"));
    h.advance(1_000);

    expect(h.played.map((entry) => entry.level)).toEqual(["done", "needs_response"]);
    expect(h.played[1].at - h.played[0].at).toBe(400);
  });

  it("collapses a burst to one needs_response cue and one error cue", () => {
    const h = createHarness();

    h.ringer.ring(alert("done", "conv_1"));
    h.advance(100);
    h.ringer.ring(alert("done", "conv_2"));
    h.advance(100);
    h.ringer.ring(alert("done", "conv_3"));
    h.advance(100);
    h.ringer.ring(alert("error", "conv_4"));
    h.advance(100);
    h.ringer.ring(alert("needs_response", "conv_5"));
    h.advance(2_000);

    expect(h.played).toEqual([
      { level: "needs_response", at: NOON + 400 },
      { level: "error", at: NOON + 2_000 },
    ]);
  });

  it("plays a lone done once when its collection window closes", () => {
    const h = createHarness();

    h.ringer.ring(alert("done"));
    h.advance(2_000);

    expect(h.played).toEqual([{ level: "done", at: NOON + 2_000 }]);
  });

  it("collapses three dones in a window to one done", () => {
    const h = createHarness();

    h.ringer.ring(alert("done", "conv_1"));
    h.advance(100);
    h.ringer.ring(alert("done", "conv_2"));
    h.advance(100);
    h.ringer.ring(alert("done", "conv_3"));
    h.advance(2_000);

    expect(h.played).toEqual([{ level: "done", at: NOON + 2_000 }]);
  });

  it("plays nothing when the collected alert stops passing filters at window close", () => {
    const h = createHarness();

    h.ringer.ring(alert("error", "conv_1"));
    h.setContext({ device: device({ enabled: false }) });
    h.advance(3_000);

    expect(h.played).toEqual([]);
  });

  it("opens a new collection for an error arriving after the previous window closed", () => {
    const h = createHarness();

    h.ringer.ring(alert("done", "conv_1"));
    h.advance(2_000);
    h.advance(500);
    h.ringer.ring(alert("error", "conv_2"));
    h.advance(2_000);

    expect(h.played).toEqual([
      { level: "done", at: NOON + 2_000 },
      { level: "error", at: NOON + 4_500 },
    ]);
  });

  it("a window that played nothing does not suppress the next alert", () => {
    const h = createHarness();

    h.ringer.ring(alert("done", "conv_1"));
    h.setContext({ device: device({ enabled: false }) });
    h.advance(2_000);
    expect(h.played).toEqual([]);

    h.setContext({ device: device({ enabled: true }) });
    h.advance(500);
    h.ringer.ring(alert("done", "conv_2"));
    h.advance(2_000);

    expect(h.played).toEqual([{ level: "done", at: NOON + 4_500 }]);
  });

  it("drops a scheduled needs_response when the device is switched off before it fires", () => {
    const h = createHarness();

    h.ringer.ring(alert("done", "conv_1"));
    // The done cue plays when its collection window closes, so a needs_response
    // arriving just after is scheduled 400 ms out rather than played at once.
    h.advance(2_100);
    h.ringer.ring(alert("needs_response"));
    h.setContext({ device: device({ enabled: false }) });
    h.advance(1_000);

    expect(h.played.map((entry) => entry.level)).toEqual(["done"]);
  });

  it("cancels a scheduled needs_response on dispose", () => {
    const h = createHarness();

    h.ringer.ring(alert("done", "conv_1"));
    h.advance(2_100);
    h.ringer.ring(alert("needs_response"));
    h.ringer.dispose();
    h.advance(1_000);

    expect(h.played.map((entry) => entry.level)).toEqual(["done"]);
  });

  it("cancels an open collection window on dispose", () => {
    const h = createHarness();

    h.ringer.ring(alert("done", "conv_1"));
    h.ringer.dispose();
    h.advance(3_000);

    expect(h.played).toEqual([]);
  });
});
