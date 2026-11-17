"use client";

import { KeyboardShortcutTooltip } from "@/components/KeyboardShortcut";
import { Button } from "@/components/ui/button";
import { showToast } from "@/components/ui/toast";
import { isInsideQuestionCard } from "@/hooks/useQuestionCardHotkeys";
import { useVoiceDictationHotkey, VOICE_DICTATION_KEYS } from "@/hooks/useVoiceDictationHotkey";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import {
  cleanupDictation,
  getVoiceInputSettings,
  VOICE_INPUT_CHANGED_EVENT,
} from "@/lib/voiceInputApi";
import type { FinalMeta } from "@/lib/dictation";
import { DictationBusyError, DictationSession, restoreDictationPunctuation } from "@/lib/dictation";
import { isElectronShell, startSystemDictation, supportsSystemDictation } from "@/lib/nativeBridge";
import { cn } from "@/lib/utils";
import { toast } from "sonner";
import { Loader2Icon, MicIcon, SquareIcon } from "lucide-react";
import { type RefObject, useCallback, useEffect, useRef, useState } from "react";

// Local-only types; speech-input.tsx already augments Window globally.
interface SpeechRecognitionLike {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  start: () => void;
  stop: () => void;
  addEventListener: (type: string, listener: (event: Event) => void) => void;
  removeEventListener: (type: string, listener: (event: Event) => void) => void;
}

interface SpeechRecognitionEventLike extends Event {
  results: {
    readonly length: number;
    [index: number]: {
      readonly length: number;
      [index: number]: { transcript: string };
      isFinal: boolean;
    };
  };
  resultIndex: number;
}

interface SpeechRecognitionErrorEventLike extends Event {
  error: string;
}

type SpeechRecognitionCtor = new () => SpeechRecognitionLike;

const getRecognitionCtor = (): SpeechRecognitionCtor | null => {
  if (typeof window === "undefined") return null;
  const w = window as unknown as {
    SpeechRecognition?: SpeechRecognitionCtor;
    webkitSpeechRecognition?: SpeechRecognitionCtor;
  };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
};

const getDefaultDictationLang = (): string => {
  if (typeof navigator === "undefined") return "en-US";
  try {
    return navigator.language || "en-US";
  } catch {
    return "en-US";
  }
};

// FFT bin ranges per bar, weighted toward voice frequencies (~100Hz–3kHz).
const BAR_BINS: readonly (readonly [number, number])[] = [
  [1, 3],
  [3, 6],
  [6, 10],
  [10, 16],
];

const BAR_BASELINE = 0.2;

// Browsers end a Web Speech take on their own after a pause (Android after
// ~1–2 s, even with `continuous`), so a live take restarts the recognizer until
// the user stops it. It gives up after this long without any recognized speech.
const WEB_SPEECH_IDLE_LIMIT_MS = 60_000;
// A recognizer that ends this soon after starting, with no speech, is failing
// rather than hearing silence; this many in a row ends the take with an error.
const WEB_SPEECH_QUICK_END_MS = 1_000;
const WEB_SPEECH_MAX_QUICK_ENDS = 3;

let activeTake: { owner: symbol; cancel: () => void } | null = null;

export interface ComposerMicButtonProps {
  /** Question-card keys belong to this take while it is active. */
  questionCard?: boolean;
  /** Runs after the complete take (including cleanup) was delivered. */
  onVoiceEnd?: () => void;
  onTranscript: (
    text: string,
    meta?: FinalMeta & { source?: "browser" | "server" | "third" },
  ) => void;
  className?: string;
  /** Clears an older interim insertion on stop; live partials stay buffered. */
  onInterim?: (text: string) => void;
  disabled?: boolean;
  lang?: string;
  /** Bind the global ⌘⌥V dictation hotkey to this mic. Enable on exactly one
   *  mounted mic (the primary composer) so two don't fight for the device. */
  enableHotkey?: boolean;
  /** Fired when dictation begins. The parent should snapshot the composer text
   *  here so {@link onVoiceDiscard} can revert to it. */
  onVoiceStart?: () => void;
  /** Fired when Esc ends dictation. The parent should restore the text it
   *  snapshotted in {@link onVoiceStart}, discarding what was dictated. */
  onVoiceDiscard?: () => void;
  /** Called when a take starts or ends listening; while listening, Enter and Esc
   *  belong to dictation. */
  onListeningChange?: (listening: boolean) => void;
  /** The composer's text box. macOS Dictation types into the focused field, so
   *  the desktop app's macOS fallback focuses this first; without it there is no
   *  fallback. */
  inputRef?: RefObject<HTMLTextAreaElement | null>;
}

/** getUserMedia permission failures, distinct from transport failures. */
const isPermissionError = (error: unknown): boolean =>
  error instanceof DOMException &&
  (error.name === "NotAllowedError" || error.name === "SecurityError");

