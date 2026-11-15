import { type RefObject, useEffect, useRef, useState } from "react";
import { ComposerMicButton } from "@/components/ComposerMicButton";
import { Button } from "@/components/ui/button";
import { cleanupDictation, getVoiceInputSettings } from "@/lib/voiceInputApi";

interface Take {
  before: string;
  after: string;
  replaced: string;
  raw: string;
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

function ChangedVoiceText({ original, text }: { original: string; text: string }) {
  let start = 0;
  let end = 0;
  while (start < original.length && start < text.length && original[start] === text[start]) start++;
  while (
    end < original.length - start &&
    end < text.length - start &&
    original[original.length - end - 1] === text[text.length - end - 1]
  )
    end++;
  return (
    <span className="min-w-0 break-words text-sm" aria-label="Changed voice text">
      {text.slice(0, start)}
      <del className="text-muted-foreground">{original.slice(start, original.length - end)}</del>
      <mark>{text.slice(start, text.length - end)}</mark>
      {text.slice(text.length - end)}
    </span>
  );
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
  const [listening, setListening] = useState(false);
  const [busy, setBusy] = useState(false);
  const [mode, setMode] = useState("off");
  const [notice, setNotice] = useState("");
  const [available, setAvailable] = useState(false);
  const [change, setChange] = useState<{
    take: Take;
    text: string;
    expected: string;
    revision: number;
  } | null>(null);

  useEffect(() => {
    aliveRef.current = true;
    void getVoiceInputSettings()
      .then((settings) => {
        if (aliveRef.current) setMode(settings.model.trim() ? settings.mode : "off");
      })
      .catch(() => {});
    return () => {
      aliveRef.current = false;
      abortRef.current?.abort();
    };
  }, []);

  const apply = (take: Take, text: string) => {
    const value = take.before + text + take.after;
    onChange(value, take.before.length + text.length);
    return value;
  };
  const matches = (take: Take) => getText() === take.expected && getRevision() === take.revision;

  const clean = async () => {
    const take = takeRef.current;
    if (!take?.raw || !matches(take) || mode === "off") return;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setBusy(true);
    setNotice("Cleaning up voice…");
    try {
      const result = await cleanupDictation(take.raw, controller.signal);
      if (!aliveRef.current || controller.signal.aborted) return;
      if (!matches(take)) {
        setNotice("Draft changed; original kept.");
        return;
      }
      if (result.text !== take.raw) {
        const expected = apply(take, result.text);
        setChange({ take, text: result.text, expected, revision: getRevision() });
        setNotice("Voice cleaned up");
      } else setNotice("Voice text kept");
      setAvailable(false);
    } catch {
      if (aliveRef.current && !controller.signal.aborted)
        setNotice("Cleanup unavailable; original kept.");
    } finally {
      if (aliveRef.current && abortRef.current === controller) setBusy(false);
    }
  };

  return (
    <div className="flex min-w-0 flex-wrap items-center gap-2">
      <ComposerMicButton
        inputRef={inputRef}
        questionCard
        disabled={disabled}
        className="!size-11"
        onListeningChange={setListening}
        onVoiceStart={() => {
          abortRef.current?.abort();
          setBusy(false);
          setChange(null);
          setAvailable(false);
          setNotice("");
          const value = getText();
          const { start, end } = getSelection();
          takeRef.current = {
            before: value.slice(0, start),
            after: value.slice(end),
            replaced: value.slice(start, end),
            raw: "",
            expected: value,
            revision: getRevision(),
          };
        }}
        onTranscript={(text) => {
          const take = takeRef.current;
          if (!aliveRef.current || !take) return;
          if (!matches(take)) {
            setNotice("Draft changed; stop and start a new take to continue.");
            return;
          }
          take.raw += (take.raw ? " " : "") + text;
          take.expected = apply(take, take.raw);
          take.revision = getRevision();
        }}
        onVoiceDiscard={() => {
          const take = takeRef.current;
          if (aliveRef.current && take && matches(take)) apply(take, take.replaced);
          takeRef.current = null;
          setAvailable(false);
        }}
        onVoiceEnd={() => {
          if (!aliveRef.current || !takeRef.current?.raw) return;
          setAvailable(true);
          if (mode === "automatic") void clean();
        }}
      />
      {mode !== "off" && available && !listening && !change && (
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={busy || disabled}
          onClick={() => void clean()}
        >
          Clean up voice
        </Button>
      )}
      {change && (
        <>
          <ChangedVoiceText original={change.take.raw} text={change.text} />
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={disabled}
            onClick={() => {
              if (getText() !== change.expected || getRevision() !== change.revision) {
                setNotice("Draft changed; restore would overwrite your edits.");
                return;
              }
              apply(change.take, change.take.raw);
              setChange(null);
              setNotice("Original restored");
            }}
          >
            Restore original
          </Button>
        </>
      )}
      <span role="status" className="text-sm text-muted-foreground">
        {notice}
      </span>
    </div>
  );
}
