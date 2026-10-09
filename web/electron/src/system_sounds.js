// Built-in OS alert sounds (macOS .aiff, Windows .wav), used by the desktop
// shell's notification menu and exposed to the web app's sound-alert settings.
//
// Pure and dependency-injected: the platform, directory reader and environment
// are passed in so the logic is unit-testable without touching the real
// filesystem or the host OS.

"use strict";

const path = require("node:path");

/** macOS built-in sounds directory. */
const SYSTEM_SOUNDS_DIR = "/System/Library/Sounds";

// Fallback list if the macOS sounds dir can't be read (matches stock macOS).
const FALLBACK_SYSTEM_SOUNDS = [
  "Basso",
  "Blow",
  "Bottle",
  "Frog",
  "Funk",
  "Glass",
  "Hero",
  "Morse",
  "Ping",
  "Pop",
  "Purr",
  "Sosumi",
  "Submarine",
  "Tink",
];

/**
 * The Windows built-in sounds directory.
 *
 * @param {{ WINDIR?: string, SystemRoot?: string }} [env]
 * @returns {string}
 */
function windowsSoundsDir(env) {
  return path.win32.join(env?.WINDIR || env?.SystemRoot || "C:\\Windows", "Media");
}

/**
 * The built-in alert sound names (no extension), sorted, for `platform`.
 * macOS falls back to the stock set when the sounds dir can't be read;
 * Windows and every other platform yield [] on a read failure.
 *
 * @param {{ platform: string, readdirSync: (dir: string) => string[], env?: Record<string, string | undefined> }} deps
 * @returns {string[]}
 */
function listSystemSounds({ platform, readdirSync, env }) {
  if (platform === "darwin") {
    try {
      const names = readdirSync(SYSTEM_SOUNDS_DIR)
        .filter((file) => file.endsWith(".aiff"))
        .map((file) => file.replace(/\.aiff$/, ""));
      return names.length > 0 ? [...names].sort() : [...FALLBACK_SYSTEM_SOUNDS];
    } catch {
      return [...FALLBACK_SYSTEM_SOUNDS];
    }
  }
  if (platform === "win32") {
    try {
      return readdirSync(windowsSoundsDir(env))
        .filter((file) => /\.wav$/i.test(file))
        .map((file) => file.replace(/\.wav$/i, ""))
        .sort();
    } catch {
      return [];
    }
  }
  return [];
}

/**
 * Resolve `name` to its absolute sound file, or null. A name is only ever a
 * path when it appears verbatim in {@link listSystemSounds} — no traversal, no
 * alternate extensions.
 *
 * @param {unknown} name
 * @param {{ platform: string, readdirSync: (dir: string) => string[], env?: Record<string, string | undefined> }} deps
 * @returns {string | null}
 */
function resolveSystemSound(name, deps) {
  if (typeof name !== "string" || name === "") return null;
  if (!listSystemSounds(deps).includes(name)) return null;
  if (deps.platform === "darwin") return path.posix.join(SYSTEM_SOUNDS_DIR, `${name}.aiff`);
  if (deps.platform === "win32") return path.win32.join(windowsSoundsDir(deps.env), `${name}.wav`);
  return null;
}

/**
 * The old Notification-menu sound setting, normalized for migration into the
 * web layer's device preferences. Unset/invalid fields are null.
 *
 * @param {unknown} settings The shell settings object.
 * @returns {{ enabled: boolean | null, name: string | null }}
 */
function legacyNotificationSound(settings) {
  const raw = settings && typeof settings === "object" ? settings : {};
  return {
    enabled:
      typeof raw.notification_sound_enabled === "boolean" ? raw.notification_sound_enabled : null,
    name:
      typeof raw.notification_sound_name === "string" && raw.notification_sound_name.length > 0
        ? raw.notification_sound_name
        : null,
  };
}

module.exports = {
  FALLBACK_SYSTEM_SOUNDS,
  SYSTEM_SOUNDS_DIR,
  legacyNotificationSound,
  listSystemSounds,
  resolveSystemSound,
};
