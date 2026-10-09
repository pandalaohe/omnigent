import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { BUILTIN_SOUNDS } from "./soundAlertPreferences";
import {
  initAudio,
  isAudioLocked,
  playBuiltinSound,
  resetSoundPlayerForTests,
  subscribeAudioLock,
} from "./soundPlayer";

class FakeGainParam {
  value = 1;
  setValueAtTime = vi.fn();
  linearRampToValueAtTime = vi.fn();
  exponentialRampToValueAtTime = vi.fn();
}

class FakeGainNode {
  gain = new FakeGainParam();
  connectedTo: unknown[] = [];
  connect = vi.fn((destination: unknown) => {
    this.connectedTo.push(destination);
  });
}

class FakeOscillatorNode {
  type = "sine";
  frequency = { value: 0 };
  connect = vi.fn();
  start = vi.fn();
  stop = vi.fn();
}

class FakeAudioContext {
  static instances: FakeAudioContext[] = [];
  static ignoreResume = false;
  state: "suspended" | "running" = "suspended";
  currentTime = 0;
  destination = { name: "destination" };
  gainNodes: FakeGainNode[] = [];
  oscillators: FakeOscillatorNode[] = [];

  constructor() {
    FakeAudioContext.instances.push(this);
  }

  createGain(): FakeGainNode {
    const node = new FakeGainNode();
    this.gainNodes.push(node);
    return node;
  }

  createOscillator(): FakeOscillatorNode {
    const node = new FakeOscillatorNode();
    this.oscillators.push(node);
    return node;
  }

  resume = vi.fn(async () => {
    if (FakeAudioContext.ignoreResume) return;
    this.state = "running";
  });
}

function installAudioContext(): void {
  (window as unknown as { AudioContext?: unknown }).AudioContext = FakeAudioContext;
}

function removeAudioContext(): void {
  delete (window as unknown as { AudioContext?: unknown }).AudioContext;
  delete (window as unknown as { webkitAudioContext?: unknown }).webkitAudioContext;
}

describe("sound player", () => {
  beforeEach(() => {
    FakeAudioContext.instances = [];
    FakeAudioContext.ignoreResume = false;
    installAudioContext();
    resetSoundPlayerForTests();
  });

  afterEach(() => {
    resetSoundPlayerForTests();
    removeAudioContext();
  });

  it("keeps browser audio locked until a user gesture", () => {
    initAudio({ native: false });

    expect(isAudioLocked()).toBe(true);
    expect(FakeAudioContext.instances).toHaveLength(0);

    window.dispatchEvent(new Event("pointerdown"));

    expect(isAudioLocked()).toBe(false);
    expect(FakeAudioContext.instances).toHaveLength(1);
    expect(FakeAudioContext.instances[0].resume).toHaveBeenCalled();
  });

  it("unlocks native audio at init and notifies lock listeners", () => {
    const listener = vi.fn();
    subscribeAudioLock(listener);

    initAudio({ native: true });

    expect(isAudioLocked()).toBe(false);
    expect(listener).toHaveBeenCalledTimes(1);
    initAudio({ native: true });
    expect(FakeAudioContext.instances).toHaveLength(1);
  });

  it("is a silent no-op while locked", async () => {
    await expect(playBuiltinSound("chime", 0.5)).resolves.toBeUndefined();
    expect(FakeAudioContext.instances).toHaveLength(0);
  });

  it("falls back to a gesture unlock when native autoplay is refused", () => {
    FakeAudioContext.ignoreResume = true;
    initAudio({ native: true });

    expect(isAudioLocked()).toBe(true);
    expect(FakeAudioContext.instances[0].resume).toHaveBeenCalledTimes(1);

    window.dispatchEvent(new Event("pointerdown"));

    expect(FakeAudioContext.instances[0].resume).toHaveBeenCalledTimes(2);
  });

  it("unlocks and plays when asked to resume from a user gesture", async () => {
    initAudio({ native: false });
    expect(isAudioLocked()).toBe(true);

    await playBuiltinSound("ping", 0.5, { resume: true });

    const context = FakeAudioContext.instances[0];
    expect(context.state).toBe("running");
    expect(context.oscillators.length).toBeGreaterThan(0);
    expect(isAudioLocked()).toBe(false);
  });

  it("stays silent when the runtime has no AudioContext", async () => {
    removeAudioContext();
    initAudio({ native: false });
    window.dispatchEvent(new Event("pointerdown"));

    expect(isAudioLocked()).toBe(true);
    await expect(playBuiltinSound("pop", 1)).resolves.toBeUndefined();
  });

  it("applies the requested volume to the master gain", async () => {
    initAudio({ native: true });

    await playBuiltinSound("ping", 0.4);

    const context = FakeAudioContext.instances[0];
    const master = context.gainNodes.find((node) => node.connectedTo.includes(context.destination));
    expect(master).toBeDefined();
    expect(master?.gain.value).toBe(0.4);
    expect(context.oscillators.length).toBeGreaterThan(0);
  });

  it("keeps every builtin sound at or under 600 ms", async () => {
    initAudio({ native: true });

    await Promise.all(BUILTIN_SOUNDS.map((sound) => playBuiltinSound(sound.id, 1)));

    const context = FakeAudioContext.instances[0];
    expect(context.oscillators.length).toBeGreaterThan(0);
    for (const oscillator of context.oscillators) {
      const [start] = oscillator.start.mock.calls[0];
      const [stop] = oscillator.stop.mock.calls[0];
      expect(stop - start).toBeLessThanOrEqual(0.6);
    }
  });
});
