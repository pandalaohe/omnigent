import { authenticatedFetch } from "./identity";
import { apiErrorFromResponse } from "./sessionsApi";
import {
  refreshUserPreferencesFromServer,
  type UserPreferencesEnvelope,
} from "./userPreferencesSync";

export const VOICE_INPUT_CHANGED_EVENT = "omnigent:voice-input-changed";

export interface VoiceInputSettings {
  words: string;
  mode: "automatic" | "manual" | "off";
  model: string;
  timeout: number;
  reasoning_effort: string | null;
  reasoning: Record<string, string> | null;
  thinking: Record<string, string | number | boolean> | null;
}

export const defaultVoiceInputSettings: VoiceInputSettings = {
  words: "",
  mode: "automatic",
  model: "",
  timeout: 6,
  reasoning_effort: null,
  reasoning: null,
  thinking: null,
};

async function voiceRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await authenticatedFetch(path, init);
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.json() as Promise<T>;
}

export async function getVoiceInputSettings(): Promise<VoiceInputSettings> {
  const body = await voiceRequest<{ preferences?: UserPreferencesEnvelope | null }>("/v1/me", {
    cache: "no-store",
  });
  return { ...defaultVoiceInputSettings, ...(body.preferences?.settings.voice_input ?? {}) };
}

export async function saveVoiceInputSettings(
  settings: VoiceInputSettings,
): Promise<VoiceInputSettings> {
  const envelope = await voiceRequest<UserPreferencesEnvelope>("/v1/me/preferences/voice_input", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ value: settings }),
  });
  await refreshUserPreferencesFromServer();
  return { ...defaultVoiceInputSettings, ...(envelope.settings.voice_input ?? {}) };
}

export function cleanupDictation(text: string, signal: AbortSignal): Promise<{ text: string }> {
  return voiceRequest("/v1/dictation/cleanup", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
    signal,
  });
}
