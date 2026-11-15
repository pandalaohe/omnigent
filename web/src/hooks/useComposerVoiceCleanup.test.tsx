import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useRef, useState } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import * as api from "@/lib/voiceInputApi";
import { useComposerVoiceCleanup } from "./useComposerVoiceCleanup";
import { useDictationInsert } from "./useDictationInsert";

vi.mock("@/lib/voiceInputApi", async (original) => ({
  ...(await original<typeof api>()),
  getVoiceInputSettings: vi.fn(),
  cleanupDictation: vi.fn(),
}));
let voice: ReturnType<typeof useComposerVoiceCleanup>;
let dictation: ReturnType<typeof useDictationInsert>;
function Composer({ scope = "one" }: { scope?: string }) {
  const [draft, setDraft] = useState("suffix");
  const field = useRef<HTMLTextAreaElement>(null);
  voice = useComposerVoiceCleanup(draft, setDraft, field, scope);
  dictation = useDictationInsert(draft, voice.produce, field);
  return (
    <textarea
      ref={field}
      value={draft}
      onFocus={dictation.noteFocus}
      onChange={(event) => {
        voice.invalidate();
        setDraft(event.target.value);
      }}
    />
  );
}
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.getVoiceInputSettings).mockResolvedValue({
    ...api.defaultVoiceInputSettings,
    model: "test-cleanup",
  });
  vi.mocked(api.cleanupDictation).mockResolvedValue({ text: "Some words." });
});
async function start() {
  const view = render(<Composer />);
  await act(async () => {});
  const field = screen.getByRole("textbox") as HTMLTextAreaElement;
  act(() => field.focus());
  field.setSelectionRange(0, 0);
  act(() => voice.begin());
  act(() => dictation.appendFinal("some words"));
  return { ...view, field };
}
it("cleans only the whole dictated segment including shared prefix characters and requests native undo", async () => {
  const { field } = await start();
  const command = vi.fn(() => true);
  Object.defineProperty(document, "execCommand", { configurable: true, value: command });
  await act(async () => voice.end());
  expect(api.cleanupDictation).toHaveBeenCalledWith("some words", expect.any(AbortSignal));
  expect(field).toHaveValue("Some words. suffix");
  expect(command).toHaveBeenCalledWith("insertText", false, "Some words.");
  delete (document as Partial<Document>).execCommand;
});
it("does not clean when off", async () => {
  vi.mocked(api.getVoiceInputSettings).mockResolvedValue({
    ...api.defaultVoiceInputSettings,
    model: "test-cleanup",
    mode: "off",
  });
  const { field } = await start();
  await act(async () => voice.end());
  expect(api.cleanupDictation).not.toHaveBeenCalled();
  expect(field).toHaveValue("some words suffix");
});
it.each(["edit", "scope", "unmount"])("rejects late cleanup after %s", async (change) => {
  let resolve!: (value: { text: string }) => void;
  vi.mocked(api.cleanupDictation).mockReturnValue(
    new Promise((done) => {
      resolve = done;
    }),
  );
  const view = await start();
  let done!: Promise<void>;
  act(() => {
    done = voice.end();
  });
  if (change === "edit") {
    fireEvent.change(view.field, { target: { value: "changed" } });
    fireEvent.change(view.field, { target: { value: "some words suffix" } });
  } else if (change === "scope") {
    view.rerender(<Composer scope="two" />);
    expect(voice.notice).toBe("");
  } else view.unmount();
  await act(async () => {
    resolve({ text: "late" });
    await done;
  });
  expect(view.field.value).toBe("some words suffix");
});
it("preserves the original on cleanup failure", async () => {
  vi.mocked(api.cleanupDictation).mockRejectedValue(new Error("unavailable"));
  const { field } = await start();
  await act(async () => voice.end());
  expect(field).toHaveValue("some words suffix");
  expect(voice.notice).toBe("Cleanup unavailable; original kept.");
});

it("does not send one-step text to cleanup a second time", async () => {
  vi.mocked(api.getVoiceInputSettings).mockResolvedValue({
    ...api.defaultVoiceInputSettings,
    model: "test-cleanup",
    speech_source: "third",
    speech_method: "chat_audio",
  });
  const view = render(<Composer />);
  await act(async () => {});
  const field = screen.getByRole("textbox") as HTMLTextAreaElement;
  act(() => field.focus());
  field.setSelectionRange(0, 0);
  act(() => voice.begin());
  act(() => {
    voice.record("Clean text.", { cleaned: true, raw: "raw text", source: "third" });
    dictation.appendFinal("Clean text.");
  });
  await act(async () => voice.end());
  expect(api.cleanupDictation).not.toHaveBeenCalled();
  expect(view.getByRole("textbox")).toHaveValue("Clean text. suffix");
});
it("cleans the browser suffix after one-step recognition fails", async () => {
  vi.mocked(api.getVoiceInputSettings).mockResolvedValue({
    ...api.defaultVoiceInputSettings,
    model: "test-cleanup",
    speech_source: "third",
    speech_method: "chat_audio",
  });
  render(<Composer />);
  await act(async () => {});
  const field = screen.getByRole("textbox") as HTMLTextAreaElement;
  act(() => field.focus());
  field.setSelectionRange(0, 0);
  act(() => voice.begin());
  act(() => {
    voice.record("Clean prefix.", { cleaned: true, source: "third" });
    dictation.appendFinal("Clean prefix.");
  });
  act(() => {
    voice.record("some words");
    dictation.appendFinal("some words");
  });
  await act(async () => voice.end());
  expect(api.cleanupDictation).toHaveBeenCalledWith("some words", expect.any(AbortSignal));
  expect(field).toHaveValue("Clean prefix. Some words. suffix");
});

it("applies one-step output through the native undo seam before the next segment", async () => {
  render(<Composer />);
  await act(async () => {});
  const field = screen.getByRole("textbox") as HTMLTextAreaElement;
  act(() => field.focus());
  field.setSelectionRange(0, 0);
  act(() => voice.begin());
  act(() => {
    voice.record("Clean.", { cleaned: true, raw: "raw words", source: "third" });
    dictation.appendFinal("raw words");
  });
  const command = vi.fn(() => true);
  Object.defineProperty(document, "execCommand", { configurable: true, value: command });
  act(() => voice.replaceFinal("raw words", "Clean."));
  expect(command).toHaveBeenCalledWith("insertText", false, "Clean.");
  expect(field).toHaveValue("Clean. suffix");
  delete (document as Partial<Document>).execCommand;
  act(() => {
    voice.record("some words");
    dictation.appendFinal("some words");
  });
  await act(async () => voice.end());
  expect(api.cleanupDictation).toHaveBeenCalledWith("some words", expect.any(AbortSignal));
  expect(field).toHaveValue("Clean. Some words. suffix");
});
