import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import * as api from "@/lib/voiceInputApi";
import * as connectionsApi from "@/lib/modelConnectionsApi";
import { VoiceInputSettings } from "./VoiceInputSettings";

vi.mock("@/lib/voiceInputApi", async (importOriginal) => ({
  ...(await importOriginal<typeof api>()),
  getVoiceInputSettings: vi.fn(),
  saveVoiceInputSettings: vi.fn(),
  testVoiceModel: vi.fn(),
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
  models: [
    { key: "speech", display_name: "Speech model", model_id: "example-speech", status: "listed" },
    {
      key: "cleanup",
      display_name: "Cleanup model",
      model_id: "example-cleanup",
      status: "listed",
    },
  ],
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
  await choose("Cleanup provider", "Example provider");
  await choose("Voice cleanup model", "Cleanup model");
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
    cleanup_connection: "example",
    cleanup_row: "cleanup",
    model: "example-cleanup",
  });
  expect(screen.getAllByRole("status").at(-1)).toHaveTextContent("Voice settings saved");
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
  expect(screen.getAllByRole("status").at(-1)).toHaveTextContent("Save unavailable");
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

it("picks by display name and saves stable keys with legacy ids", async () => {
  vi.mocked(api.getVoiceInputSettings).mockResolvedValue({
    ...api.defaultVoiceInputSettings,
    speech_source: "third",
    speech_connection: "example",
    cleanup_connection: "example",
    model: "saved-custom-model",
  });
  render(<VoiceInputSettings />);
  await act(async () => {});
  expect(screen.getByText(/Pick the model again/)).toBeVisible();
  expect(screen.queryByText("Custom model ID…")).toBeNull();
  await choose("Speech model", "Speech model");
  await choose("Voice cleanup model", "Cleanup model");
  await act(async () =>
    fireEvent.click(screen.getByRole("button", { name: "Save voice settings" })),
  );
  expect(api.saveVoiceInputSettings).toHaveBeenCalledWith(
    expect.objectContaining({
      speech_row: "speech",
      speech_model: "example-speech",
      cleanup_row: "cleanup",
      model: "example-cleanup",
    }),
  );
});
it("keeps unmatched legacy id when only vocabulary changes", async () => {
  vi.mocked(api.getVoiceInputSettings).mockResolvedValue({
    ...api.defaultVoiceInputSettings,
    cleanup_connection: "example",
    model: "legacy-unmatched",
  });
  render(<VoiceInputSettings />);
  await act(async () => {});
  fireEvent.change(screen.getByRole("textbox", { name: "Voice word list" }), {
    target: { value: "New word" },
  });
  await act(async () =>
    fireEvent.click(screen.getByRole("button", { name: "Save voice settings" })),
  );
  expect(api.saveVoiceInputSettings).toHaveBeenCalledWith(
    expect.objectContaining({ model: "legacy-unmatched", cleanup_row: "" }),
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

it.each([
  "wrong_type",
  "key_rejected",
  "unreachable",
  "gone",
  "timeout",
  "rejected",
  "ok",
] as const)("shows the %s feature-test result for the unsaved selection", async (kind) => {
  vi.mocked(api.getVoiceInputSettings).mockResolvedValue({
    ...api.defaultVoiceInputSettings,
    speech_source: "third",
    speech_connection: "example",
  });
  vi.mocked(api.testVoiceModel).mockResolvedValue({
    ok: kind === "ok",
    kind,
    text: `Result: ${kind}`,
    elapsed_ms: 10,
  });
  render(<VoiceInputSettings />);
  await act(async () => {});
  await choose("Speech model", "Speech model");
  const group = screen.getByRole("group", { name: "Speech to text" });
  await act(async () => fireEvent.click(within(group).getByRole("button", { name: "Test" })));
  expect(api.testVoiceModel).toHaveBeenCalledWith({
    slot: "speech",
    connection: "example",
    row: "speech",
    method: "transcription",
  });
  expect(within(group).getByRole("status")).toHaveTextContent(`Result: ${kind}`);
  expect(api.saveVoiceInputSettings).not.toHaveBeenCalled();
});
it("tests the unsaved cleanup selection and clears its result after another model is picked", async () => {
  vi.mocked(api.getVoiceInputSettings).mockResolvedValue({
    ...api.defaultVoiceInputSettings,
    cleanup_connection: "example",
  });
  vi.mocked(api.testVoiceModel).mockResolvedValue({
    ok: true,
    kind: "ok",
    text: "Cleanup test passed",
    elapsed_ms: 10,
  });
  render(<VoiceInputSettings />);
  await act(async () => {});
  await choose("Voice cleanup model", "Cleanup model");
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Test" })));
  expect(api.testVoiceModel).toHaveBeenCalledWith({
    slot: "cleanup",
    connection: "example",
    row: "cleanup",
    method: "transcription",
  });
  expect(screen.getByText("Cleanup test passed")).toBeVisible();
  await choose("Voice cleanup model", "Speech model");
  expect(screen.queryByText("Cleanup test passed")).toBeNull();
});
it("uses a typeahead above five choices and keeps a select below the threshold", async () => {
  vi.mocked(api.getVoiceInputSettings).mockResolvedValue({
    ...api.defaultVoiceInputSettings,
    speech_source: "third",
    speech_connection: "example",
  });
  vi.mocked(connectionsApi.listModelConnections).mockResolvedValue({
    can_manage: true,
    connections: [
      {
        ...provider,
        models: Array.from({ length: 8 }, (_, i) => ({
          key: `row-${i}`,
          display_name: `Choice ${i}`,
          model_id: `id-${i}`,
          status: "listed",
        })),
      },
    ],
  });
  render(<VoiceInputSettings />);
  await act(async () => {});
  expect(screen.getByRole("combobox", { name: "Speech provider" }).tagName).toBe("BUTTON");
  expect(screen.getByRole("combobox", { name: "Speech model" }).tagName).toBe("INPUT");
});

it("uses a typeahead when more than five speech providers are available", async () => {
  vi.mocked(api.getVoiceInputSettings).mockResolvedValue({
    ...api.defaultVoiceInputSettings,
    speech_source: "third",
  });
  vi.mocked(connectionsApi.listModelConnections).mockResolvedValue({
    can_manage: true,
    connections: Array.from({ length: 6 }, (_, index) => ({
      ...provider,
      name: `provider-${index}`,
      display_name: `Provider ${index}`,
    })),
  });
  render(<VoiceInputSettings />);
  await act(async () => {});
  expect(screen.getByRole("combobox", { name: "Speech provider" }).tagName).toBe("INPUT");
});

it("refreshes rollback-compatible model id on save after an administrator changes the selected row", async () => {
  vi.mocked(api.getVoiceInputSettings).mockResolvedValue({
    ...api.defaultVoiceInputSettings,
    cleanup_connection: "example",
    cleanup_row: "cleanup",
    model: "previous-cleanup",
  });
  render(<VoiceInputSettings />);
  await act(async () => {});
  await act(async () =>
    fireEvent.click(screen.getByRole("button", { name: "Save voice settings" })),
  );
  expect(api.saveVoiceInputSettings).toHaveBeenCalledWith(
    expect.objectContaining({ cleanup_row: "cleanup", model: "example-cleanup" }),
  );
});
