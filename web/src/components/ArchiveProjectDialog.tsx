import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { ProjectSinglePicker } from "@/components/ProjectSinglePicker";
import { SelectionList } from "@/components/SelectionList";
import { resolveMulti, type MultiValue } from "@/lib/multiSelection";
import { invalidateProjectArchiveQueries, useBulkMoveToProject } from "@/hooks/useConversations";
import {
  archiveProject,
  listProjects,
  previewProjectArchive,
  unarchiveProject,
  type ProjectArchivePreview,
  type ProjectArchiveResult,
} from "@/lib/projectsApi";
import { updateScheduledTask } from "@/lib/scheduledTasksApi";

function sessionStopText(session: ProjectArchivePreview["sessions"][number]) {
  if (session.host_keeps_cli) return "This host keeps the command line after archive";
  if (session.status === "running") return "Closes after this turn is idle, within about 1 hour";
  if (session.status === "waiting" || session.status === "waiting_for_reply")
    return "Closes within about 1 hour even without a reply";
  return "Closes after archive";
}

function nextRunText(value: string | null) {
  if (!value) return "Next run unavailable";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Next run unavailable";
  return date.toLocaleString();
}

export function projectArchiveResultMessage(
  projectName: string,
  result: ProjectArchiveResult,
  unarchive: boolean,
) {
  const tasks = result.tasks.done
    .map((task) =>
      unarchive ? `${task.name} (next: ${nextRunText(task.next_run_at)})` : task.name,
    )
    .join(", ");
  const skipped = result.sessions.skipped.length + result.tasks.skipped.length;
  return unarchive
    ? `Restored ${projectName} · ${result.sessions.done.length} sessions${tasks ? ` · ${tasks} resumed` : ""}${skipped ? ` · ${skipped} not restored` : ""}`
    : `Archived ${projectName} · ${result.sessions.done.length} sessions${tasks ? ` · ${tasks} stopped` : ""}`;
}

export function projectAncestorIdsToMove(
  sessions: ProjectArchivePreview["sessions"],
  selectedIds: string[],
  projectId: string,
): string[] {
  const byId = new Map(sessions.map((session) => [session.id, session]));
  const ancestors = new Set<string>();
  for (const id of selectedIds) {
    let parent = byId.get(id)?.parent_id;
    const seen = new Set<string>();
    while (parent && !seen.has(parent)) {
      seen.add(parent);
      const candidate = byId.get(parent);
      if (!candidate) break;
      if (candidate.belongs_to_project ?? candidate.project_id === projectId) ancestors.add(parent);
      parent = candidate.parent_id;
    }
  }
  return [...ancestors];
}

function toastResult(
  projectName: string,
  result: ProjectArchiveResult,
  unarchive: boolean,
  retry: () => void,
  undo?: () => void,
) {
  if (result.status === "incomplete") {
    toast(`Some members of ${projectName} were not processed (${result.failed.length} failed).`, {
      duration: 10_000,
      action: { label: "Retry", onClick: retry },
    });
    return;
  }
  const text = projectArchiveResultMessage(projectName, result, unarchive);
  toast(text, { duration: 10_000, ...(undo ? { action: { label: "Undo", onClick: undo } } : {}) });
}

