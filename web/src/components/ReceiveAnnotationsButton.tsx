import { useEffect, useRef, useState } from "react";
import { CircleDotIcon } from "lucide-react";
import {
  BrowserAnnotationsApiError,
  getAnnotationReceiver,
  setAnnotationReceiver,
  type AnnotationReceiver,
} from "@/lib/browserAnnotationsApi";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { useViewerId } from "@/hooks/useViewerId";
import { getCurrentUserId } from "@/lib/identity";
import { getOmnigentHostGeneration, getOmnigentServerIdentity } from "@/lib/host";

interface Props {
  sessionId: string;
  archived: boolean;
}

export function ReceiveAnnotationsButton({ sessionId, archived }: Props) {
  useViewerId();
  const scope = JSON.stringify([
    sessionId,
    getOmnigentServerIdentity(),
    getOmnigentHostGeneration(),
    getCurrentUserId(),
  ]);
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  const lifecycle = useRef(0);
  const loadSequence = useRef(0);
  const [receiver, setReceiver] = useState<AnnotationReceiver | null>(null);
  const [loadedScope, setLoadedScope] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    const generation = ++lifecycle.current;
    const current = () =>
      active &&
      lifecycle.current === generation &&
      scopeRef.current === scope &&
      scope ===
        JSON.stringify([
          sessionId,
          getOmnigentServerIdentity(),
          getOmnigentHostGeneration(),
          getCurrentUserId(),
        ]);
    const load = async () => {
      if (!current() || getCurrentUserId() === null) return;
      const sequence = ++loadSequence.current;
      try {
        const state = await getAnnotationReceiver(sessionId);
        if (current() && sequence === loadSequence.current) {
          setReceiver(state);
          setLoadedScope(scope);
          setError(null);
        }
      } catch {
        if (current() && sequence === loadSequence.current) {
          setReceiver(null);
          setLoadedScope(null);
          setError("Could not check annotation receiving status.");
        }
      }
    };
    setReceiver(null);
    setLoadedScope(null);
    setBusy(false);
    setError(null);
    void load();
    const onFocus = () => void load();
    window.addEventListener("focus", onFocus);
    window.addEventListener("omnigent:annotation-receiver-changed", onFocus);
    return () => {
      active = false;
      lifecycle.current += 1;
      window.removeEventListener("focus", onFocus);
      window.removeEventListener("omnigent:annotation-receiver-changed", onFocus);
    };
  }, [scope, sessionId]);

  const scopedReceiver = loadedScope === scope ? receiver : null;
  const pressed = scopedReceiver?.session_id === sessionId;
  const disabledReason = archived
    ? "Archived sessions cannot receive web annotations."
    : scopedReceiver?.target_editable === false
      ? "You need an explicit edit grant for this session."
      : !scopedReceiver || getCurrentUserId() === null
        ? "Checking annotation receiving status."
        : null;

  const toggle = async () => {
    if (!scopedReceiver || disabledReason || busy) return;
    const generation = lifecycle.current;
    const current = () =>
      lifecycle.current === generation &&
      scopeRef.current === scope &&
      scope ===
        JSON.stringify([
          sessionId,
          getOmnigentServerIdentity(),
          getOmnigentHostGeneration(),
          getCurrentUserId(),
        ]);
    if (!current()) return;
    loadSequence.current += 1;
    setBusy(true);
    setError(null);
    try {
      const updated = await setAnnotationReceiver(
        pressed ? null : sessionId,
        scopedReceiver.revision,
      );
      if (!current()) return;
      setReceiver(updated);
      setLoadedScope(scope);
      window.dispatchEvent(new Event("omnigent:annotation-receiver-changed"));
    } catch (cause) {
      if (!current()) return;
      setError(
        cause instanceof BrowserAnnotationsApiError && cause.status === 409
          ? "Receiving session changed elsewhere. Status refreshed."
          : "Could not change annotation receiving status.",
      );
      const sequence = ++loadSequence.current;
      try {
        const state = await getAnnotationReceiver(sessionId);
        if (current() && sequence === loadSequence.current) {
          setReceiver(state);
          setLoadedScope(scope);
        }
      } catch {
        if (current() && sequence === loadSequence.current) {
          setReceiver(null);
          setLoadedScope(null);
        }
      }
    } finally {
      if (current()) setBusy(false);
    }
  };

  return (
    <div className="flex min-w-0 items-center gap-1">
      <Button
        type="button"
        variant="ghost"
        size="sm"
        aria-label={pressed ? "Receiving web annotations" : "Receive web annotations"}
        aria-pressed={pressed}
        title={
          disabledReason ?? (pressed ? "Stop receiving web annotations" : "Receive web annotations")
        }
        disabled={busy || disabledReason !== null}
        onClick={() => void toggle()}
        className={cn(
          "h-7 shrink-0 gap-1 rounded-md px-2 text-xs",
          pressed ? "text-primary" : "text-muted-foreground",
        )}
      >
        <CircleDotIcon className="size-3.5" />
        <span className="max-md:hidden">
          {pressed ? "Receiving web annotations" : "Receive web annotations"}
        </span>
        <span className="md:hidden">{pressed ? "Receiving" : "Receive"}</span>
      </Button>
      {error && (
        <span role="alert" className="max-w-36 truncate text-xs text-destructive" title={error}>
          {error}
        </span>
      )}
    </div>
  );
}
