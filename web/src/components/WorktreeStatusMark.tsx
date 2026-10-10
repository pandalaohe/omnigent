import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useWorktreeStatus, type WorktreeState } from "@/hooks/useWorktreeStatus";
import { cn } from "@/lib/utils";

const LABELS: Record<WorktreeState, string> = {
  none: "No worktree",
  clean: "Worktrees clean",
  dirty: "Worktree has uncommitted changes",
  unknown: "Worktree status unknown",
  protected: "Worktree kept by rule",
  shared: "Worktree shared with another session",
  removed: "Worktree removed",
};

function TreeGlyph({ state }: { state: WorktreeState }) {
  const ghost = state === "unknown" || state === "removed";
  return (
    <svg
      viewBox="0 0 24 24"
      width="14"
      height="14"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      data-testid="worktree-tree-glyph"
    >
      <path
        d="M8 22 11 14V3 M11 8h5l4-4 M11 13h6l4-2 M11 11 6 8V5"
        strokeDasharray={state === "unknown" ? "2.4 2" : undefined}
      />
      {[
        [9.5, 1.5],
        [18.5, 2.5],
        [19.5, 9.5],
        [4.5, 3.5],
      ].map(([x, y]) => (
        <rect
          key={`${x}-${y}`}
          x={x}
          y={y}
          width="3"
          height="3"
          rx=".35"
          fill={ghost ? "none" : "currentColor"}
          stroke={ghost ? "currentColor" : "none"}
          strokeWidth={ghost ? "1.6" : undefined}
        />
      ))}
      {(state === "protected" || state === "shared") && <path d="M4.5 20.5h7" strokeWidth="2.8" />}
    </svg>
  );
}

/** Read-only marker safe to place inside a navigation link. */
export function WorktreeStatusMark({
  sessionId,
  className,
}: {
  sessionId: string | null | undefined;
  className?: string;
}) {
  const { data, supported, isError, isFetching } = useWorktreeStatus(sessionId);
  if (!sessionId || !supported) return null;
  const state: WorktreeState = isError || isFetching || !data ? "unknown" : data.aggregate.state;
  if (state === "none") return null;
  const label = LABELS[state];
  const blockers = data?.blockers ?? [];
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          tabIndex={0}
          role="img"
          aria-label={label}
          data-worktree-state={state}
          className={cn(
            "inline-flex size-5 shrink-0 items-center justify-center rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
            state === "clean" && "text-success",
            state === "dirty" && "text-warning",
            state === "unknown" && "text-muted-foreground",
            (state === "protected" || state === "shared") && "text-foreground opacity-70",
            state === "removed" && "text-muted-foreground opacity-45",
            className,
          )}
        >
          <TreeGlyph state={state} />
        </span>
      </TooltipTrigger>
      <TooltipContent side="bottom" className="flex-col items-start gap-1">
        <span className="font-medium">{label}</span>
        {data?.aggregate.reason && <span>{data.aggregate.reason}</span>}
        {data?.own.branch && <span>Branch: {data.own.branch} (kept on archive)</span>}
        {data?.own.merged !== null && data?.own.merged !== undefined && (
          <span>
            {data.own.merged ? "Merged" : "Not merged"}
            {data.own.merge_target ? ` into ${data.own.merge_target}` : ""}. Merge state does not
            affect safe deletion.
          </span>
        )}
        {data && data.session_count > 1 && (
          <span>
            This session: {LABELS[data.own.state]}
            {data.own.reason ? ` — ${data.own.reason}` : ""}
          </span>
        )}
        {blockers.map((blocker) => (
          <span key={blocker.session_id}>
            {blocker.title}: {LABELS[blocker.state]}
            {blocker.reason ? ` — ${blocker.reason}` : ""}
          </span>
        ))}
      </TooltipContent>
    </Tooltip>
  );
}
