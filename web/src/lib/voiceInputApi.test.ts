import { afterEach, expect, it, vi } from "vitest";
import {
  getVoiceInputSettings,
  saveVoiceInputSettings,
  defaultVoiceInputSettings,
} from "./voiceInputApi";
import { authenticatedFetch } from "./identity";

vi.mock("./identity", () => ({ authenticatedFetch: vi.fn() }));
vi.mock("./userPreferencesSync", () => ({
  refreshUserPreferencesFromServer: vi.fn(async () => {}),
}));
afterEach(() => vi.clearAllMocks());
it("reads and saves through the existing user-preference namespace API", async () => {
  const settings = { ...defaultVoiceInputSettings, words: "ExampleApp", reasoning_effort: "low" };
  vi.mocked(authenticatedFetch)
    .mockResolvedValueOnce(
      new Response(
        JSON.stringify({ preferences: { version: 1, settings: { voice_input: settings } } }),
      ),
    )
    .mockResolvedValueOnce(
      new Response(JSON.stringify({ version: 1, settings: { voice_input: settings } })),
    );
  expect(await getVoiceInputSettings()).toEqual(settings);
  expect(await saveVoiceInputSettings(settings)).toEqual(settings);
  expect(authenticatedFetch).toHaveBeenNthCalledWith(1, "/v1/me", { cache: "no-store" });
  expect(authenticatedFetch).toHaveBeenNthCalledWith(
    2,
    "/v1/me/preferences/voice_input",
    expect.objectContaining({ method: "PATCH", body: JSON.stringify({ value: settings }) }),
  );
});

it("reads legacy manual as on and fills empty instructions with the default", async () => {
  vi.mocked(authenticatedFetch).mockResolvedValueOnce(
    new Response(
      JSON.stringify({
        preferences: { settings: { voice_input: { mode: "manual", instructions: "" } } },
      }),
    ),
  );
  const result = await getVoiceInputSettings();
  expect(result.mode).toBe("automatic");
  expect(result.instructions).toBe(defaultVoiceInputSettings.instructions);
});
