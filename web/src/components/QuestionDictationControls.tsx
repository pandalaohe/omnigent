import { flushSync } from "react-dom";
import { type RefObject, useEffect, useRef, useState } from "react";
import { ComposerMicButton } from "@/components/ComposerMicButton";
import { replaceDictatedText } from "@/lib/replaceDictatedText";
import {
  cleanupDictation,
  getVoiceInputSettings,
  VOICE_INPUT_CHANGED_EVENT,
} from "@/lib/voiceInputApi";

interface Take {
  before: string;
  after: string;
  replaced: string;
  raw: string;
  segments: { shown: string; original: string; cleaned: boolean; source?: string }[];
  expected: string;
  revision: number;
}

interface Props {
  inputRef: RefObject<HTMLTextAreaElement | null>;
  getText: () => string;
  getRevision: () => number;
  getSelection: () => { start: number; end: number };
  onChange: (text: string, caret: number) => void;
  disabled?: boolean;
}

/** Mounted per question: a take and its cleanup can never migrate to another answer. */
export function QuestionDictationControls({
  inputRef,
  getText,
  getRevision,
  getSelection,
  onChange,
  disabled,
}: Props) {
  const takeRef = useRef<Take | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const aliveRef = useRef(true);
  const [mode, setMode] = useState("off");
  const [oneStep, setOneStep] = useState(false);
  const [notice, setNotice] = useState("");

  useEffect(() => {
    aliveRef.current = true;
    const refresh = () => {
      void getVoiceInputSettings()
        .then((settings) => {
          if (aliveRef.current) {
            setMode(settings.model.trim() ? settings.mode : "off");
            setOneStep(
              settings.speech_source === "third" && settings.speech_method === "chat_audio",
            );
          }
        })
        .catch(() => {});
    };
    refresh();
    window.addEventListener(VOICE_INPUT_CHANGED_EVENT, refresh);
    return () => {
      aliveRef.current = false;
      window.removeEventListener(VOICE_INPUT_CHANGED_EVENT, refresh);
      abortRef.current?.abort();
    };
  }, []);

  useEffect(() => {
    if (!notice || notice === "Cleaning up voice…") return;
    const timer = window.setTimeout(() => setNotice(""), 4000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  const apply = (take: Take, text: string, undo = false) => {
    if (undo)
      replaceDictatedText(
        inputRef.current,
        take.expected,
        take.before.length,
        take.expected.length - take.after.length,
        text,
      );
    const value = take.before + text + take.after;
    onChange(value, take.before.length + text.length);
    return value;
  };
  const matches = (take: Take) => getText() === take.expected && getRevision() === take.revision;

  const clean = async () => {
    const take = takeRef.current;
    if (!take?.raw || !matches(take) || mode === "off") return;
    abortRef.current?.abort();
    const firstUncleaned = take.segments.findIndex(
      (segment) => !segment.cleaned && (!oneStep || segment.source !== "third"),
    );
    if (firstUncleaned === -1) return;
    const prefix = take.segments
      .slice(0, firstUncleaned)
      .map((segment) => segment.shown)
      .join(" ");
    const suffix = take.segments
      .slice(firstUncleaned)
      .map((segment) => segment.original)
      .join(" ");
    const controller = new AbortController();
    abortRef.current = controller;
    setNotice("Cleaning up voice…");
    try {
      const result = await cleanupDictation(suffix, controller.signal);
      if (!aliveRef.current || controller.signal.aborted) return;
      if (!matches(take)) {
        setNotice("Draft changed; original kept.");
        return;
      }
      if (result.text !== suffix)
        apply(take, [prefix, result.text].filter(Boolean).join(" "), true);
      setNotice("");
    } catch {
      if (aliveRef.current && !controller.signal.aborted)
        setNotice("Cleanup unavailable; original kept.");
    }
  };

  return (
    <div className="flex min-w-0 flex-wrap items-center gap-2">
      <ComposerMicButton
        inputRef={inputRef}
        questionCard
        disabled={disabled}
        className="!size-11"
        onCleanup={(original, text) => {
          const take = takeRef.current;
          if (!aliveRef.current || !take || take.raw !== original || !matches(take)) return;
          take.expected = apply(take, text, true);
          take.revision = getRevision();
        }}
        onVoiceStart={() => {
          abortRef.current?.abort();
          setNotice("");
          const value = getText();
          const { start, end } = getSelection();
          takeRef.current = {
            before: value.slice(0, start),
            after: value.slice(end),
            replaced: value.slice(start, end),
            raw: "",
            segments: [],
            expected: value,
            revision: getRevision(),
          };
        }}
        onTranscript={(text, meta) => {
          const take = takeRef.current;
          if (!aliveRef.current || !take) return;
          if (!matches(take)) {
            setNotice("Draft changed; stop and start a new take to continue.");
            return;
          }
          take.segments.push({
            shown: text,
            original: meta?.raw ?? text,
            cleaned: meta?.cleaned === true || meta?.cleanupHandled === true,
            source: meta?.source,
          });
          const previous = take.raw;
          take.raw += (take.raw ? " " : "") + text;
          if (meta?.cleaned && meta.raw && meta.raw !== text) {
            flushSync(() => {
              take.expected = apply(take, [previous, meta.raw].filter(Boolean).join(" "));
            });
            take.revision = getRevision();
            take.expected = apply(take, take.raw, true);
          } else take.expected = apply(take, take.raw);
          take.revision = getRevision();
        }}
        onVoiceDiscard={() => {
          const take = takeRef.current;
          if (aliveRef.current && take && matches(take)) apply(take, take.replaced);
          takeRef.current = null;
        }}
        onVoiceEnd={() => {
          if (!aliveRef.current || !takeRef.current?.raw) return;
          if (mode !== "off") void clean();
        }}
      />
      <span role="status" className="text-sm text-muted-foreground">
        {notice}
      </span>
    </div>
  );
}
