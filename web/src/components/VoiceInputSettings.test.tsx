import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import * as api from "@/lib/voiceInputApi";
import * as connectionsApi from "@/lib/modelConnectionsApi";
import { VoiceInputSettings } from "./VoiceInputSettings";

vi.mock("@/lib/voiceInputApi", async (importOriginal) => ({
  ...(await importOriginal<typeof api>()),
  getVoiceInputSettings: vi.fn(),
  saveVoiceInputSettings: vi.fn(),
}));
vi.mock("@/lib/modelConnectionsApi", async (original) => ({
  ...(await original<typeof connectionsApi>()),
  listModelConnections: vi.fn(),
}));
const provider: connectionsApi.ModelConnection = {
  name: "example",
  display_name: "Example provider",
  kind: "key",
  base_url: "https://provider.example/v1",
  wire_api: "chat",
  models: ["example-speech", "example-cleanup"],
  key_set: true,
  key_source: "stored",
  editable: true,
};
async function choose(name: string, option: string) {
  fireEvent.keyDown(screen.getByRole("combobox", { name }), { key: "Enter" });
  fireEvent.click(await screen.findByRole("option", { name: option }));
}
afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(connectionsApi.listModelConnections).mockResolvedValue({
    can_manage: true,
    connections: [provider],
  });
  vi.mocked(api.getVoiceInputSettings).mockResolvedValue(api.defaultVoiceInputSettings);
  vi.mocked(api.saveVoiceInputSettings).mockImplementation(async (settings) => settings);
});

it("loads and saves vocabulary, cleanup on/off and custom instructions", async () => {
  render(<VoiceInputSettings />);
  await act(async () => {});
  fireEvent.change(screen.getByRole("textbox", { name: "Voice word list" }), {
    target: { value: "ExampleApp may be heard as example app" },
  });
  await choose("Voice cleanup mode", "Off");
  await choose("Voice cleanup model", "Custom model ID…");
  fireEvent.change(screen.getByRole("textbox", { name: "Cleanup model custom model ID" }), {
    target: { value: "openai/test-cleanup" },
  });
  expect(screen.getByRole("textbox", { name: "Voice cleanup instructions" })).toHaveValue(
    api.defaultCleanupInstructions,
  );
  fireEvent.change(screen.getByRole("textbox", { name: "Voice cleanup instructions" }), {
    target: { value: "Use short sentences." },
  });
  fireEvent.click(screen.getByRole("button", { name: "Restore default" }));
  expect(screen.getByRole("textbox", { name: "Voice cleanup instructions" })).toHaveValue(
    api.defaultCleanupInstructions,
  );
  fireEvent.change(screen.getByRole("textbox", { name: "Voice cleanup instructions" }), {
    target: { value: "Use short sentences." },
  });
  await act(async () =>
    fireEvent.click(screen.getByRole("button", { name: "Save voice settings" })),
  );
  expect(api.saveVoiceInputSettings).toHaveBeenCalledWith({
    ...api.defaultVoiceInputSettings,
    words: "ExampleApp may be heard as example app",
    mode: "off",
    instructions: "Use short sentences.",
    model: "openai/test-cleanup",
  });
  expect(screen.getByRole("status")).toHaveTextContent("Voice settings saved");
});

it("reports a failed save and preserves the vocabulary draft", async () => {
  vi.mocked(api.saveVoiceInputSettings).mockRejectedValue(new Error("Save unavailable"));
  render(<VoiceInputSettings />);
  await act(async () => {});
  fireEvent.change(screen.getByRole("textbox", { name: "Voice word list" }), {
    target: { value: "keep my draft" },
  });
  await act(async () =>
    fireEvent.click(screen.getByRole("button", { name: "Save voice settings" })),
  );
  expect(screen.getByRole("textbox", { name: "Voice word list" })).toHaveValue("keep my draft");
  expect(screen.getByRole("status")).toHaveTextContent("Save unavailable");
});

