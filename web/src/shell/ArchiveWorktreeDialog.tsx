import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from "react";
import { ArchiveWorktreeTierChoices } from "@/components/ArchiveWorktreeTierChoices";
import { WORKTREE_LABELS } from "@/components/WorktreeStatusMark";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import type { Conversation } from "@/hooks/useConversations";
import { fetchSessionWorktreeStatus, type ArchiveWorktreeEntry } from "@/hooks/useWorktreeStatus";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import {
  fetchArchiveWorktreePreference,
  saveArchiveWorktreePreference,
  type ArchiveWorktreeMode,
  type ArchiveWorktreePreference,
} from "@/lib/archiveWorktreePreferences";
import { getOmnigentHostGeneration } from "@/lib/host";

type ProceedFn = (keepWorktreeIds: ReadonlySet<string>, mode?: ArchiveWorktreeMode) => void;
interface PendingPrompt {
  worktrees: ArchiveWorktreeEntry[];
  preference: ArchiveWorktreePreference;
  active: () => boolean;
  choose: (mode: ArchiveWorktreeMode) => void;
  cancel: () => void;
}

/** Ask for a tier until the account preference disables archive prompts. */
export function useArchiveWorktreePrompt(scope?: string): {
  requestArchive: (
    conversations: readonly Conversation[],
    proceed: ProceedFn,
    cancel?: () => void,
  ) => void;
  dialog: ReactNode;
} {
  const [pending, setPending] = useState<PendingPrompt | null>(null);
  const checking = useRef(false);
  const epoch = useRef(0);
  const mounted = useRef(false);
  const cancellation = useRef<(() => void) | null>(null);
  const generation = getOmnigentHostGeneration();
  const serverInfo = useServerInfo();
  const supported = serverInfo !== "loading" && serverInfo.worktree_status === true;
  useEffect(() => {
    epoch.current++;
    mounted.current = true;
    checking.current = false;
    setPending(null);
    return () => {
      mounted.current = false;
      cancellation.current?.();
      cancellation.current = null;
    };
  }, [scope, generation]);

  const requestArchive = useCallback(
    (conversations: readonly Conversation[], proceed: ProceedFn, cancel?: () => void) => {
      if (checking.current) {
        cancel?.();
        return;
      }
      if (!supported) {
        proceed(new Set());
        return;
      }
      checking.current = true;
      let cancelled = false;
      const cancelRequest = () => {
        if (!cancelled) {
          cancelled = true;
          cancel?.();
        }
      };
      cancellation.current = cancelRequest;
      const request = ++epoch.current;
      const hostGeneration = getOmnigentHostGeneration();
      const active = () =>
        mounted.current &&
        epoch.current === request &&
        getOmnigentHostGeneration() === hostGeneration;
      const finish = () => {
        checking.current = false;
        cancellation.current = null;
        setPending(null);
      };
      void (async () => {
        try {
          const preference = await fetchArchiveWorktreePreference();
          if (!active()) {
            cancelRequest();
            return;
          }
          if (!preference.ask_before_archive) {
            finish();
            proceed(new Set());
            return;
          }
          const statuses = await Promise.allSettled(
            conversations.map((conversation) => fetchSessionWorktreeStatus(conversation.id, true)),
          );
          if (!active()) {
            cancelRequest();
            return;
          }
          const keepWorktreeIds = new Set<string>();
          const entries = statuses.flatMap((result, index) => {
            if (result.status === "rejected") {
              keepWorktreeIds.add(conversations[index].id);
              return [
                {
                  session_id: conversations[index].id,
                  title: conversations[index].title || "Session and descendants",
                  state: "unknown" as const,
                  path: null,
                  reason: "worktree status unavailable; descendant directories kept",
                  kept: true,
                },
              ];
            }
            const status = result.value;
            return (
              status.worktrees ??
              (status.own.state === "none"
                ? []
                : [
                    {
                      session_id: conversations[index].id,
                      title: conversations[index].title || "Untitled session",
                      state: status.own.state,
                      reason: status.own.reason,
                      path: status.own.path,
                      kept: false,
                    },
                  ])
            );
          });
          const worktrees = [
            ...new Map(entries.map((entry) => [entry.session_id, entry])).values(),
          ];
          if (worktrees.length === 0) {
            finish();
            proceed(new Set());
            return;
          }
          setPending({
            worktrees,
            preference,
            active,
            choose: (mode) => {
              if (!active()) return;
              finish();
              proceed(keepWorktreeIds, mode);
            },
            cancel: () => {
              epoch.current++;
              finish();
              cancelRequest();
            },
          });
        } catch {
          if (active()) {
            finish();
            proceed(new Set(conversations.map((conversation) => conversation.id)));
          } else cancelRequest();
        }
      })();
    },
    [supported],
  );

  return { requestArchive, dialog: pending ? <ArchiveWorktreeDialog pending={pending} /> : null };
}