export const ComposerMicButton = ({
  onTranscript,
  className,
  onInterim,
  disabled,
  lang = getDefaultDictationLang(),
  enableHotkey = false,
  onVoiceStart,
  onVoiceDiscard,
  onListeningChange,
  inputRef,
  questionCard = false,
  onVoiceEnd,
}: ComposerMicButtonProps) => {
  // Web Speech is primary wherever its constructor exists; a take that dies with
  // "network" falls back to the server for that take only (see handleError).
  // Electron has the constructor but never a backend, so there only the server can serve,
  // and on macOS a server without dictation hands the take to macOS Dictation.
  const [Ctor] = useState(getRecognitionCtor);
  const electron = isElectronShell();
  const [systemDictationBridge] = useState(supportsSystemDictation);
  const serverInfo = useServerInfo();
  const [speechSource, setSpeechSource] = useState("browser");
  const cleanupEnabledRef = useRef(false);
  const oneStepRef = useRef(false);
  useEffect(() => {
    let alive = true;
    const refresh = () => {
      void getVoiceInputSettings()
        .then((settings) => {
          if (alive) {
            setSpeechSource(settings.speech_source ?? "browser");
            cleanupEnabledRef.current = settings.mode !== "off" && !!settings.model.trim();
            oneStepRef.current =
              settings.speech_source === "third" && settings.speech_method === "chat_audio";
          }
        })
        .catch(() => {});
    };
    refresh();
    window.addEventListener(VOICE_INPUT_CHANGED_EVENT, refresh);
    return () => {
      alive = false;
      window.removeEventListener(VOICE_INPUT_CHANGED_EVENT, refresh);
    };
  }, []);
  const thirdSelected = speechSource === "third";
  const serverAvailable =
    thirdSelected || (serverInfo !== "loading" && serverInfo.dictation_available);
  const punctuationAvailable =
    serverInfo !== "loading" && serverInfo.dictation_punctuation_available === true;
  const systemDictation =
    electron &&
    systemDictationBridge &&
    inputRef !== undefined &&
    serverInfo !== "loading" &&
    !serverAvailable;
  const canDictate = serverAvailable || (Ctor !== null && !electron) || systemDictation;
  // Mirrored into a ref so the mount-time recognition handlers (closed
  // over [Ctor, lang]) see the current probe result.
  const thirdSelectedRef = useRef(thirdSelected);
  thirdSelectedRef.current = thirdSelected;
  const serverAvailableRef = useRef(serverAvailable);
  serverAvailableRef.current = serverAvailable;
  const [isListening, setIsListening] = useState(false);
  // Server-path only: the take has started but audio isn't flowing yet (the
  // first take cold-loads the model, up to ~40s). Shows a spinner so the click
  // isn't a dead-looking button until it flips to listening.
  const [connecting, setConnecting] = useState(false);
  const [processing, setProcessing] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const recognitionRef = useRef<SpeechRecognitionLike | null>(null);
  const sessionRef = useRef<DictationSession | null>(null);
  const failedSessionRef = useRef<DictationSession | null>(null);
  const retryTakeRef = useRef<() => void>(() => {});
  const failedToastRef = useRef<string | number | undefined>(undefined);
  // Refs so handlers aren't re-attached on every parent re-render.
  const onTranscriptRef = useRef(onTranscript);
  onTranscriptRef.current = onTranscript;
  const segmentsRef = useRef<{ text: string; meta?: FinalMeta; excluded: boolean }[]>([]);
  const serverOneStepRef = useRef(false);
  const cleanupAbortRef = useRef<AbortController | null>(null);
  const bufferTranscript = useCallback((text: string, meta?: FinalMeta, excluded = false) => {
    segmentsRef.current.push({ text, meta, excluded });
  }, []);
  const takeInputRef = useRef<{
    field: HTMLTextAreaElement | null;
    value: string | undefined;
    edited: boolean;
    onEdit: () => void;
  } | null>(null);
  const releaseTakeInput = useCallback(() => {
    const target = takeInputRef.current;
    target?.field?.removeEventListener("input", target.onEdit);
    target?.field?.removeEventListener("change", target.onEdit);
    takeInputRef.current = null;
  }, []);
  const captureTakeInput = useCallback(() => {
    releaseTakeInput();
    const field = inputRef?.current ?? null;
    const target = {
      field,
      value: field?.value,
      edited: false,
      onEdit: () => {
        target.edited = true;
      },
    };
    field?.addEventListener("input", target.onEdit);
    field?.addEventListener("change", target.onEdit);
    takeInputRef.current = target;
  }, [inputRef, releaseTakeInput]);
  const onInterimRef = useRef(onInterim);
  onInterimRef.current = onInterim;
  // Synced refs so the mount-time listeners call the latest callbacks.
  const onVoiceStartRef = useRef(onVoiceStart);
  onVoiceStartRef.current = onVoiceStart;
  const onVoiceDiscardRef = useRef(onVoiceDiscard);
  onVoiceDiscardRef.current = onVoiceDiscard;
  const onListeningChangeRef = useRef(onListeningChange);
  onListeningChangeRef.current = onListeningChange;
  const onVoiceEndRef = useRef(onVoiceEnd);
  onVoiceEndRef.current = onVoiceEnd;
  const ownerRef = useRef(Symbol("dictation"));
  const mountedRef = useRef(true);
  const wasListeningRef = useRef(false);
  useEffect(() => {
    if (!isListening) return;
    onListeningChangeRef.current?.(true);
    return () => onListeningChangeRef.current?.(false);
  }, [isListening]);
  // Set by the Esc handler so late results after a discard don't repopulate the
  // composer the parent just reverted. Cleared on the next start.
  const discardingRef = useRef(false);
  // Synced prop ref so the recognition result handler (closure over the
  // mount-time effect) can drop late events when the composer goes
  // disabled mid-utterance.
  const disabledRef = useRef(disabled);
  disabledRef.current = disabled;
  const punctuationAvailableRef = useRef(punctuationAvailable);
  punctuationAvailableRef.current = punctuationAvailable;
  // Web Speech can emit several final phrases in one continuous take. Restore
  // them serially so a short later request can never overtake a longer earlier
  // one and scramble the composer's word order.
  const takeGenerationRef = useRef(0);
  const punctuationQueueRef = useRef<Promise<void>>(Promise.resolve());
  const enqueueWebTranscriptRef = useRef<(text: string) => void>(() => {});
  enqueueWebTranscriptRef.current = (text: string) => {
    const transcript = text.trim();
    if (!transcript) return;
    const takeGeneration = takeGenerationRef.current;
    if (!punctuationAvailableRef.current) {
      if (!disabledRef.current && !discardingRef.current) {
        bufferTranscript(transcript);
      }
      return;
    }
    const deliver = async () => {
      let displayText = transcript;
      try {
        displayText = await restoreDictationPunctuation(transcript);
      } catch {
        // Punctuation is an enhancement. Preserve recognized words when the
        // model is cold, unavailable, or the request times out.
      }
      if (
        takeGeneration === takeGenerationRef.current &&
        !disabledRef.current &&
        !discardingRef.current
      ) {
        bufferTranscript(displayText);
      }
    };
    punctuationQueueRef.current = punctuationQueueRef.current.then(deliver, deliver);
  };
  const enqueueServerTranscriptRef = useRef<
    (text: string, ordered: boolean, meta?: FinalMeta) => void
  >(() => {});
  enqueueServerTranscriptRef.current = (text: string, ordered: boolean, meta?: FinalMeta) => {
    const transcript = text.trim();
    if (!transcript) return;
    const takeGeneration = takeGenerationRef.current;
    const excluded = serverOneStepRef.current;
    const deliver = () => {
      if (
        takeGeneration === takeGenerationRef.current &&
        (!disabledRef.current || thirdSelectedRef.current) &&
        !discardingRef.current
      ) {
        bufferTranscript(transcript, meta, excluded);
      }
    };
    if (ordered) {
      punctuationQueueRef.current = punctuationQueueRef.current.then(deliver, deliver);
    } else {
      deliver();
    }
  };
  // Click guard: true between toggle() and the matching start/end event.
  // Prevents rapid double-clicks from calling recognition.start() twice,
  // which throws InvalidStateError in Chrome.
  const transitionRef = useRef(false);
  // Server-take guard, deliberately separate from transitionRef: a failed
  // Web Speech attempt fires a late "end" event that resets transitionRef,
  // which must not unlock a second server take mid-handshake.
  const serverBusyRef = useRef(false);
  // Lets the mount-time Web Speech error handler start the fallback take
  // without closing over toggleServer's identity.
  const toggleServerRef = useRef<(continueTake?: boolean) => Promise<void>>(async () => {});
  const serverContinuesWebTakeRef = useRef(false);
  // Latest server partial for the current take, so a crash can preserve what
  // was spoken instead of blanking it. Set on each partial, cleared on final,
  // and reset when a take starts so a stale partial can't resurrect.
  const interimRef = useRef("");
  // Web Speech take the user has started and not stopped. While set, a
  // recognizer that ends on its own is restarted rather than ending the take.
  const webSpeechTakeRef = useRef<{
    lastSpeechAt: number;
    recognizerStartedAt: number;
    quickEnds: number;
    restarting: boolean;
    started: boolean;
  } | null>(null);

  const cancelTake = useCallback(() => {
    releaseTakeInput();
    discardingRef.current = true;
    takeGenerationRef.current += 1;
    punctuationQueueRef.current = Promise.resolve();
    segmentsRef.current = [];
    cleanupAbortRef.current?.abort();
    webSpeechTakeRef.current = null;
    setProcessing(false);
    setRetrying(false);
    sessionRef.current?.cancel();
    sessionRef.current = null;
    serverBusyRef.current = false;
    try {
      recognitionRef.current?.stop();
    } catch {
      /* Already stopped. */
    }
    setIsListening(false);
  }, [releaseTakeInput]);

  const completeTake = useCallback(() => {
    const generation = takeGenerationRef.current;
    if (!discardingRef.current) setProcessing(true);
    const completion = punctuationQueueRef.current.then(async () => {
      if (
        mountedRef.current &&
        !discardingRef.current &&
        generation === takeGenerationRef.current
      ) {
        const segments = segmentsRef.current;
        segmentsRef.current = [];
        const text = segments.map((segment) => segment.text).join(" ");
        const raw = segments.map((segment) => segment.meta?.raw ?? segment.text).join(" ");
        let output = text;
        let cleaned = segments.length > 0 && segments.every((segment) => segment.meta?.cleaned);
        const first = segments.findIndex((segment) => !segment.excluded && !segment.meta?.cleaned);
        if (text && cleanupEnabledRef.current && first !== -1) {
          const prefix = segments
            .slice(0, first)
            .map((segment) => segment.text)
            .join(" ");
          const suffix = segments
            .slice(first)
            .map((segment) => segment.meta?.raw ?? segment.text)
            .join(" ");
          const controller = new AbortController();
          cleanupAbortRef.current = controller;
          try {
            const result = await cleanupDictation(suffix, controller.signal);
            output = [prefix, result.text].filter(Boolean).join(" ");
            cleaned = true;
          } catch {
            if (!controller.signal.aborted) showToast("Cleanup unavailable; original kept.");
          }
        }
        if (
          !mountedRef.current ||
          discardingRef.current ||
          (disabledRef.current && !thirdSelectedRef.current) ||
          generation !== takeGenerationRef.current ||
          (takeInputRef.current &&
            (takeInputRef.current.edited ||
              takeInputRef.current.field !== (inputRef?.current ?? null) ||
              takeInputRef.current.value !== inputRef?.current?.value))
        )
          return;
        if (output) {
          // Cleanup is owned here for every mic; insertion adapters must not retry it.
          if (cleaned || cleanupEnabledRef.current)
            onTranscriptRef.current(output, { raw, cleaned, cleanupHandled: true });
          else onTranscriptRef.current(output);
        }
        onVoiceEndRef.current?.();
      }
    });
    void completion.finally(() => {
      if (generation === takeGenerationRef.current) {
        releaseTakeInput();
        if (activeTake?.owner === ownerRef.current) activeTake = null;
        if (mountedRef.current) setProcessing(false);
      }
    });
  }, [inputRef, releaseTakeInput]);

  useEffect(() => {
    if (isListening) {
      wasListeningRef.current = true;
      return;
    }
    if (connecting || !wasListeningRef.current) return;
    wasListeningRef.current = false;
    completeTake();
  }, [isListening, connecting, completeTake]);

  // Surface a failure both in the button (tooltip + red tint) and as a toast,
  // so a failed take is an explained error rather than a silent flash back to
  // the idle mic. Stable identity: only setError/showToast, both stable.
  const reportError = useCallback((message: string) => {
    setError(message);
    showToast(message);
  }, []);

  const reportRetainedFailure = useCallback((message: string) => {
    setError(message);
    if (failedToastRef.current !== undefined) toast.dismiss(failedToastRef.current);
    failedToastRef.current = showToast(
      <span>
        {message}{" "}
        <Button
          variant="link"
          size="sm"
          onClick={() => {
            if (failedToastRef.current !== undefined) toast.dismiss(failedToastRef.current);
            failedToastRef.current = undefined;
            retryTakeRef.current();
          }}
        >
          Retry
        </Button>
      </span>,
      { duration: 0 },
    );
  }, []);

  // Written via .style.transform from rAF — avoids 60Hz React re-renders.
  const barRefs = useRef<(HTMLSpanElement | null)[]>(BAR_BINS.map(() => null));

  useEffect(() => {
    // Electron never starts a Web Speech take, so skip the dead recognizer.
    if (!Ctor || electron) return;

    const recognition = new Ctor();
    // Ask for one long take; handleEnd restarts browsers that end it anyway.
    recognition.continuous = true;
    recognition.interimResults = false;
    recognition.lang = lang;

    // A dead recognizer keeps firing start/end/error after the take has
    // fallen back to the server; those stale events must not clobber the
    // server session's isListening/transition state.
    const serverTakeOwnsState = () => sessionRef.current !== null || serverBusyRef.current;

    const handleStart = () => {
      if (serverTakeOwnsState()) return;
      transitionRef.current = false;
      const take = webSpeechTakeRef.current;
      // No take: the user stopped while a restart was starting; end follows.
      if (!take) return;
      take.started = true;
      if (take.restarting) {
        // A restart continues the same take: keep the Esc snapshot and state.
        take.restarting = false;
        return;
      }
      discardingRef.current = false;
      setProcessing(false);
      takeGenerationRef.current += 1;
      punctuationQueueRef.current = Promise.resolve();
      segmentsRef.current = [];
      cleanupAbortRef.current?.abort();
      setError(null);
      setIsListening(true);
      // Snapshot point: let the parent record the text so Esc can revert to it.
      onVoiceStartRef.current?.();
      captureTakeInput();
    };
    // Restart the recognizer when it ends on its own mid-take. Returns false
    // when the take should end instead.
    const restartTake = (): boolean => {
      const take = webSpeechTakeRef.current;
      // Only a take that reached "start" is listening; a hidden page can't
      // reacquire the mic, so let the take end rather than fail noisily.
      if (!take?.started || disabledRef.current || discardingRef.current) return false;
      if (document.hidden) return false;
      const now = Date.now();
      if (now - take.lastSpeechAt >= WEB_SPEECH_IDLE_LIMIT_MS) return false;
      const heardNothing = take.lastSpeechAt <= take.recognizerStartedAt;
      if (heardNothing && now - take.recognizerStartedAt < WEB_SPEECH_QUICK_END_MS) {
        take.quickEnds += 1;
        if (take.quickEnds >= WEB_SPEECH_MAX_QUICK_ENDS) {
          reportError("Voice input stopped unexpectedly. Please try again.");
          return false;
        }
      } else {
        take.quickEnds = 0;
      }
      take.restarting = true;
      take.recognizerStartedAt = now;
      try {
        recognition.start();
      } catch {
        return false;
      }
      return true;
    };
    const handleEnd = () => {
      if (serverTakeOwnsState()) return;
      transitionRef.current = false;
      if (restartTake()) return;
      webSpeechTakeRef.current = null;
      setIsListening(false);
    };
    const handleError = (event: Event) => {
      if (serverTakeOwnsState()) return;
      transitionRef.current = false;
      const err = (event as SpeechRecognitionErrorEventLike).error;
      // "no-speech" is the browser's silence timeout: the end event that
      // follows restarts a live take. Any other error ends it.
      if (err === "no-speech" && webSpeechTakeRef.current) return;
      webSpeechTakeRef.current = null;
      // An unavailable or blocked speech service can still use client mic
      // capture with server transcription. getUserMedia checks mic permission.
      if (
        (err === "network" || err === "service-not-allowed") &&
        serverAvailableRef.current &&
        !thirdSelectedRef.current &&
        !disabledRef.current &&
        !discardingRef.current
      ) {
        setIsListening(false);
        void toggleServerRef.current(true);
        return;
      }
      // "no-speech" / "aborted" are routine (silence timeout, user stop).
      if (err === "not-allowed") {
        reportError("Microphone access denied. Allow access and try again.");
      } else if (err && err !== "no-speech" && err !== "aborted") {
        reportError("Voice input isn't available on this device.");
      }
      setIsListening(false);
    };
    const handleResult = (event: Event) => {
      // Drop late events that arrive after the composer went disabled, or after
      // an Esc discard the parent has already reverted.
      if (disabledRef.current || discardingRef.current) return;
      const speechEvent = event as SpeechRecognitionEventLike;
      let finalTranscript = "";
      for (let i = speechEvent.resultIndex; i < speechEvent.results.length; i += 1) {
        const result = speechEvent.results[i];
        if (result.isFinal) {
          finalTranscript += result[0]?.transcript ?? "";
        }
      }
      if (finalTranscript.trim() && webSpeechTakeRef.current)
        webSpeechTakeRef.current.lastSpeechAt = Date.now();
      enqueueWebTranscriptRef.current(finalTranscript);
    };

    recognition.addEventListener("start", handleStart);
    recognition.addEventListener("end", handleEnd);
    recognition.addEventListener("error", handleError);
    recognition.addEventListener("result", handleResult);
    recognitionRef.current = recognition;

    return () => {
      recognition.removeEventListener("start", handleStart);
      recognition.removeEventListener("end", handleEnd);
      recognition.removeEventListener("error", handleError);
      recognition.removeEventListener("result", handleResult);
      webSpeechTakeRef.current = null;
      recognition.stop();
      recognitionRef.current = null;
    };
  }, [Ctor, electron, lang, reportError, captureTakeInput]);

  // Auto-stop if the composer goes disabled mid-dictation. Stops the
  // recognizer; the disabledRef guard in handleResult catches any final
  // events still queued before the end event fires. A server session is
  // cancelled outright (no tail flush) — the take is moot once the
  // composer can't accept text.
  useEffect(() => {
    if (!disabled || (thirdSelectedRef.current && sessionRef.current)) return;
    releaseTakeInput();
    takeGenerationRef.current += 1;
    punctuationQueueRef.current = Promise.resolve();
    cleanupAbortRef.current?.abort();
    segmentsRef.current = [];
    setProcessing(false);
    if (!isListening) return;
    if (sessionRef.current) {
      sessionRef.current.cancel();
      sessionRef.current = null;
      setIsListening(false);
      onInterimRef.current?.("");
      return;
    }
    webSpeechTakeRef.current = null;
    try {
      recognitionRef.current?.stop();
    } catch {
      // .stop() on an already-stopped recognizer can throw in some
      // browsers; safe to ignore — the end event will reconcile state.
    }
  }, [disabled, isListening, releaseTakeInput]);

  // Release the mic if the component unmounts mid-take (e.g. the
  // new-chat dialog closes while dictating).
  useEffect(() => {
    mountedRef.current = true;
    const owner = ownerRef.current;
    return () => {
      mountedRef.current = false;
      releaseTakeInput();
      discardingRef.current = true;
      if (activeTake?.owner === owner) activeTake = null;
      takeGenerationRef.current += 1;
      punctuationQueueRef.current = Promise.resolve();
      segmentsRef.current = [];
      cleanupAbortRef.current?.abort();
      sessionRef.current?.cancel();
      sessionRef.current = null;
    };
  }, [releaseTakeInput]);

  // Second getUserMedia stream just for visualization — Web Speech API
  // hides its audio buffer. Chrome batches the permission to one prompt.
  useEffect(() => {
    if (!isListening) return;
    let cancelled = false;
    let stream: MediaStream | null = null;
    let audioCtx: AudioContext | null = null;
    let rafId: number | null = null;
    // Snapshot so cleanup doesn't read a stale .current (exhaustive-deps).
    const bars = barRefs.current;

    const start = async () => {
      try {
        stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      } catch {
        // Mic permission denied just for the visualization stream — leave
        // the bars at baseline. The speech recognition error handler will
        // surface the user-facing message if it also fails.
        return;
      }
      if (cancelled) {
        for (const track of stream.getTracks()) track.stop();
        return;
      }
      audioCtx = new AudioContext();
      const source = audioCtx.createMediaStreamSource(stream);
      const analyser = audioCtx.createAnalyser();
      analyser.fftSize = 64;
      // Built-in temporal smoothing so bars don't jitter frame-to-frame.
      analyser.smoothingTimeConstant = 0.75;
      source.connect(analyser);
      const data = new Uint8Array(analyser.frequencyBinCount);

      const tick = () => {
        analyser.getByteFrequencyData(data);
        for (let i = 0; i < BAR_BINS.length; i += 1) {
          const [lo, hi] = BAR_BINS[i];
          let sum = 0;
          for (let j = lo; j < hi; j += 1) sum += data[j];
          const avg = sum / (hi - lo) / 255;
          // 1.6× headroom for quiet speech; clamp at 1 to fit the button.
          const scale = Math.max(BAR_BASELINE, Math.min(1, avg * 1.6));
          const el = bars[i];
          if (el) el.style.transform = `scaleY(${scale})`;
        }
        rafId = requestAnimationFrame(tick);
      };
      rafId = requestAnimationFrame(tick);
    };

    start();

    return () => {
      cancelled = true;
      if (rafId !== null) cancelAnimationFrame(rafId);
      if (stream) {
        for (const track of stream.getTracks()) track.stop();
      }
      if (audioCtx && audioCtx.state !== "closed") {
        audioCtx.close();
      }
      // Reset for the next session.
      for (const el of bars) {
        if (el) el.style.transform = `scaleY(${BAR_BASELINE})`;
      }
    };
  }, [isListening]);

  // Server-dictation toggle. Start resolves only once the mic + socket
  // handshake are up, so isListening flips exactly when audio flows.
  const toggleServer = useCallback(
    async (continueTake = false) => {
      if (serverBusyRef.current) return;
      serverBusyRef.current = true;
      const session = sessionRef.current;
      if (session) {
        setProcessing(true);
        const generation = takeGenerationRef.current;
        const orderedWithWebTake = serverContinuesWebTakeRef.current;
        serverContinuesWebTakeRef.current = false;
        let stopped: Awaited<ReturnType<DictationSession["stop"]>>;
        try {
          stopped = await session.stop();
        } catch {
          session.cancel();
          reportError("Voice input failed. Please try again.");
          stopped = { text: "" };
        }
        const tail = (typeof stopped === "string" ? stopped : stopped.text).trim();
        if (
          (!disabledRef.current || thirdSelectedRef.current) &&
          !discardingRef.current &&
          generation === takeGenerationRef.current &&
          mountedRef.current
        ) {
          // A non-empty tail supersedes the pending interim via
          // onTranscript; an empty one just clears the interim region.
          if (tail)
            enqueueServerTranscriptRef.current(
              tail,
              orderedWithWebTake,
              typeof stopped === "string" ? undefined : stopped,
            );
          else onInterimRef.current?.("");
        }
        if (generation !== takeGenerationRef.current) return;
        if (typeof stopped !== "string" && stopped.undelivered?.segments) {
          failedSessionRef.current = session;
          const message = `${stopped.undelivered.segments} phrase(s) (~${stopped.undelivered.seconds} s) were not transcribed.`;
          reportRetainedFailure(message);
        }
        sessionRef.current = null;
        setIsListening(false);
        serverBusyRef.current = false;
        return;
      }
      try {
        discardingRef.current = false;
        failedSessionRef.current = null;
        setProcessing(false);
        interimRef.current = "";
        setConnecting(true);
        if (!continueTake) {
          // Snapshot point: let the parent record the text so Esc can revert to it.
          takeGenerationRef.current += 1;
          punctuationQueueRef.current = Promise.resolve();
          segmentsRef.current = [];
          cleanupAbortRef.current?.abort();
          onVoiceStartRef.current?.();
          captureTakeInput();
        }
        serverContinuesWebTakeRef.current = continueTake;
        serverOneStepRef.current = oneStepRef.current;
        const generation = takeGenerationRef.current;
        const next = await DictationSession.start({
          onPartial: (text) => {
            // Drop late partials after an Esc discard — they'd repopulate the
            // composer the parent just reverted.
            if (
              generation === takeGenerationRef.current &&
              !disabledRef.current &&
              !discardingRef.current
            ) {
              interimRef.current = text;
            }
          },
          onFinal: (text, meta) => {
            if (generation !== takeGenerationRef.current || !mountedRef.current) return;
            interimRef.current = "";
            enqueueServerTranscriptRef.current(text, continueTake, meta);
          },
          onError: (_message, meta) => {
            if (generation !== takeGenerationRef.current || !mountedRef.current) return;
            if (thirdSelected && sessionRef.current && !serverBusyRef.current) {
              reportError("Recognition connection failed. Stop to retry the recorded audio.");
              return;
            }
            sessionRef.current = null;
            serverContinuesWebTakeRef.current = false;
            // Preserve anything spoken but not yet finalized: pin the pending
            // partial as a final rather than blanking it, so a crash mid-take
            // doesn't discard the user's words.
            const pending = interimRef.current.trim();
            interimRef.current = "";
            if (pending && !disabledRef.current && !discardingRef.current) {
              bufferTranscript(pending, undefined, serverOneStepRef.current);
            } else {
              onInterimRef.current?.("");
            }
            setIsListening(false);
            const lost = meta?.undelivered;
            const suffix = lost?.segments
              ? ` ${lost.segments} phrase(s) (~${lost.seconds} s) were not transcribed.`
              : "";
            reportError(
              (thirdSelected
                ? "Third-party recognition failed."
                : "Voice input failed. Please try again.") + suffix,
            );
          },
        });
        if (
          !mountedRef.current ||
          disabledRef.current ||
          discardingRef.current ||
          generation !== takeGenerationRef.current
        ) {
          next.cancel();
          setConnecting(false);
          serverBusyRef.current = false;
          return;
        }
        sessionRef.current = next;
        setError(null);
        setIsListening(true);
      } catch (startError) {
        serverContinuesWebTakeRef.current = false;
        setIsListening(false);
        reportError(
          startError instanceof DictationBusyError
            ? "Voice input is busy. Please try again shortly."
            : isPermissionError(startError)
              ? "Microphone access denied. Allow access and try again."
              : thirdSelected
                ? "Third-party recognition failed."
                : "Voice input isn't available on this device.",
        );
      }
      // Reached only by the start path (the stop branch returns earlier), so this
      // clears the handshake spinner on both success and failure.
      setConnecting(false);
      serverBusyRef.current = false;
    },
    [reportError, reportRetainedFailure, thirdSelected, bufferTranscript, captureTakeInput],
  );
  toggleServerRef.current = toggleServer;
  retryTakeRef.current = () => {
    const session = failedSessionRef.current;
    if (!session || processing || isListening || disabledRef.current) return;
    const generation = ++takeGenerationRef.current;
    activeTake = { owner: ownerRef.current, cancel: cancelTake };
    onVoiceStartRef.current?.();
    discardingRef.current = false;
    punctuationQueueRef.current = Promise.resolve();
    segmentsRef.current = [];
    captureTakeInput();
    sessionRef.current = session;
    serverBusyRef.current = true;
    setProcessing(true);
    setRetrying(true);
    void session
      .retry()
      .then((tail) => {
        if (generation !== takeGenerationRef.current || !mountedRef.current) return;
        if (tail.text) enqueueServerTranscriptRef.current(tail.text, false, tail);
        if (tail.undelivered?.segments) reportRetainedFailure("Recognition failed. Audio is kept.");
        else {
          failedSessionRef.current = null;
          setError(null);
        }
      })
      .catch(() => {
        if (generation === takeGenerationRef.current)
          reportRetainedFailure("Recognition failed. Audio is kept.");
      })
      .finally(() => {
        if (generation !== takeGenerationRef.current) return;
        sessionRef.current = null;
        serverBusyRef.current = false;
        setRetrying(false);
        completeTake();
      });
  };

  // macOS owns this take: its own indicator shows listening, and its text
  // reaches the composer as typing, so none of the take state above is used.
  const startMacDictation = useCallback(() => {
    const input = inputRef?.current;
    if (!input) return;
    input.focus();
    void startSystemDictation().then((started) => {
      if (started) setError(null);
      else reportError("Voice input isn't available on this device.");
    });
  }, [inputRef, reportError]);

  const toggle = useCallback(() => {
    if (processing) {
      cancelTake();
      return;
    }
    if (systemDictation && !serverAvailable) {
      activeTake?.cancel();
      activeTake = null;
      if (!questionCard) startMacDictation();
      return;
    }
    if (!isListening && !serverBusyRef.current && !transitionRef.current) {
      DictationSession.discardRetainedAudio();
      failedSessionRef.current = null;
      if (failedToastRef.current !== undefined) toast.dismiss(failedToastRef.current);
      failedToastRef.current = undefined;
      if (activeTake?.owner !== ownerRef.current) activeTake?.cancel();
      activeTake = { owner: ownerRef.current, cancel: cancelTake };
    }
    // An active (or starting) server take is owned by the server path,
    // whichever mode started it.
    if (sessionRef.current || serverBusyRef.current) {
      void toggleServer();
      return;
    }
    if (!isListening && serverAvailable && speechSource !== "browser") {
      void toggleServer();
      return;
    }
    // Electron's SpeechRecognition has no backend, so a Web Speech take there
    // always fails: go straight to the server, else to macOS Dictation.
    if (!Ctor || electron) {
      if (serverAvailable) void toggleServer();
      else if (systemDictation) startMacDictation();
      return;
    }
    // Guard against rapid clicks landing before start/end event fires.
    if (transitionRef.current) return;
    const recognition = recognitionRef.current;
    if (!recognition) return;
    transitionRef.current = true;
    try {
      if (isListening) {
        setProcessing(true);
        webSpeechTakeRef.current = null;
        recognition.stop();
      } else {
        setProcessing(false);
        const now = Date.now();
        webSpeechTakeRef.current = {
          lastSpeechAt: now,
          recognizerStartedAt: now,
          quickEnds: 0,
          restarting: false,
          started: false,
        };
        takeGenerationRef.current += 1;
        punctuationQueueRef.current = Promise.resolve();
        cleanupAbortRef.current?.abort();
        segmentsRef.current = [];
        recognition.start();
      }
    } catch {
      // InvalidStateError from a double-call — drop the guard so the
      // user can try again, and let the next event reconcile state.
      transitionRef.current = false;
      setProcessing(false);
      if (!isListening) webSpeechTakeRef.current = null;
    }
  }, [
    isListening,
    processing,
    Ctor,
    electron,
    serverAvailable,
    systemDictation,
    startMacDictation,
    toggleServer,
    cancelTake,
    questionCard,
    speechSource,
  ]);

  // ⌘⌥V toggles dictation from anywhere — same as clicking the button. Enabled
  // only while a dictation path can run and the composer isn't disabled, so
  // the chord is inert when it can't do anything.
  const hotkeyEnabled = enableHotkey && canDictate && !disabled;
  useVoiceDictationHotkey(toggle, hotkeyEnabled);

  // While listening, Enter commits (end the take, keep the text) and Esc
  // cancels (end the take, discard back to the pre-dictation snapshot). Bound in
  // the capture phase so it preempts the composer's own Enter-sends / Esc-stops.
  // Path-aware: a live server take is torn down via the DictationSession, a Web
  // Speech take via the recognizer.
  useEffect(() => {
    if (!isListening && !retrying) return;
    const handler = (e: globalThis.KeyboardEvent): void => {
      // A focused question card owns Enter and Esc; the take keeps running in
      // the composer while the card decides what the key means.
      if (!questionCard && isInsideQuestionCard(e.target)) return;
      if (e.repeat || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === "Enter" && !e.shiftKey) {
        // Commit: end the take and keep the text. toggle() routes to the right
        // path — Web Speech stop, or a server stop that flushes the tail.
        e.preventDefault();
        e.stopPropagation();
        toggle();
      } else if (e.key === "Escape") {
        // Cancel: flag the discard so trailing results are dropped, revert the
        // composer, then tear the take down immediately (no tail flush).
        e.preventDefault();
        e.stopPropagation();
        if (retrying) {
          cancelTake();
          onVoiceDiscardRef.current?.();
          return;
        }
        discardingRef.current = true;
        takeGenerationRef.current += 1;
        punctuationQueueRef.current = Promise.resolve();
        onVoiceDiscardRef.current?.();
        const session = sessionRef.current;
        if (session) {
          sessionRef.current = null;
          serverBusyRef.current = false;
          session.cancel();
          setIsListening(false);
        } else {
          webSpeechTakeRef.current = null;
          try {
            recognitionRef.current?.stop();
          } catch {
            // Already stopping — the end event will reconcile state.
          }
        }
      }
    };
    window.addEventListener("keydown", handler, true);
    return () => window.removeEventListener("keydown", handler, true);
  }, [isListening, retrying, toggle, questionCard, cancelTake]);

  if (questionCard && systemDictation) {
    return (
      <span role="note" className="text-sm text-muted-foreground">
        Use system dictation from your keyboard; automatic cleanup is unavailable for this input.
      </span>
    );
  }
  if (!canDictate) return null;

  // Stable accessible name with aria-pressed signals toggle state to
  // screen readers. Error text takes over the tooltip when set.
  const a11yLabel = "Voice dictation";
  const tooltip =
    error ??
    (processing
      ? "Processing voice input… Click to cancel."
      : connecting
        ? "Starting voice input…"
        : a11yLabel);

  return (
    <KeyboardShortcutTooltip
      label={tooltip}
      keys={hotkeyEnabled ? VOICE_DICTATION_KEYS : undefined}
    >
      <Button
        type="button"
        size="icon"
        variant="ghost"
        disabled={disabled && !(thirdSelected && (isListening || processing))}
        onClick={toggle}
        aria-pressed={isListening}
        // While listening, Escape cancels dictation before page-wide shortcuts see it.
        data-escape-owner={isListening ? "" : undefined}
        aria-busy={connecting || processing}
        aria-label={a11yLabel}
        data-composer-action="microphone"
        data-server-dictation-available={serverAvailable}
        data-dictating={isListening || undefined}
        className={cn(
          "size-9 mobile-touch-target md:size-8",
          isListening &&
            "bg-muted/60 text-foreground hover:bg-destructive/10 hover:text-destructive focus-visible:bg-destructive/10 focus-visible:text-destructive",
          error && "text-destructive",
          className,
        )}
      >
        {connecting || processing ? (
          <Loader2Icon
            className="size-4 animate-spin motion-reduce:animate-none"
            data-icon-size="16"
            aria-hidden
          />
        ) : isListening ? (
          // Bars fade out and stop icon fades in on hover OR keyboard focus,
          // so keyboard users get the stop affordance without needing hover.
          <span className="relative flex size-4 items-center justify-center" aria-hidden>
            <span className="flex h-full items-center gap-[2px] transition-opacity group-hover/button:opacity-0 group-focus-visible/button:opacity-0">
              {BAR_BINS.map(([lo, hi], i) => (
                <span
                  key={`${lo}-${hi}`}
                  ref={(el) => {
                    barRefs.current[i] = el;
                  }}
                  className="block h-3 w-[2px] origin-center rounded-full bg-current"
                  style={{ transform: `scaleY(${BAR_BASELINE})` }}
                />
              ))}
            </span>
            <SquareIcon className="absolute size-3 fill-current opacity-0 transition-opacity group-hover/button:opacity-100 group-focus-visible/button:opacity-100" />
          </span>
        ) : (
          <MicIcon className="size-4" data-icon-size="16" />
        )}
      </Button>
    </KeyboardShortcutTooltip>
  );
};
