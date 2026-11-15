import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";

export function FilePathBar({
  path,
  absolutePath,
  validate,
  onNavigate,
}: {
  path: string;
  absolutePath: string;
  validate: (path: string, signal: AbortSignal) => Promise<unknown>;
  onNavigate?: (path: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(absolutePath);
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const request = useRef<AbortController | null>(null);
  const errorId = useId();

  useEffect(() => () => request.current?.abort(), []);
  useLayoutEffect(() => {
    if (editing) {
      input.current?.focus();
      input.current?.select();
    }
  }, [editing]);

  const cancel = () => {
    request.current?.abort();
    setEditing(false);
    setPending(false);
    setError("");
    setDraft(absolutePath);
  };

  return (
    <div className="min-w-0 flex-1">
      {editing ? (
        <form
          onSubmit={async (event) => {
            event.preventDefault();
            if (pending) return;
            const destination = draft.trim();
            if (!destination || !onNavigate) {
              setError(destination ? "File navigation unavailable" : "Enter a file path");
              return;
            }
            if (/^https?:\/\//i.test(destination)) {
              setError("Enter a file path; open web addresses in a browser tab");
              return;
            }
            const controller = new AbortController();
            request.current = controller;
            setPending(true);
            setError("");
            try {
              await validate(destination, controller.signal);
              if (controller.signal.aborted) return;
              setEditing(false);
              onNavigate(destination);
            } catch (cause) {
              if (!controller.signal.aborted)
                setError(
                  `Cannot open file: ${cause instanceof Error ? cause.message : String(cause)}`,
                );
            } finally {
              if (!controller.signal.aborted) setPending(false);
            }
          }}
        >
          <input
            ref={input}
            aria-label="File path"
            aria-invalid={!!error}
            aria-describedby={error ? errorId : undefined}
            aria-busy={pending}
            className="w-full min-w-0 rounded border border-input bg-background px-1 font-mono text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            value={draft}
            onChange={(event) => {
              request.current?.abort();
              setPending(false);
              setDraft(event.target.value);
              setError("");
            }}
            onBlur={cancel}
            onKeyDown={(event) => {
              event.stopPropagation();
              if (event.key === "Escape") {
                event.preventDefault();
                cancel();
              }
            }}
          />
          {error && (
            <div id={errorId} role="alert" className="break-words text-xs text-destructive">
              {error}
            </div>
          )}
        </form>
      ) : (
        <button
          type="button"
          aria-label={`Edit file path: ${path}`}
          title={absolutePath}
          className="block max-w-full truncate rounded text-left font-mono text-sm text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          onClick={() => {
            setDraft(absolutePath);
            setEditing(true);
          }}
        >
          {path}
        </button>
      )}
    </div>
  );
}
