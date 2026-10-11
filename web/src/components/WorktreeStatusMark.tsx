import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useWorktreeStatus, type WorktreeState } from "@/hooks/useWorktreeStatus";
import { cn } from "@/lib/utils";

export const WORKTREE_LABELS: Record<WorktreeState, string> = {
  none: "No worktree",
  clean: "Clean or rebuildable worktree",
  local: "Worktree has local files",
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
  const state: WorktreeState = isError || isFetching || !data ? "unknown" : data.own.state;
  const label = WORKTREE_LABELS[state];
  const blockers = (data?.blockers ?? []).filter(
    (blocker) =>
      blocker.session_id !== sessionId &&
      !(
        blocker.state === "protected" &&
        ["project entry", "main checkout"].includes(blocker.reason ?? "")
      ),
  );
  if (state === "none" && blockers.length === 0) return null;
  const action =
    state === "clean" || state === "local"
      ? "follows archive setting"
      : state === "removed"
        ? "branch kept"
        : state === "none"
          ? "no directory to remove"
          : "kept on archive";
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          tabIndex={0}
          role="img"
          aria-label={
            blockers.length ? `${label}; ${blockers.length} child worktrees need attention` : label
          }
          data-worktree-state={state}
          className={cn(
            "relative inline-flex size-5 shrink-0 items-center justify-center rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
            state === "clean" && "text-success",
            state === "local" && "text-warning",
            state === "dirty" && "text-destructive",
            state === "unknown" && "text-muted-foreground",
            (state === "protected" || state === "shared") && "text-foreground opacity-70",
            state === "removed" && "text-muted-foreground opacity-45",
            className,
          )}
        >
          <TreeGlyph state={state} />
          {blockers.length > 0 && (
            <span
              className="absolute -right-1 -top-1 rounded bg-warning px-0.5 text-[9px] leading-3 text-zinc-950"
              aria-hidden="true"
            >
              {blockers.length}
            </span>
          )}
        </span>
      </TooltipTrigger>
      <TooltipContent side="bottom" className="flex-col items-start gap-1">
        <span className="font-medium">
          {label} · {action}
        </span>
        {blockers.slice(0, 2).map((blocker) => (
          <span key={blocker.session_id} className="max-w-full truncate">
            {blocker.title}: {WORKTREE_LABELS[blocker.state]}
          </span>
        ))}
        {blockers.length > 2 && <span>{blockers.length - 2} more child worktrees</span>}
        <span className="text-xs opacity-75">Session menu → Worktree details</span>
      </TooltipContent>
    </Tooltip>
  );
}
