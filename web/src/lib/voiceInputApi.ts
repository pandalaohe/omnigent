import { authenticatedFetch } from "./identity";
import { apiErrorFromResponse } from "./sessionsApi";

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

export function getVoiceInputSettings(): Promise<VoiceInputSettings> {
  return voiceRequest("/v1/settings/voice-input");
}

export function saveVoiceInputSettings(settings: VoiceInputSettings): Promise<VoiceInputSettings> {
  return voiceRequest("/v1/settings/voice-input", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(settings),
  });
}

export function cleanupDictation(text: string, signal: AbortSignal): Promise<{ text: string }> {
  return voiceRequest("/v1/dictation/cleanup", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
    signal,
  });
}
