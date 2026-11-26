import { AlertTriangleIcon } from "lucide-react";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { useHoverPinFloat } from "@/hooks/useHoverPinFloat";
import {
  modelResolutionNotes,
  sessionHeaderModelState,
  type ModelResolutionNote,
  type SessionHeaderModelState,
} from "@/lib/modelReference";
import type { Session } from "@/lib/types";

type ModelResolutionSession = Pick<
  Session,
  "harness" | "requested" | "launched" | "running" | "codexModelOptions" | "labels"
>;

export function ModelResolutionChip({
  session,
}: {
  session: ModelResolutionSession | null | undefined;
}) {
  const notes = modelResolutionNotes(session?.labels);
  const modelState = sessionHeaderModelState(session);
  if (notes.length === 0 && !modelState.hasDifference) return null;
  return <ModelResolutionWarningButton modelState={modelState} notes={notes} />;
}

function ModelResolutionWarningButton({
  modelState,
  notes,
}: {
  modelState: SessionHeaderModelState;
  notes: readonly ModelResolutionNote[];
}) {
  const { open, setOpen, triggerHandlers, contentHandlers } = useHoverPinFloat();
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          data-testid="model-resolution-warning"
          aria-label="Model resolution warning"
          className="relative flex h-8 min-w-8 shrink-0 items-center justify-center rounded-full bg-transparent px-1.5 text-amber-600 hover:text-amber-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:ring-offset-2 focus-visible:ring-offset-background md:h-6 md:min-w-6 md:px-1 dark:text-amber-500 dark:hover:text-amber-400"
          {...triggerHandlers}
          onClick={(event) => {
            // A click or tap pins the float; Escape or a click beside it clears it.
            event.preventDefault();
            setOpen(true);
          }}
        >
          <AlertTriangleIcon className="size-3.5" aria-hidden="true" />
        </button>
      </PopoverTrigger>
      <PopoverContent
        side="top"
        align="end"
        collisionPadding={8}
        data-pop=""
        data-testid="model-resolution-popover"
        className="w-80 max-w-[calc(100vw-1rem)] space-y-3 text-sm"
        onOpenAutoFocus={(event) => event.preventDefault()}
        onCloseAutoFocus={(event) => event.preventDefault()}
        {...contentHandlers}
      >
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
