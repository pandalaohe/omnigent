import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import type { Conversation } from "@/hooks/useConversations";
import { fetchSessionWorktreeStatus, type SessionWorktreeStatus } from "@/hooks/useWorktreeStatus";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { fetchArchiveWorktreePreference } from "@/lib/archiveWorktreePreferences";
import { getOmnigentHostGeneration } from "@/lib/host";

/** Explicit keep overrides the server policy for the selected unsafe sessions. */
type ProceedFn = (keepWorktreeIds: ReadonlySet<string>) => void;
interface Warning {
  id: string;
  title: string;
  own: SessionWorktreeStatus["own"] | null;
}
interface PendingPrompt {
  warnings: Warning[];
  proceed: ProceedFn;
  cancel?: () => void;
}

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
  const cancelPrompt = useRef<(() => void) | null>(null);
  const warningList = useRef<HTMLDivElement>(null);
  const serverInfo = useServerInfo();
  const supported = serverInfo !== "loading" && serverInfo.worktree_status === true;
  useEffect(() => {
    const lifecycle = mounted;
    const requestEpoch = epoch;
    const cancellation = cancelPrompt;
    mounted.current = true;
    checking.current = false;
    setPending(null);
    return () => {
      lifecycle.current = false;
      requestEpoch.current++;
      cancellation.current?.();
      cancellation.current = null;
    };
  }, [scope]);
  useEffect(() => {
    if (!pending) return;
    // Opening from a menu must wait for that menu's focus restoration.
    const timer = setTimeout(
      () =>
        warningList.current?.parentElement
          ?.querySelector<HTMLButtonElement>("[data-testid='archive-worktree-keep']")
          ?.focus(),
      0,
    );
    return () => clearTimeout(timer);
  }, [pending]);

  const requestArchive = useCallback(
    (conversations: readonly Conversation[], proceed: ProceedFn, cancel?: () => void) => {
      if (checking.current || pending !== null) {
        cancel?.();
        return;
      }
      if (!supported || conversations.length === 0) {
        proceed(new Set());
        return;
      }
      checking.current = true;
      cancelPrompt.current = cancel ?? null;
      const request = epoch.current;
      const generation = getOmnigentHostGeneration();
      const active = () =>
        mounted.current && epoch.current === request && getOmnigentHostGeneration() === generation;
      void (async () => {
        let prompted = false;
        try {
          // Read afresh so another device's preference applies to this archive.
          const mode = await fetchArchiveWorktreePreference();
          if (!active()) {
            cancel?.();
            return;
          }
          if (mode === "never") {
            proceed(new Set());
            return;
          }
          const warnings = (
            await Promise.all(
              conversations.map(async (conversation): Promise<Warning | null> => {
                let own: SessionWorktreeStatus["own"] | null = null;
                try {
                  own = (await fetchSessionWorktreeStatus(conversation.id, true)).own;
                } catch {
                  // A failed read is not proof that a folder is clean.
                }
                if (own && ["clean", "none", "removed"].includes(own.state)) return null;
                return {
                  id: conversation.id,
                  title: conversation.title || "Untitled session",
                  own,
                };
              }),
            )
          ).filter((warning): warning is Warning => warning !== null);
          if (!active()) {
            cancel?.();
            return;
          }
          if (warnings.length === 0) proceed(new Set());
          else {
            prompted = true;
            setPending({ warnings, proceed, cancel });
          }
        } catch {
          // No readable preference: archive conservatively, retaining every tree.
          if (active()) proceed(new Set(conversations.map((conversation) => conversation.id)));
          else cancel?.();
        } finally {
          if (epoch.current === request) checking.current = false;
          if (!prompted && epoch.current === request) cancelPrompt.current = null;
        }
      })();
    },
    [supported, pending],
  );

  return {
    requestArchive,
    dialog: (
      <Dialog
        open={pending !== null}
        onOpenChange={(open) => {
          if (!open) {
            pending?.cancel?.();
            cancelPrompt.current = null;
            setPending(null);
          }
        }}
      >
        {pending && (
          <DialogContent
            className="sm:max-w-lg"
            onClick={(event) => event.stopPropagation()}
            data-testid="archive-worktree-dialog"
            onOpenAutoFocus={(event) => {
              event.preventDefault();
              if (event.target instanceof HTMLElement)
                event.target
                  .querySelector<HTMLButtonElement>("[data-testid='archive-worktree-keep']")
                  ?.focus();
            }}
          >
            <DialogHeader>
              <DialogTitle>Keep these worktrees when archiving</DialogTitle>
              <DialogDescription>
                These worktrees cannot be safely deleted. Deleting a worktree would lose its
                uncommitted and untracked files. Archive only keeps the files and branches.
              </DialogDescription>
            </DialogHeader>
            <div ref={warningList} className="max-h-72 space-y-3 overflow-y-auto">
              {pending.warnings.map(({ id, title, own }) => (
                <div key={id} className="rounded-md border border-border p-3 text-ui">
                  <p className="font-medium">{title}</p>
                  {own?.path && (
                    <p className="break-all text-sm text-muted-foreground">{own.path}</p>
                  )}
                  <p className="mt-1 text-sm text-muted-foreground">
                    {own?.reason || "Worktree state could not be checked."}
                  </p>
                  {!!own?.files.length && (
                    <ul
                      className="mt-2 space-y-1 text-sm"
                      aria-label={`Files that would be lost in ${title}`}
                    >
                      {own.files.map((file) => (
                        <li key={`${file.status}-${file.path}`} className="break-all font-mono">
                          {file.status} {file.path}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              ))}
            </div>
            <DialogFooter className="border-t-0 bg-transparent">
              <Button
                type="button"
                variant="ghost"
                onClick={() => {
                  pending.cancel?.();
                  cancelPrompt.current = null;
                  setPending(null);
                }}
              >
                Cancel
              </Button>
              <Button
                type="button"
                autoFocus
                data-testid="archive-worktree-keep"
                componentId="archive.worktree_prompt.archive_only"
                onClick={() => {
                  const current = pending;
                  cancelPrompt.current = null;
                  setPending(null);
                  current.proceed(new Set(current.warnings.map((warning) => warning.id)));
                }}
              >
                Archive only
              </Button>
            </DialogFooter>
          </DialogContent>
        )}
      </Dialog>
    ),
  };
}