function ArchiveWorktreeDialog({ pending }: { pending: PendingPrompt }) {
  const [mode, setMode] = useState(pending.preference.mode);
  const [remember, setRemember] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const checkboxId = useId();
  const confirm = useRef<HTMLButtonElement>(null);
  const choosing = useRef(false);
  useEffect(() => {
    // Wait for the opening menu's focus restoration.
    const timer = setTimeout(() => confirm.current?.focus(), 0);
    return () => clearTimeout(timer);
  }, []);

  async function choose() {
    if (choosing.current || !pending.active()) return;
    choosing.current = true;
    setError(null);
    setSaving(true);
    try {
      if (remember)
        await saveArchiveWorktreePreference({
          ...pending.preference,
          mode,
          ask_before_archive: false,
        });
      pending.choose(mode);
    } catch (failure) {
      if (pending.active())
        setError(failure instanceof Error ? failure.message : "Saving archive preferences failed.");
    } finally {
      choosing.current = false;
      if (pending.active()) setSaving(false);
    }
  }

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) pending.cancel();
      }}
    >
      <DialogContent
        className="sm:max-w-lg"
        onClick={(event) => event.stopPropagation()}
        data-testid="archive-worktree-dialog"
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          confirm.current?.focus();
        }}
      >
        <DialogHeader>
          <DialogTitle>Worktrees on archive</DialogTitle>
          <DialogDescription>
            This action archives the selected sessions and their active descendants. Choose which
            worktree directories to remove. Branches are kept. Uncommitted source changes are never
            deleted.
          </DialogDescription>
        </DialogHeader>
        <fieldset disabled={saving} aria-label="Worktree deletion tier">
          <ArchiveWorktreeTierChoices mode={mode} onChange={setMode} />
        </fieldset>
        <ul
          aria-label="Worktrees in this archive"
          className="max-h-56 space-y-3 overflow-y-auto rounded-md border p-3 text-sm"
        >
          {pending.worktrees.map((entry) => {
            const removable =
              !entry.kept &&
              mode !== "never" &&
              (entry.state === "clean" || (entry.state === "local" && mode === "yellow_too"));
            return (
              <li key={entry.session_id}>
                <p className="font-medium">
                  {entry.title} · {WORKTREE_LABELS[entry.state]}
                </p>
                {entry.path && (
                  <p className="break-all font-mono text-xs text-muted-foreground">{entry.path}</p>
                )}
                <p className="text-muted-foreground">
                  {removable
                    ? "Remove after safety checks"
                    : entry.state === "unknown" && !entry.kept && mode !== "never"
                      ? `Keep unless a fresh safety check permits removal: ${entry.reason || "status unknown"}`
                      : `Keep: ${mode === "never" && !entry.kept ? "selected tier keeps all worktrees" : entry.state === "local" && !entry.kept ? "selected tier keeps local files" : entry.reason || "safety check required"}`}
                </p>
              </li>
            );
          })}
        </ul>
        <div className="flex flex-col gap-1">
          <div className="flex items-center gap-2">
            <Checkbox
              id={checkboxId}
              checked={remember}
              disabled={saving}
              onCheckedChange={(checked) => setRemember(checked === true)}
              data-testid="archive-worktree-remember"
              componentId="archive.worktree_prompt.remember"
            />
            <label htmlFor={checkboxId} className="cursor-pointer text-ui">
              Don't ask again
            </label>
          </div>
          <p className="pl-6 text-sm text-muted-foreground">
            Change the tier, rebuildable files, or Ask before archiving in Settings › Git.
          </p>
        </div>
        {error && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}
        <DialogFooter className="border-t-0 bg-transparent">
          <Button type="button" variant="ghost" onClick={pending.cancel}>
            Cancel
          </Button>
          <Button
            ref={confirm}
            type="button"
            disabled={saving}
            variant={mode === "never" ? "default" : "destructive"}
            data-testid="archive-worktree-confirm"
            componentId="archive.worktree_prompt.confirm"
            onClick={() => void choose()}
          >
            {saving ? "Saving…" : "Archive"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
