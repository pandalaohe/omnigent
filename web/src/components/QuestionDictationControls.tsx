import { type RefObject, useEffect, useRef, useState } from "react";
import { ComposerMicButton } from "@/components/ComposerMicButton";

interface Take {
  before: string;
  after: string;
  replaced: string;
  text: string;
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

/** Mounted per question so a finalized take cannot migrate to another answer. */
export function QuestionDictationControls({
  inputRef,
  getText,
  getRevision,
  getSelection,
  onChange,
  disabled,
}: Props) {
  const takeRef = useRef<Take | null>(null);
  const aliveRef = useRef(true);
  const [notice, setNotice] = useState("");

  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);

  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(""), 4000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  const apply = (take: Take, text: string) => {
    const value = take.before + text + take.after;
    onChange(value, take.before.length + text.length);
    return value;
  };
  const matches = (take: Take) => getText() === take.expected && getRevision() === take.revision;

  return (
    <div className="flex min-w-0 flex-wrap items-center gap-2">
      <ComposerMicButton
        inputRef={inputRef}
        questionCard
        disabled={disabled}
        className="!size-7"
        onVoiceStart={() => {
          setNotice("");
          const value = getText();
          const { start, end } = getSelection();
          takeRef.current = {
            before: value.slice(0, start),
            after: value.slice(end),
            replaced: value.slice(start, end),
            text: "",
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
          take.text += (take.text ? " " : "") + text;
          take.expected = apply(take, take.text);
          take.revision = getRevision();
        }}
        onVoiceDiscard={() => {
          const take = takeRef.current;
          if (aliveRef.current && take && matches(take)) apply(take, take.replaced);
          takeRef.current = null;
        }}
        onVoiceEnd={() => {
          const take = takeRef.current;
          if (aliveRef.current && take?.text && matches(take))
            inputRef.current?.focus({ preventScroll: true });
        }}
      />
      <span role="status" className="text-sm text-muted-foreground">
        {notice}
      </span>
    </div>
  );
}
