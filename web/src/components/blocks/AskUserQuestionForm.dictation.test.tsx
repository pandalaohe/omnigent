import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ComposerMicButtonProps } from "@/components/ComposerMicButton";
import { AskUserQuestionForm } from "./AskUserQuestionForm";

let mic: ComposerMicButtonProps;
vi.mock("@/components/ComposerMicButton", () => ({
  ComposerMicButton: (props: ComposerMicButtonProps) => {
    mic = props;
    return (
      <button type="button" onClick={() => props.onVoiceStart?.()}>
        Voice dictation
      </button>
    );
  },
}));
afterEach(cleanup);

function renderCard() {
  return render(
    <AskUserQuestionForm
      questions={[
        { id: "first", question: "First question", multiSelect: false, options: [] },
        { id: "second", question: "Second question", multiSelect: false, options: [] },
      ]}
      onSubmit={vi.fn()}
      onReject={vi.fn()}
    />,
  );
}
async function dictate(text = "Final answer.") {
  fireEvent.click(screen.getByRole("button", { name: "Voice dictation" }));
  act(() => mic.onTranscript(text, { cleaned: true, cleanupHandled: true }));
}
async function stop() {
  act(() => mic.onVoiceEnd?.());
}

describe("question-card finalized dictation", () => {
  it("inserts cleaned card text once without creating a browser undo-to-raw entry", async () => {
    renderCard();
    await act(async () => {});
    fireEvent.click(screen.getByRole("button", { name: "Voice dictation" }));
    const descriptor = Object.getOwnPropertyDescriptor(document, "execCommand");
    const command = vi.fn(() => true);
    Object.defineProperty(document, "execCommand", { configurable: true, value: command });
    try {
      act(() =>
        mic.onTranscript("Final answer.", {
          raw: "unprocessed answer",
          cleaned: true,
          cleanupHandled: true,
        }),
      );
      await stop();
      expect(screen.getByRole("textbox", { name: "Custom answer" })).toHaveValue("Final answer.");
      expect(command).not.toHaveBeenCalled();
    } finally {
      if (descriptor) Object.defineProperty(document, "execCommand", descriptor);
      else delete (document as Partial<Document>).execCommand;
    }
  });
  it("toggles clear and restore without browser undo and replaces the remembered text after typing", () => {
    renderCard();
    const field = screen.getByRole("textbox", { name: "Custom answer" });
    expect(screen.getByRole("button", { name: "Clear answer" })).toBeDisabled();
    fireEvent.change(field, { target: { value: "first answer" } });
    fireEvent.click(screen.getByRole("button", { name: "Clear answer" }));
    expect(field).toHaveValue("");
    const restore = screen.getByRole("button", { name: "Restore answer" });
    expect(restore).toHaveAttribute("title", "Restore answer");
    fireEvent.click(restore);
    expect(field).toHaveValue("first answer");
    fireEvent.click(screen.getByRole("button", { name: "Clear answer" }));
    fireEvent.change(field, { target: { value: "new answer" } });
    expect(screen.getByRole("button", { name: "Clear answer" })).toHaveAttribute(
      "title",
      "Clear answer",
    );
    fireEvent.click(screen.getByRole("button", { name: "Clear answer" }));
    fireEvent.click(screen.getByRole("button", { name: "Restore answer" }));
    expect(field).toHaveValue("new answer");
    fireEvent.click(screen.getByRole("button", { name: "Clear answer" }));
    fireEvent.change(field, { target: { value: "temporary" } });
    fireEvent.change(field, { target: { value: "" } });
    expect(screen.getByRole("button", { name: "Clear answer" })).toBeDisabled();
  });
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
    await dictate();
    await stop();
    expect(screen.getByRole("textbox", { name: "Custom answer" })).toHaveFocus();
    expect(screen.getByRole("radio", { name: "Select custom answer" })).toBeChecked();
    expect(screen.getByRole("radio", { name: /Default/ })).not.toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: "Submit" }));
    expect(onSubmit).toHaveBeenCalledWith({ answer: "Final answer." });
  });

  it("inserts final text at the saved caret while preserving typed surroundings", async () => {
    renderCard();
    const field = screen.getByRole("textbox", { name: "Custom answer" }) as HTMLTextAreaElement;
    fireEvent.change(field, { target: { value: "typed prefix | suffix" } });
    field.setSelectionRange(13, 13);
    fireEvent.select(field);
    await dictate();
    await stop();
    expect(field).toHaveValue("typed prefix Final answer.| suffix");
  });

  it("preserves clear and restore against late results, including restoring the same text", async () => {
    renderCard();
    await dictate("old words");
    await stop();
    const oldMic = mic;
    const field = screen.getByRole("textbox", { name: "Custom answer" });
    fireEvent.click(screen.getByRole("button", { name: "Clear answer" }));
    act(() => oldMic.onTranscript("late result"));
    expect(field).toHaveValue("");
    fireEvent.click(screen.getByRole("button", { name: "Restore answer" }));
    act(() => oldMic.onTranscript("late result"));
    expect(field).toHaveValue("old words");
    expect(field).toHaveFocus();
  });

  it("rejects a final result after editing away and back", async () => {
    renderCard();
    fireEvent.click(screen.getByRole("button", { name: "Voice dictation" }));
    const field = screen.getByRole("textbox", { name: "Custom answer" });
    fireEvent.change(field, { target: { value: "new words" } });
    fireEvent.change(field, { target: { value: "" } });
    act(() => mic.onTranscript("late result"));
    await stop();
    expect(field).toHaveValue("");
    expect(screen.getByText("Draft changed; stop and start a new take to continue.")).toBeVisible();
  });

  it("rejects callbacks after question switch and unmount", async () => {
    const view = renderCard();
    await dictate("first answer");
    await stop();
    const oldMic = mic;
    fireEvent.click(screen.getByRole("button", { name: /Next/ }));
    act(() => {
      oldMic.onTranscript("late words");
      oldMic.onVoiceEnd?.();
    });
    expect(screen.getByRole("textbox", { name: "Custom answer" })).toHaveValue("");
    fireEvent.click(screen.getByRole("button", { name: /Prev/ }));
    expect(screen.getByRole("textbox", { name: "Custom answer" })).toHaveValue("first answer");
    view.unmount();
    act(() => {
      oldMic.onTranscript("after collapse");
      oldMic.onVoiceEnd?.();
    });
  });

  it("preserves selected text when the take is discarded before delivery", () => {
    renderCard();
    const field = screen.getByRole("textbox", { name: "Custom answer" }) as HTMLTextAreaElement;
    fireEvent.change(field, { target: { value: "selected" } });
    field.setSelectionRange(0, 8);
    fireEvent.select(field);
    fireEvent.click(screen.getByRole("button", { name: "Voice dictation" }));
    act(() => mic.onVoiceDiscard?.());
    act(() => mic.onTranscript("late result"));
    expect(field).toHaveValue("selected");
  });
});
