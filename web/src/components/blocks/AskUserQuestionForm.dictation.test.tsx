import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ComposerMicButtonProps } from "@/components/ComposerMicButton";
import { defaultVoiceInputSettings } from "@/lib/voiceInputApi";
import * as voiceApi from "@/lib/voiceInputApi";
import { AskUserQuestionForm } from "./AskUserQuestionForm";

vi.mock("@/lib/voiceInputApi", async (importOriginal) => ({
  ...(await importOriginal<typeof voiceApi>()),
  getVoiceInputSettings: vi.fn(),
  cleanupDictation: vi.fn(),
}));
let mic: ComposerMicButtonProps;
vi.mock("@/components/ComposerMicButton", () => ({
  ComposerMicButton: (props: ComposerMicButtonProps) => {
    mic = props;
    return (
      <button
        type="button"
        onClick={() => {
          props.onVoiceStart?.();
          props.onListeningChange?.(true);
        }}
      >
        Voice dictation
      </button>
    );
  },
}));
afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(voiceApi.getVoiceInputSettings).mockResolvedValue({
    ...defaultVoiceInputSettings,
    model: "openai/test-cleanup",
  });
  vi.mocked(voiceApi.cleanupDictation).mockResolvedValue({
    text: "Do not deploy /opt/work/app at 12:30.",
  });
});

function renderCard() {
  const onSubmit = vi.fn();
  const onReject = vi.fn();
  const view = render(
    <AskUserQuestionForm
      questions={[
        { id: "first", question: "First question", multiSelect: false, options: [] },
        { id: "second", question: "Second question", multiSelect: false, options: [] },
      ]}
      onSubmit={onSubmit}
      onReject={onReject}
    />,
  );
  return { ...view, onSubmit, onReject };
}
async function dictate(text = "don't deploy /opt/work/app at 12:30") {
  await act(async () => {});
  fireEvent.click(screen.getByRole("button", { name: "Voice dictation" }));
  act(() => mic.onTranscript(text));
}
async function stop() {
  await act(async () => {
    mic.onListeningChange?.(false);
    mic.onVoiceEnd?.();
  });
}

describe("question-card dictated segments", () => {
  it("selects and focuses the dictated answer instead of the default option, then submits it", async () => {
    const onSubmit = vi.fn();
    render(
      <AskUserQuestionForm
        questions={[
          {
            id: "answer",
            question: "Choose",
            multiSelect: false,
            options: [{ label: "Default", description: "Preset" }],
          },
        ]}
        onSubmit={onSubmit}
        onReject={vi.fn()}
      />,
    );
    expect(screen.getByRole("radio", { name: /Default/ })).toBeChecked();
    await dictate("spoken answer");
    await stop();
    const field = screen.getByRole("textbox", { name: "Custom answer" });
    expect(field).toHaveFocus();
    expect(screen.getByRole("radio", { name: "Select custom answer" })).toBeChecked();
    expect(screen.getByRole("radio", { name: /Default/ })).not.toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: "Submit" }));
    expect(onSubmit).toHaveBeenCalledWith({ answer: "Do not deploy /opt/work/app at 12:30." });
  });

  it("clears the answer in one click and rejects an outstanding cleanup", async () => {
    let finish!: (value: { text: string }) => void;
    vi.mocked(voiceApi.cleanupDictation).mockReturnValue(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    renderCard();
    await dictate("old words");
    await stop();
    const field = screen.getByRole("textbox", { name: "Custom answer" });
    fireEvent.click(screen.getByRole("button", { name: "Clear answer" }));
    expect(field).toHaveValue("");
    expect(field).toHaveFocus();
    await act(async () => finish({ text: "late cleanup" }));
    expect(field).toHaveValue("");
    expect(screen.getByRole("button", { name: "Clear answer" })).toBeDisabled();
  });

  it("replaces only the segment at the cursor with no extra buttons", async () => {
    const { onSubmit, onReject } = renderCard();
    const field = screen.getByRole("textbox", { name: "Custom answer" });
    fireEvent.change(field, { target: { value: "typed prefix | suffix" } });
    (field as HTMLTextAreaElement).setSelectionRange(13, 13);
    fireEvent.select(field);
    await dictate();
    await stop();
    expect(voiceApi.cleanupDictation).toHaveBeenCalledWith(
      "don't deploy /opt/work/app at 12:30",
      expect.any(AbortSignal),
    );
    expect(field).toHaveValue("typed prefix Do not deploy /opt/work/app at 12:30.| suffix");
    expect(screen.queryByText("Changed voice text")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Restore original" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Clean up voice" })).not.toBeInTheDocument();
    expect(onSubmit).not.toHaveBeenCalled();
    expect(onReject).not.toHaveBeenCalled();
  });

  it("rejects a cleanup result after typing even when the text is changed back", async () => {
    let finish!: (value: { text: string }) => void;
    vi.mocked(voiceApi.cleanupDictation).mockReturnValue(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    renderCard();
    await dictate("raw words");
    await stop();
    const field = screen.getByRole("textbox", { name: "Custom answer" });
    fireEvent.change(field, { target: { value: "new typed words" } });
    fireEvent.change(field, { target: { value: "raw words" } });
    await act(async () => finish({ text: "cleaned words" }));
    expect(field).toHaveValue("raw words");
    expect(screen.getByText("Draft changed; original kept.")).toBeVisible();
  });

  it("voids callbacks and pending cleanup on question switch and collapse", async () => {
    let finish!: (value: { text: string }) => void;
    vi.mocked(voiceApi.cleanupDictation).mockReturnValue(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    const view = renderCard();
    await dictate("first answer");
    const oldMic = mic;
    await stop();
    fireEvent.click(screen.getByRole("button", { name: /Next/ }));
    act(() => oldMic.onTranscript("late words"));
    await act(async () => finish({ text: "late cleanup" }));
    expect(screen.getByRole("textbox", { name: "Custom answer" })).toHaveValue("");
    fireEvent.click(screen.getByRole("button", { name: /Prev/ }));
    expect(screen.getByRole("textbox", { name: "Custom answer" })).toHaveValue("first answer");
    view.unmount();
    act(() => oldMic.onTranscript("after collapse"));
  });

  it("maps legacy manual mode to automatic without sending handwritten text", async () => {
    vi.mocked(voiceApi.getVoiceInputSettings).mockResolvedValue({
      ...defaultVoiceInputSettings,
      model: "openai/test-cleanup",
      mode: "manual",
    });
    renderCard();
    fireEvent.change(screen.getByRole("textbox", { name: "Custom answer" }), {
      target: { value: "handwritten " },
    });
    await dictate("spoken");
    await stop();
    await waitFor(() =>
      expect(voiceApi.cleanupDictation).toHaveBeenCalledWith("spoken", expect.any(AbortSignal)),
    );
  });

  it("keeps original text on failure and on Escape discard preserves the selected text", async () => {
    vi.mocked(voiceApi.cleanupDictation).mockRejectedValue(new Error("unavailable"));
    renderCard();
    const field = screen.getByRole("textbox", { name: "Custom answer" });
    fireEvent.change(field, { target: { value: "selected" } });
    (field as HTMLTextAreaElement).setSelectionRange(0, 8);
    fireEvent.select(field);
    await dictate("dictated");
    act(() => mic.onVoiceDiscard?.());
    expect(field).toHaveValue("selected");
    await dictate("raw");
    await stop();
    expect(field).toHaveValue("selectedraw");
    expect(screen.getByText("Cleanup unavailable; original kept.")).toBeVisible();
  });
});

