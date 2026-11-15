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
  it("replaces only the segment at the cursor and restores it without submitting", async () => {
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
    expect(screen.getByLabelText("Changed voice text").querySelector("mark")).toHaveTextContent(
      "Do not deploy",
    );
    fireEvent.click(screen.getByRole("button", { name: "Restore original" }));
    expect(field).toHaveValue("typed prefix don't deploy /opt/work/app at 12:30| suffix");
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

  it("does not send handwritten text and supports manual or off mode", async () => {
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
    expect(voiceApi.cleanupDictation).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Clean up voice" }));
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
