// Synthesized alert sounds (no audio files) through the Web Audio API.
//
// Browsers lock audio until a user gesture, so `initAudio` either creates the
// context now (native shells allow autoplay) or waits for the first
// pointerdown/keydown (browser). `isAudioLocked`/`subscribeAudioLock` let the
// settings UI show that an interaction is still needed.

import {
  clampSoundVolume,
  type BuiltinSoundId,
  type SoundAlertDevicePreferences,
  type SoundAlertPreferences,
  type SoundLevel,
} from "./soundAlertPreferences";

type AudioContextConstructor = new () => AudioContext;

interface ToneSpec {
  type: OscillatorType;
  frequency: number;
  startOffset: number;
  duration: number;
  attack: number;
  peak: number;
}

/** Every tone stays well under the 600 ms budget for a cue. */
const SOUND_TONES: Record<BuiltinSoundId, readonly ToneSpec[]> = {
  chime: [
    { type: "sine", frequency: 660, startOffset: 0, duration: 0.2, attack: 0.01, peak: 0.5 },
    { type: "sine", frequency: 880, startOffset: 0.16, duration: 0.24, attack: 0.01, peak: 0.5 },
  ],
  ping: [
    { type: "sine", frequency: 1320, startOffset: 0, duration: 0.2, attack: 0.01, peak: 0.45 },
  ],
  pop: [
    {
      type: "triangle",
      frequency: 440,
      startOffset: 0,
      duration: 0.08,
      attack: 0.005,
      peak: 0.6,
    },
  ],
  alert: [
    { type: "square", frequency: 523, startOffset: 0, duration: 0.16, attack: 0.01, peak: 0.18 },
    { type: "square", frequency: 392, startOffset: 0.18, duration: 0.2, attack: 0.01, peak: 0.18 },
  ],
};

let context: AudioContext | null = null;
let unlockListenersAttached = false;
let audioLocked = true;
const lockListeners = new Set<() => void>();

function audioContextConstructor(): AudioContextConstructor | null {
  if (typeof window === "undefined") return null;
  const runtime = window as unknown as {
    AudioContext?: AudioContextConstructor;
    webkitAudioContext?: AudioContextConstructor;
  };
  return runtime.AudioContext ?? runtime.webkitAudioContext ?? null;
}

function createContextIfPossible(): boolean {
  if (context !== null) return true;
  const Constructor = audioContextConstructor();
  if (!Constructor) return false;
  try {
    context = new Constructor();
  } catch {
    context = null;
    return false;
  }
  return true;
}

function attachUnlockListeners(): void {
  if (unlockListenersAttached) return;
  unlockListenersAttached = true;
  window.addEventListener("pointerdown", unlockAudio);
  window.addEventListener("keydown", unlockAudio);
}

function detachUnlockListeners(): void {
  if (!unlockListenersAttached) return;
  unlockListenersAttached = false;
  window.removeEventListener("pointerdown", unlockAudio);
  window.removeEventListener("keydown", unlockAudio);
}

function notifyAudioLock(): void {
  const locked = isAudioLocked();
  if (locked === audioLocked) return;
  audioLocked = locked;
  for (const listener of [...lockListeners]) listener();
}

function resumeContext(): void {
  const current = context;
  if (!current) return;
  if (current.state === "running") {
    detachUnlockListeners();
    notifyAudioLock();
    return;
  }
  try {
    const resumed = current.resume();
    notifyAudioLock();
    void Promise.resolve(resumed)
      .then(() => {
        if (context !== current) return;
        if (current.state === "running") detachUnlockListeners();
        notifyAudioLock();
      })
      .catch(() => {});
  } catch {
    // A resume outside a user gesture can throw; stay locked until one arrives.
  }
}

function unlockAudio(): void {
  if (!createContextIfPossible()) {
    // Without a constructor the runtime can never play, so stop listening.
    detachUnlockListeners();
    return;
  }
  resumeContext();
}

/**
 * Prepare audio playback. `native` shells may autoplay, so their context is
 * created immediately; browsers wait for the first user gesture. Idempotent.
 */
export function initAudio({ native }: { native: boolean }): void {
  if (context !== null) return;
  if (native) {
    if (createContextIfPossible()) {
      resumeContext();
      // Native shells usually allow autoplay; when resume hasn't landed yet
      // (or fails), a later gesture can still unlock the context.
      if (isAudioLocked()) attachUnlockListeners();
    }
    return;
  }
  attachUnlockListeners();
}

/** True while no context exists or it is not running. */
export function isAudioLocked(): boolean {
  return context === null || context.state !== "running";
}

/** Notified whenever the locked state flips, so a hint can appear or clear. */
export function subscribeAudioLock(listener: () => void): () => void {
  lockListeners.add(listener);
  return () => {
    lockListeners.delete(listener);
  };
}

/** Resume (or create) the context; resolves whether it is running. */
async function ensureRunning(): Promise<boolean> {
  if (!createContextIfPossible()) return false;
  const current = context;
  if (!current) return false;
  if (current.state !== "running") {
    try {
      await current.resume();
    } catch {
      // A resume outside a user gesture can throw; stay locked until one arrives.
    }
  }
  if (context !== current) return false;
  if (current.state === "running") detachUnlockListeners();
  notifyAudioLock();
  return current.state === "running";
}

/**
 * Play a builtin sound. `resume: true` is for call sites that run inside a
 * user gesture (the settings preview), where unlocking the context first is
 * both allowed and required for the first audible note.
 */
export async function playBuiltinSound(
  id: BuiltinSoundId,
  volume: number,
  options: { resume?: boolean } = {},
): Promise<void> {
  if (options.resume && !(await ensureRunning())) return;
  const current = context;
  if (!current || current.state !== "running") return;
  const master = current.createGain();
  master.gain.value = clampSoundVolume(volume);
  master.connect(current.destination);
  const now = current.currentTime;
  for (const tone of SOUND_TONES[id]) {
    const oscillator = current.createOscillator();
    oscillator.type = tone.type;
    oscillator.frequency.value = tone.frequency;
    const envelope = current.createGain();
    const start = now + tone.startOffset;
    const end = start + tone.duration;
    envelope.gain.setValueAtTime(0, start);
    envelope.gain.linearRampToValueAtTime(tone.peak, start + tone.attack);
    envelope.gain.exponentialRampToValueAtTime(0.0001, end);
    oscillator.connect(envelope);
    envelope.connect(master);
    oscillator.start(start);
    oscillator.stop(end + 0.02);
  }
}

export function playLevel(
  level: SoundLevel,
  preferences: SoundAlertPreferences,
  device: SoundAlertDevicePreferences,
): Promise<void> {
  return playBuiltinSound(preferences.levels[level].sound, device.volume);
}

export function resetSoundPlayerForTests(): void {
  detachUnlockListeners();
  context = null;
  audioLocked = true;
  lockListeners.clear();
}
