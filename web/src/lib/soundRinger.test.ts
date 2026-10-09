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
  const played: { level: SoundLevel; at: number }[] = [];
  let queue: { fn: () => void; at: number }[] = [];
  const ringer = createSoundRinger({
    play: (level) => played.push({ level, at: now }),
    getContext: () => ({
      account: account(),
      device: device(),
      windowFocused: false,
      activeConversationId: undefined,
      now: new Date(now),
      ...overrides,
    }),
    nowMs: () => now,
    schedule: (fn, ms) => {
      queue.push({ fn, at: now + ms });
    },
  });
  function advance(ms: number): void {
    const target = now + ms;
    for (;;) {
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
  return { ringer, played, advance, setTime };
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

  it("spaces the later class 400 ms after the earlier one", () => {
    const h = createHarness();

    h.ringer.ring(alert("needs_response"));
    h.advance(100);
    h.ringer.ring(alert("error"));
    h.advance(1_000);

    expect(h.played.map((entry) => entry.level)).toEqual(["needs_response", "error"]);
    expect(h.played[1].at - h.played[0].at).toBe(400);
  });

  it("collapses a completion burst to one cue and lets an error through", () => {
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
    h.advance(1_000);

    expect(h.played.map((entry) => entry.level)).toEqual(["done", "error", "needs_response"]);
    const error = h.played.find((entry) => entry.level === "error");
    const needs = h.played.find((entry) => entry.level === "needs_response");
    expect(error).toBeDefined();
    expect(needs).toBeDefined();
    expect(needs?.at).toBe((error?.at ?? 0) + 400);
  });

  it("lets an error join a completion burst but not a completion after an error", () => {
    const withError = createHarness();
    withError.ringer.ring(alert("done", "conv_1"));
    withError.ringer.ring(alert("error", "conv_2"));
    withError.advance(1_000);
    expect(withError.played.map((entry) => entry.level)).toEqual(["done", "error"]);

    const withDone = createHarness();
    withDone.ringer.ring(alert("error", "conv_1"));
    withDone.ringer.ring(alert("done", "conv_2"));
    withDone.advance(1_000);
    expect(withDone.played.map((entry) => entry.level)).toEqual(["error"]);
  });
});
