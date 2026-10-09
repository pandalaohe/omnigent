import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { playNativeSystemSoundMock } = vi.hoisted(() => ({
  playNativeSystemSoundMock: vi.fn(),
}));

vi.mock("./nativeBridge", () => ({
  playNativeSystemSound: playNativeSystemSoundMock,
}));

import {
  BUILTIN_SOUNDS,
  SOUND_ALERT_DEFAULTS,
  SOUND_ALERT_DEVICE_DEFAULTS,
  type SoundAlertDevicePreferences,
} from "./soundAlertPreferences";
import {
  initAudio,
  isAudioLocked,
  playBuiltinSound,
  playLevel,
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

class FakeBufferSource {
  buffer: unknown = null;
  connect = vi.fn();
  start = vi.fn();
}

class FakeAudioContext {
  static instances: FakeAudioContext[] = [];
  static ignoreResume = false;
  state: "suspended" | "running" = "suspended";
  currentTime = 0;
  destination = { name: "destination" };
  gainNodes: FakeGainNode[] = [];
  oscillators: FakeOscillatorNode[] = [];
  bufferSources: FakeBufferSource[] = [];

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

  createBufferSource(): FakeBufferSource {
    const node = new FakeBufferSource();
    this.bufferSources.push(node);
    return node;
  }

  decodeAudioData = vi.fn(async (_data: ArrayBuffer) => ({ duration: 0.5 }));

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

function deviceWith(
  overrides: Partial<SoundAlertDevicePreferences> = {},
): SoundAlertDevicePreferences {
  return { ...SOUND_ALERT_DEVICE_DEFAULTS, ...overrides };
}

describe("sound player", () => {
  beforeEach(() => {
    FakeAudioContext.instances = [];
    FakeAudioContext.ignoreResume = false;
    installAudioContext();
    resetSoundPlayerForTests();
    playNativeSystemSoundMock.mockReset();
    playNativeSystemSoundMock.mockResolvedValue({ played: false });
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

  it("plays a device system-sound override through the shell, not the built-in", async () => {
    initAudio({ native: true });
    playNativeSystemSoundMock.mockResolvedValue({ played: true });

    await playLevel("done", SOUND_ALERT_DEFAULTS, deviceWith({ systemSounds: { done: "Glass" } }));

    expect(playNativeSystemSoundMock).toHaveBeenCalledWith("Glass", 0.7);
    expect(FakeAudioContext.instances[0].oscillators).toHaveLength(0);
  });

  it("falls back to the built-in sound when the shell does not play the system sound", async () => {
    initAudio({ native: true });
    playNativeSystemSoundMock.mockResolvedValue({ played: false });

    await playLevel("done", SOUND_ALERT_DEFAULTS, deviceWith({ systemSounds: { done: "Glass" } }));

    expect(FakeAudioContext.instances[0].oscillators.length).toBeGreaterThan(0);
  });

  it("decodes shell WAV bytes and plays them through a gain node at the device volume", async () => {
    initAudio({ native: true });
    playNativeSystemSoundMock.mockResolvedValue({ bytes: new Uint8Array([1, 2, 3, 4]) });

    await playLevel(
      "done",
      SOUND_ALERT_DEFAULTS,
      deviceWith({ systemSounds: { done: "ding" }, volume: 0.3 }),
    );

    const context = FakeAudioContext.instances[0];
    expect(context.decodeAudioData).toHaveBeenCalledTimes(1);
    const [decoded] = context.decodeAudioData.mock.calls[0];
    expect((decoded as ArrayBuffer).byteLength).toBe(4);
    expect(context.bufferSources).toHaveLength(1);
    expect(context.bufferSources[0].start).toHaveBeenCalled();
    expect(context.oscillators).toHaveLength(0);
    const master = context.gainNodes.find((node) => node.connectedTo.includes(context.destination));
    expect(master?.gain.value).toBe(0.3);
  });
});