export function ArchiveProjectDialog({
  projectId,
  projectName,
  open,
  onOpenChange,
}: {
  projectId: string;
  projectName: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const preview = useQuery({
    queryKey: ["project-archive-preview", projectId],
    queryFn: () => previewProjectArchive(projectId),
    enabled: open,
  });
  const projects = useQuery({
    queryKey: ["projects", "active-list"],
    queryFn: () => listProjects(),
    enabled: open,
  });
  const moveSessions = useBulkMoveToProject();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [moveKind, setMoveKind] = useState<"sessions" | "tasks" | null>(null);
  const [moveSelection, setMoveSelection] = useState<MultiValue>({ mode: "only", ids: [] });
  const [destination, setDestination] = useState<string | null>(null);
  const [expandedSessions, setExpandedSessions] = useState(false);
  const sessions = useMemo(() => preview.data?.sessions ?? [], [preview.data]);
  const tasks = useMemo(() => preview.data?.scheduled_tasks ?? [], [preview.data]);
  const projectChoices = (projects.data ?? []).filter((project) => project.id !== projectId);
  const moveItems = useMemo(
    () =>
      moveKind === "sessions"
        ? sessions.map((session) => ({
            id: session.id,
            label: session.title || session.id,
            description: sessionStopText(session),
          }))
        : tasks.map((task) => ({
            id: task.id,
            label: task.name,
            aside: nextRunText(task.next_run_at),
          })),
    [moveKind, sessions, tasks],
  );
  const selected = resolveMulti(moveSelection, moveItems);
  const selectedAncestors = useMemo(
    () => (moveKind === "sessions" ? projectAncestorIdsToMove(sessions, selected, projectId) : []),
    [moveKind, projectId, selected, sessions],
  );
  const refresh = async () => {
    await Promise.all([invalidateProjectArchiveQueries(queryClient), preview.refetch()]);
  };
  const archive = async () => {
    setBusy(true);
    setError(null);
    try {
      const result = await archiveProject(projectId);
      await refresh();
      toastResult(
        projectName,
        result,
        false,
        () => void archive(),
        () => void restoreProjectWithNotice(projectId, projectName, queryClient),
      );
      if (result.status === "complete") onOpenChange(false);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  };
  const move = async () => {
    if (!destination || !selected.length || !moveKind) return;
    setBusy(true);
    setError(null);
    try {
      if (moveKind === "sessions") {
        const target = projectChoices.find((project) => project.id === destination);
        if (!target) return;
        await moveSessions.mutateAsync({
          ids: [...new Set([...selected, ...selectedAncestors])],
          project: target.name,
        });
      } else {
        await Promise.all(
          selected.map((id) => updateScheduledTask(id, { projectId: destination })),
        );
      }
      await refresh();
      setMoveKind(null);
      setMoveSelection({ mode: "only", ids: [] });
      setDestination(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!busy) {
          onOpenChange(next);
          if (!next) setMoveKind(null);
        }
      }}
    >
      <DialogContent
        className="flex max-h-[90dvh] max-w-2xl flex-col overflow-hidden"
        data-testid="archive-project-dialog"
      >
        <DialogHeader>
          <DialogTitle>
            {moveKind ? `Move ${moveKind} to another project` : `Archive ${projectName}?`}
          </DialogTitle>
          <DialogDescription>
            {moveKind
              ? "Moved items remain active and will not be archived with this project."
              : "The project leaves the sidebar and project pickers. You can restore it later."}
          </DialogDescription>
        </DialogHeader>
        <div className="min-h-0 overflow-y-auto text-sm">
          {preview.isLoading && <p>Loading archive preview…</p>}
          {preview.isError && (
            <p role="alert" className="text-destructive">
              Couldn’t load the archive preview.{" "}
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => void preview.refetch()}
              >
                Retry
              </Button>
            </p>
          )}
          {moveKind ? (
            <div className="space-y-3">
              <SelectionList
                key={moveKind}
                mode="multi"
                items={moveItems}
                value={moveSelection}
                onChange={setMoveSelection}
                searchLabel={`Search ${moveKind}`}
                onConfirm={() => void move()}
              />
              {selectedAncestors.length > 0 && (
                <p className="text-xs text-muted-foreground">
                  Included parents:{" "}
                  {selectedAncestors
                    .map((id) => sessions.find((session) => session.id === id)?.title ?? id)
                    .join(", ")}
                </p>
              )}
              <div>
                <p className="mb-1 font-medium">Move to</p>
                <ProjectSinglePicker
                  projects={projectChoices}
                  value={destination}
                  onChange={setDestination}
                  label="Destination project"
                  noneLabel="Choose a project"
                />
              </div>
            </div>
          ) : (
            preview.data && (
              <div className="space-y-4">
                <section>
                  <h3 className="font-medium">{sessions.length} sessions will be archived</h3>
                  {(expandedSessions ? sessions : sessions.slice(0, 8)).map((session) => (
                    <div
                      key={session.id}
                      className="flex flex-wrap justify-between gap-x-3 border-b py-2"
                    >
                      <span>
                        {session.title || session.id}
                        {session.via_parent_outside_project && (
                          <span className="ml-2 text-xs text-muted-foreground">
                            Child in another project
                          </span>
                        )}
                      </span>
                      <span className="text-xs text-muted-foreground">
                        {sessionStopText(session)}
                      </span>
                    </div>
                  ))}
                  {sessions.length > 8 && (
                    <Button
                      type="button"
                      size="sm"
                      variant="ghost"
                      onClick={() => setExpandedSessions(!expandedSessions)}
                    >
                      {expandedSessions ? "Show fewer" : `Show all ${sessions.length}`}
                    </Button>
                  )}
                </section>
                <section>
                  <h3 className="font-medium">{tasks.length} automations will stop firing</h3>
                  {tasks.map((task) => {
                    const soon =
                      task.next_run_at &&
                      new Date(task.next_run_at).getTime() - Date.now() < 86_400_000 &&
                      new Date(task.next_run_at).getTime() > Date.now();
                    return (
                      <div
                        key={task.id}
                        className="flex flex-wrap justify-between gap-x-3 border-b py-2"
                      >
                        <span>
                          {task.name}
                          {task.last_run_status === "running" && (
                            <span className="ml-2 text-xs">Current run will finish</span>
                          )}
                        </span>
                        <span className={soon ? "text-amber-500" : "text-xs text-muted-foreground"}>
                          {soon ? "Within 24 hours · " : ""}
                          {nextRunText(task.next_run_at)}
                        </span>
                      </div>
                    );
                  })}
                </section>
                <p className="rounded-md bg-muted p-3 text-xs text-muted-foreground">
                  Project settings, entry folders, default agent, order, sidebar groups, and
                  favorites remain. Worktrees remain. Archived sessions stop receiving messages and
                  staying warm immediately. Command lines follow each host’s archive setting.
                </p>
                {preview.data.health_check_uses_project && (
                  <p className="text-xs text-amber-500">
                    A health check uses this project; after archive it will start without a project.
                  </p>
                )}
                <div className="flex flex-wrap gap-2">
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    disabled={!sessions.length}
                    onClick={() => {
                      setMoveKind("sessions");
                      setMoveSelection({ mode: "only", ids: [] });
                    }}
                  >
                    Move sessions to another project…
                  </Button>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    disabled={!tasks.length}
                    onClick={() => {
                      setMoveKind("tasks");
                      setMoveSelection({ mode: "only", ids: [] });
                    }}
                  >
                    Move automations to another project…
                  </Button>
                </div>
              </div>
            )
          )}
        </div>
        {error && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}
        <DialogFooter>
          <Button
            type="button"
            variant="ghost"
            onClick={() => (moveKind ? setMoveKind(null) : onOpenChange(false))}
            disabled={busy}
          >
            Cancel
          </Button>
          {moveKind ? (
            <Button
              type="button"
              onClick={() => void move()}
              disabled={busy || !destination || !selected.length}
            >
              Move{" "}
              {selected.length + selectedAncestors.filter((id) => !selected.includes(id)).length}
            </Button>
          ) : (
            <Button type="button" onClick={() => void archive()} disabled={busy || !preview.data}>
              Archive project
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export async function restoreProjectWithNotice(
  projectId: string,
  projectName: string,
  queryClient: ReturnType<typeof useQueryClient>,
) {
  const result = await unarchiveProject(projectId);
  await invalidateProjectArchiveQueries(queryClient);
  toastResult(
    projectName,
    result,
    true,
    () => void restoreProjectWithNotice(projectId, projectName, queryClient),
  );
  return result;
}
