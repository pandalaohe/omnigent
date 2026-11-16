import { type RefObject, useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  cleanupDictation,
  getVoiceInputSettings,
  VOICE_INPUT_CHANGED_EVENT,
} from "@/lib/voiceInputApi";
import type { ComposerMicButtonProps } from "@/components/ComposerMicButton";
import { replaceDictatedText } from "@/lib/replaceDictatedText";

interface Take {
  original: string;
  segments: { text: string; cleaned: boolean; source?: string }[];
  start: number | null;
  end: number;
  expected: string;
  revision: number;
  field: HTMLTextAreaElement | null;
}

/** Observe only dictation writes; any other draft revision invalidates the take. */
export function useComposerVoiceCleanup(
  draft: string,
  setDraft: (value: string) => void,
  inputRef: RefObject<HTMLTextAreaElement | null>,
  scope: string,
) {
  const live = useRef({ draft, revision: 0, scope });
  if (live.current.draft !== draft || live.current.scope !== scope) {
    live.current = { draft, scope, revision: live.current.revision + 1 };
  }
  const pendingCaret = useRef<{ value: string; position: number } | null>(null);
  useLayoutEffect(() => {
    const pending = pendingCaret.current;
    pendingCaret.current = null;
    if (pending?.value === draft)
      inputRef.current?.setSelectionRange(pending.position, pending.position);
  });
  const setter = useRef(setDraft);
  setter.current = setDraft;
  const take = useRef<Take | null>(null);
  const abort = useRef<AbortController | null>(null);
  const enabled = useRef(false);
  const oneStep = useRef(false);
  const [notice, setNotice] = useState("");
  const matches = (value: Take) =>
    live.current.draft === value.expected &&
    live.current.revision === value.revision &&
    inputRef.current === value.field;

  useEffect(() => {
    setNotice("");
    let alive = true;
    const refresh = () =>
      void getVoiceInputSettings()
        .then((settings) => {
          if (alive) {
            enabled.current = settings.mode !== "off" && !!settings.model.trim();
            oneStep.current =
              settings.speech_source === "third" && settings.speech_method === "chat_audio";
          }
        })
        .catch(() => {});
    refresh();
    window.addEventListener(VOICE_INPUT_CHANGED_EVENT, refresh);
    return () => {
      alive = false;
      abort.current?.abort();
      take.current = null;
      window.removeEventListener(VOICE_INPUT_CHANGED_EVENT, refresh);
    };
  }, [scope]);
  useEffect(() => {
    if (!notice || notice === "Cleaning up voice…") return;
    const timer = window.setTimeout(() => setNotice(""), 4000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  const write = (value: string) => {
    live.current.draft = value;
    live.current.revision++;
    setter.current(value);
  };
  return {
    notice,
    invalidate: () => {
      live.current.revision++;
    },
    begin: () => {
      abort.current?.abort();
      setNotice("");
      take.current = {
        start: null,
        end: 0,
        original: live.current.draft,
        segments: [],
        expected: live.current.draft,
        revision: live.current.revision,
        field: inputRef.current,
      };
    },
    record: (text: string, meta?: Parameters<ComposerMicButtonProps["onTranscript"]>[1]) => {
      take.current?.segments.push({
        text,
        cleaned: meta?.cleaned === true || meta?.cleanupHandled === true,
        source: meta?.source,
      });
    },
    replaceFinal: (original: string, text: string) => {
      const current = take.current;
      if (!current || current.start === null || !matches(current)) return;
      let end = current.end;
      while (end > current.start && /\s/.test(current.expected[end - 1])) end--;
      const start = end - original.length;
      if (start < current.start || current.expected.slice(start, end) !== original) return;
      const next = current.expected.slice(0, start) + text + current.expected.slice(end);
      replaceDictatedText(current.field, current.expected, start, end, text);
      pendingCaret.current = { value: next, position: start + text.length };
      write(next);
      current.end += text.length - original.length;
      current.expected = next;
      current.revision = live.current.revision;
      current.field?.setSelectionRange(start + text.length, start + text.length);
    },
    produce: (value: string, region?: { start: number; text: string }) => {
      const current = take.current;
      if (
        current &&
        (!matches(current) ||
          (region &&
            current.start !== null &&
            (region.start > current.end || region.start + region.text.length < current.start)))
      )
        take.current = null;
      const delta = value.length - live.current.draft.length;
      write(value);
      if (take.current && region) {
        take.current.start = Math.min(take.current.start ?? region.start, region.start);
        take.current.end = Math.max(take.current.end + delta, region.start + region.text.length);
        take.current.expected = value;
        take.current.revision = live.current.revision;
      }
    },
    discard: () => {
      const current = take.current;
      abort.current?.abort();
      if (current && matches(current)) write(current.original);
      take.current = null;
      setNotice("");
    },
    end: async () => {
      const current = take.current;
      if (!current || !matches(current) || !enabled.current) return;
      take.current = null;
      if (current.start === null) return;
      let start = current.start;
      let end = current.end;
      // Padding inserted by the composer belongs to the surrounding draft.
      while (start < end && /\s/.test(current.expected[start])) start++;
      while (end > start && /\s/.test(current.expected[end - 1])) end--;
      if (current.segments.length) {
        const first = current.segments.findIndex(
          (segment) => !segment.cleaned && (!oneStep.current || segment.source !== "third"),
        );
        if (first === -1) return;
        if (first > 0) {
          const prefix = current.segments
            .slice(0, first)
            .map((segment) => segment.text)
            .join(" ");
          if (!current.expected.slice(start, end).startsWith(prefix)) return;
          start += prefix.length;
          while (start < end && /\s/.test(current.expected[start])) start++;
        }
      }
      const raw = current.expected.slice(start, end);
      if (!raw.trim()) return;
      const controller = new AbortController();
      abort.current = controller;
      setNotice("Cleaning up voice…");
      try {
        const result = await cleanupDictation(raw, controller.signal);
        if (controller.signal.aborted) return;
        if (!matches(current)) {
          setNotice("Draft changed; original kept.");
          return;
        }
        if (result.text !== raw) {
          replaceDictatedText(current.field, current.expected, start, end, result.text);
          write(current.expected.slice(0, start) + result.text + current.expected.slice(end));
        }
        setNotice("");
      } catch {
        if (!controller.signal.aborted) setNotice("Cleanup unavailable; original kept.");
      }
    },
  };
}
