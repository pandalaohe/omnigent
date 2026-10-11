import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from "react";
import { ArchiveWorktreeTierChoices } from "@/components/ArchiveWorktreeTierChoices";
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
  worktreeCount: number;
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
      const worktreeCount = conversations.filter(
        (conversation) => conversation.git_branch != null || Boolean(conversation.worktree),
      ).length;
      if (!supported || worktreeCount === 0) {
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
          setPending({
            worktreeCount,
            preference,
            active,
            choose: (mode) => {
              if (!active()) return;
              finish();
              proceed(new Set(), mode);
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
            {pending.worktreeCount === 1
              ? "This session has a git worktree."
              : pending.worktreeCount + " of these sessions have git worktrees."}{" "}
            Choose which worktree directories to remove. Branches are kept. Uncommitted source
            changes are never deleted.
          </DialogDescription>
        </DialogHeader>
        <fieldset disabled={saving} aria-label="Worktree deletion tier">
          <ArchiveWorktreeTierChoices mode={mode} onChange={setMode} />
        </fieldset>
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