it("edits and restores the complete transcription rules", async () => {
  render(<VoiceInputSettings />);
  await act(async () => {});
  await choose("Speech source", "Third-party provider");
  const field = screen.getByRole("textbox", { name: "Transcription instructions" });
  expect(field).toHaveValue(api.defaultTranscriptionInstructions);
  fireEvent.change(field, { target: { value: "Custom recognition rules" } });
  fireEvent.click(screen.getByRole("button", { name: "Restore transcription default" }));
  expect(field).toHaveValue(api.defaultTranscriptionInstructions);
});

it("offers fetched models in both slots with explicit custom IDs and preserves saved custom models", async () => {
  vi.mocked(api.getVoiceInputSettings).mockResolvedValue({
    ...api.defaultVoiceInputSettings,
    speech_source: "third",
    speech_connection: "example",
    cleanup_connection: "example",
    model: "saved-custom-model",
  });
  render(<VoiceInputSettings />);
  await act(async () => {});
  expect(screen.getByRole("textbox", { name: "Cleanup model custom model ID" })).toHaveValue(
    "saved-custom-model",
  );
  await choose("Speech model", "example-speech");
  await choose("Voice cleanup model", "example-cleanup");
  expect(screen.queryByRole("textbox", { name: "Cleanup model custom model ID" })).toBeNull();
  await choose("Speech model", "Custom model ID…");
  fireEvent.change(screen.getByRole("textbox", { name: "Speech model custom model ID" }), {
    target: { value: "custom-audio-model" },
  });
  await act(async () =>
    fireEvent.click(screen.getByRole("button", { name: "Save voice settings" })),
  );
  expect(api.saveVoiceInputSettings).toHaveBeenCalledWith(
    expect.objectContaining({ speech_model: "custom-audio-model", model: "example-cleanup" }),
  );
});
it("shows one-step choice in the speech group and marks separate cleanup skipped", async () => {
  vi.mocked(api.getVoiceInputSettings).mockResolvedValue({
    ...api.defaultVoiceInputSettings,
    speech_source: "third",
    speech_connection: "example",
  });
  render(<VoiceInputSettings />);
  await act(async () => {});
  const group = screen.getByRole("group", { name: "Speech to text" });
  const toggle = within(group).getByRole("switch", { name: "Also clean up in the same request" });
  fireEvent.click(toggle);
  expect(toggle).toHaveAttribute("aria-checked", "true");
  expect(screen.getByRole("combobox", { name: "Speech method" })).toHaveTextContent(
    "Audio chat + cleanup",
  );
  expect(screen.getByText("Skipped: speech also cleans up.")).toBeVisible();
  expect(screen.getByRole("combobox", { name: "Voice cleanup mode" })).toBeDisabled();
  await act(async () =>
    fireEvent.click(screen.getByRole("button", { name: "Save voice settings" })),
  );
  expect(api.saveVoiceInputSettings).toHaveBeenCalledWith(
    expect.objectContaining({ speech_method: "chat_audio" }),
  );
  fireEvent.click(toggle);
  expect(screen.queryByText("Skipped: speech also cleans up.")).toBeNull();
  expect(screen.getByRole("combobox", { name: "Voice cleanup mode" })).not.toBeDisabled();
});
it("hides one-step for a Responses provider", async () => {
  vi.mocked(connectionsApi.listModelConnections).mockResolvedValue({
    can_manage: true,
    connections: [{ ...provider, wire_api: "responses" }],
  });
  vi.mocked(api.getVoiceInputSettings).mockResolvedValue({
    ...api.defaultVoiceInputSettings,
    speech_source: "third",
    speech_connection: "example",
  });
  render(<VoiceInputSettings />);
  await act(async () => {});
  expect(screen.queryByRole("switch", { name: "Also clean up in the same request" })).toBeNull();
  fireEvent.keyDown(screen.getByRole("combobox", { name: "Speech method" }), { key: "Enter" });
  expect(screen.queryByRole("option", { name: "Audio chat + cleanup" })).toBeNull();
});