it("leaves dictated text unchanged when cleanup is off", async () => {
  vi.mocked(voiceApi.getVoiceInputSettings).mockResolvedValue({
    ...defaultVoiceInputSettings,
    model: "test-model",
    mode: "off",
  });
  renderCard();
  await dictate("raw words");
  await stop();
  expect(voiceApi.cleanupDictation).not.toHaveBeenCalled();
  expect(screen.getByRole("textbox", { name: "Custom answer" })).toHaveValue("raw words");
});

it("keeps one-step results and cleans only a browser fallback suffix", async () => {
  vi.mocked(voiceApi.getVoiceInputSettings).mockResolvedValue({
    ...defaultVoiceInputSettings,
    model: "test-cleanup",
    speech_source: "third",
    speech_method: "chat_audio",
  });
  vi.mocked(voiceApi.cleanupDictation).mockResolvedValue({ text: "Browser tail." });
  renderCard();
  await act(async () => {});
  fireEvent.click(screen.getByRole("button", { name: "Voice dictation" }));
  act(() =>
    mic.onTranscript("Clean prefix.", { raw: "raw prefix", cleaned: true, source: "third" }),
  );
  act(() => mic.onTranscript("browser tail"));
  await stop();
  expect(voiceApi.cleanupDictation).toHaveBeenCalledWith("browser tail", expect.any(AbortSignal));
  expect(screen.getByRole("textbox", { name: "Custom answer" })).toHaveValue(
    "Clean prefix. Browser tail.",
  );
});
it("skips separate cleanup when one-step returns raw fallback", async () => {
  vi.mocked(voiceApi.getVoiceInputSettings).mockResolvedValue({
    ...defaultVoiceInputSettings,
    model: "test-cleanup",
    speech_source: "third",
    speech_method: "chat_audio",
  });
  renderCard();
  await act(async () => {});
  fireEvent.click(screen.getByRole("button", { name: "Voice dictation" }));
  act(() => mic.onTranscript("original", { cleaned: false, source: "third" }));
  await stop();
  expect(voiceApi.cleanupDictation).not.toHaveBeenCalled();
  expect(screen.getByRole("textbox", { name: "Custom answer" })).toHaveValue("original");
});
