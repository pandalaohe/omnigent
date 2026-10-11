import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useWorktreeStatus } from "@/hooks/useWorktreeStatus";
import { WORKTREE_LABELS } from "./WorktreeStatusMark";

export function WorktreeDetailsDialog({
  sessionId,
  open,
  onOpenChange,
}: {
  sessionId: string | null | undefined;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { data, supported, isError, isFetching } = useWorktreeStatus(open ? sessionId : null);
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="max-h-[85dvh] overflow-y-auto sm:max-w-xl"
        onClick={(event) => event.stopPropagation()}
        data-testid="worktree-details-dialog"
      >
        <DialogHeader>
          <DialogTitle>Worktree details</DialogTitle>
          <DialogDescription>
            Archive removes only this session's eligible worktree. Branches are kept; uncommitted
            source changes are never deleted.
          </DialogDescription>
        </DialogHeader>
        {!supported ? (
          <p className="text-ui text-muted-foreground">
            Worktree details are unavailable on this server.
          </p>
        ) : isError ? (
          <p role="alert" className="text-ui text-destructive">
            Could not check this worktree. It will be kept.
          </p>
        ) : isFetching || !data ? (
          <p className="text-ui text-muted-foreground">Checking worktree…</p>
        ) : (
          <div className="space-y-4 text-ui">
            <section className="space-y-1" aria-label="This session's worktree">
              <p className="font-medium">{WORKTREE_LABELS[data.own.state]}</p>
              {data.own.reason && <p className="text-muted-foreground">{data.own.reason}</p>}
              {data.own.path && <p className="break-all font-mono text-sm">{data.own.path}</p>}
              {data.own.branch && (
                <p>
                  Branch: <span className="font-mono">{data.own.branch}</span> (kept)
                </p>
              )}
              {data.own.merged !== null && (
                <p className="text-muted-foreground">
                  {data.own.merged ? "Merged" : "Not merged"}
                  {data.own.merge_target ? ` into ${data.own.merge_target}` : ""}. Merge state does
                  not affect deletion.
                </p>
              )}
            </section>
            {data.own.files.length > 0 && (
              <section aria-label="Worktree files">
                <h3 className="mb-2 font-medium">Files in this worktree</h3>
                <ul className="space-y-2">
                  {data.own.files.map((file) => (
                    <li key={`${file.status}-${file.path}`} className="text-sm">
                      <span className="block break-all font-mono">
                        {file.status} {file.path}
                      </span>
                      <span className="text-muted-foreground">
                        {file.kind === "rebuildable"
                          ? "Rebuildable local files"
                          : file.kind === "local" || file.status === "!!"
                            ? "Local files (Git ignored)"
                            : "Uncommitted source changes"}
                      </span>
                    </li>
                  ))}
                </ul>
              </section>
            )}
            {data.blockers.filter((blocker) => blocker.session_id !== sessionId).length > 0 && (
              <section aria-label="Descendant worktrees">
                <h3 className="mb-2 font-medium">Child worktrees</h3>
                <ul className="space-y-2">
                  {data.blockers
                    .filter((blocker) => blocker.session_id !== sessionId)
                    .map((blocker) => (
                      <li key={blocker.session_id}>
                        <span className="font-medium">{blocker.title}</span>:{" "}
                        {WORKTREE_LABELS[blocker.state]}
                        {blocker.reason && (
                          <span className="block text-sm text-muted-foreground">
                            {blocker.reason}
                          </span>
                        )}
                      </li>
                    ))}
                </ul>
              </section>
            )}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
