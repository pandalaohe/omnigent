import { AlertTriangleIcon } from "lucide-react";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { useSession } from "@/hooks/useSession";
import { modelResolutionNotes, sessionHeaderModelState } from "@/lib/modelReference";

export function SessionHeaderModel({ sessionId }: { sessionId: string }) {
  const { session } = useSession(sessionId);
  const { launched } = sessionHeaderModelState(session);
  if (!launched) return null;
  return (
    <span
      data-testid="session-header-model"
      title={launched}
      aria-label={`Launched model: ${launched}`}
      className="hidden max-w-44 shrink-0 truncate rounded-md border border-border/60 bg-background/60 px-2 py-0.5 font-mono text-xs text-muted-foreground md:inline-flex"
    >
      {launched}
    </span>
  );
}

export function ModelResolutionChip({ sessionId }: { sessionId: string }) {
  const { session } = useSession(sessionId);
  const notes = modelResolutionNotes(session?.labels);
  const modelState = sessionHeaderModelState(session);
  if (notes.length === 0 && !modelState.hasDifference) return null;
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label="Model resolution warning"
          className="flex h-7 shrink-0 items-center gap-1 rounded-full border border-amber-500/40 bg-background px-2 text-xs text-amber-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <AlertTriangleIcon className="size-3.5" aria-hidden="true" />
          Model reference
        </button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-80 max-w-[calc(100vw-1rem)] space-y-3 text-sm">
        <h2 className="font-medium">Model resolution</h2>
        {modelState.hasDifference && (
          <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs">
            {modelState.requested && (
              <>
                <dt className="text-muted-foreground">Requested</dt>
                <dd className="break-all font-mono">{modelState.requested}</dd>
              </>
            )}
            {modelState.launched && (
              <>
                <dt className="text-muted-foreground">Launched</dt>
                <dd className="break-all font-mono">{modelState.launched}</dd>
              </>
            )}
            {modelState.running && (
              <>
                <dt className="text-muted-foreground">Running</dt>
                <dd className="break-all font-mono">{modelState.running}</dd>
              </>
            )}
          </dl>
        )}
        {notes.length > 0 && (
          <ul className="space-y-2">
            {notes.map((note) => (
              <li key={JSON.stringify(note)} className="space-y-1">
                <p className="break-all font-mono text-xs">
                  {note.saved}
                  {note.model !== note.saved && <> → {note.model}</>}
                </p>
                <p className="text-muted-foreground">
                  {note.outcome === "missing"
                    ? "Not listed by this host; tried as unknown."
                    : "Resolved automatically for this host."}
                </p>
              </li>
            ))}
          </ul>
        )}
      </PopoverContent>
    </Popover>
  );
}
