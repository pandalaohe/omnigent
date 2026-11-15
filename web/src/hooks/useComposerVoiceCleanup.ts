import { type RefObject, useEffect, useRef, useState } from "react";
import {
  cleanupDictation,
  getVoiceInputSettings,
  VOICE_INPUT_CHANGED_EVENT,
} from "@/lib/voiceInputApi";
import { replaceDictatedText } from "@/lib/replaceDictatedText";

interface Take {
  original: string;
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
  const setter = useRef(setDraft);
  setter.current = setDraft;
  const take = useRef<Take | null>(null);
  const abort = useRef<AbortController | null>(null);
  const enabled = useRef(false);
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
          if (alive) enabled.current = settings.mode !== "off" && !!settings.model.trim();
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
        expected: live.current.draft,
        revision: live.current.revision,
        field: inputRef.current,
      };
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
