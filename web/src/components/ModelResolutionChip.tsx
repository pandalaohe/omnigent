import { Link } from "@/lib/routing";
import { AlertTriangleIcon } from "lucide-react";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { useSession } from "@/hooks/useSession";
import { modelResolutionNotes } from "@/lib/modelReference";

export function ModelResolutionChip({ sessionId }: { sessionId: string }) {
  const { session } = useSession(sessionId);
  const notes = modelResolutionNotes(session?.labels);
  if (notes.length === 0) return null;
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
        <Link
          className="text-primary underline underline-offset-2"
          to="/settings/agents#model-references"
        >
          Review saved models
        </Link>
      </PopoverContent>
    </Popover>
  );
}
