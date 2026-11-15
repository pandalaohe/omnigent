import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import * as api from "@/lib/voiceInputApi";
import { VoiceInputSettings } from "./VoiceInputSettings";

vi.mock("@/lib/voiceInputApi", async (importOriginal) => ({
  ...(await importOriginal<typeof api>()),
  getVoiceInputSettings: vi.fn(),
  saveVoiceInputSettings: vi.fn(),
}));
afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.getVoiceInputSettings).mockResolvedValue(api.defaultVoiceInputSettings);
  vi.mocked(api.saveVoiceInputSettings).mockImplementation(async (settings) => settings);
});

it("loads and saves vocabulary, manual mode and thinking parameters through the shared settings API", async () => {
  render(<VoiceInputSettings />);
  await act(async () => {});
  fireEvent.change(screen.getByRole("textbox", { name: "Voice word list" }), {
    target: { value: "ExampleApp may be heard as example app" },
  });
  fireEvent.change(screen.getByRole("combobox", { name: "Voice cleanup mode" }), {
    target: { value: "manual" },
  });
  fireEvent.click(screen.getByText("Cleanup model", { exact: true }));
  fireEvent.change(screen.getByRole("textbox", { name: "Voice cleanup model" }), {
    target: { value: "openai/test-cleanup" },
  });
  fireEvent.change(screen.getByRole("textbox", { name: "Voice thinking parameters" }), {
    target: {
      value:
        '{"reasoning_effort":"low","reasoning":{"effort":"low"},"thinking":{"type":"disabled"}}',
    },
  });
  await act(async () =>
    fireEvent.click(screen.getByRole("button", { name: "Save voice settings" })),
  );
  expect(api.saveVoiceInputSettings).toHaveBeenCalledWith({
    ...api.defaultVoiceInputSettings,
    words: "ExampleApp may be heard as example app",
    mode: "manual",
    model: "openai/test-cleanup",
    reasoning_effort: "low",
    reasoning: { effort: "low" },
    thinking: { type: "disabled" },
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
