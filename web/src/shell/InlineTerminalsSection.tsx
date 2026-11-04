// Shells tab content for the right-side rail: the session's shells as
// rows. Clicking a shell row hands it to `onExpand`, which opens the
// shell as a rail tab. On desktop, creating a new shell is done from the
// tab strip's "+" menu (see NewTabMenu), so no create affordance shows
// here. On mobile (no tab strip) the drawer passes ``showNewShell`` to
// surface a leading "+ New shell" row as the create entry point.

import { TerminalIcon, XIcon } from "lucide-react";
import { useMemo, useState } from "react";
import {
  inventoryTerminals,
  terminalTabKey,
  terminalDisplayLabel,
  useTerminals,
  useDeleteTerminal,
  type TerminalInfo,
} from "@/hooks/useTerminals";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import { NewTerminalButton } from "./NewTerminalButton";
import { useTerminalFirst } from "./TerminalFirstContext";
import { TerminalStatusBadge } from "./terminalStatus";
import { useTerminalStatuses } from "./useTerminalStatuses";

interface InlineTerminalsSectionProps {
  conversationId: string;
  /** Open a shell in the main view, keyed by its terminal tab key. */
  onExpand: (terminalKey: string) => void;
  /** Show a leading "+ New shell" create row. Off by default — the desktop
   *  rail creates shells from the tab strip's "+" menu. Set on mobile, where
   *  there's no tab strip, so the drawer stays a usable create entry point. */
  showNewShell?: boolean;
  canClose?: boolean;
}

export function InlineTerminalsSection({
  conversationId,
  onExpand,
  showNewShell = false,
  canClose = true,
}: InlineTerminalsSectionProps) {
  const { terminals: allTerminals } = useTerminals(conversationId);
  const close = useDeleteTerminal(conversationId);
  const [closing, setClosing] = useState<TerminalInfo | null>(null);
  // Inventory view: the agent's own terminal (SDK REPL / native vendor
  // pane) backs the pill's Terminal view and must not appear as a
  // shell row here.
  const terminalFirstCtx = useTerminalFirst();
  const terminals = useMemo(
    () => inventoryTerminals(allTerminals, terminalFirstCtx?.isTerminalFirst ?? false),
    [allTerminals, terminalFirstCtx?.isTerminalFirst],
  );
  const { getStatus } = useTerminalStatuses(terminals);

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden bg-card">
      {/* Plain top-aligned list of the session's shells. A leading
          "+ New shell" row (mobile only, gated inside NewTerminalButton on the
          agent's terminal access) keeps the create entry point reachable where
          there's no tab strip "+" menu. */}
      <div className="flex min-h-0 flex-1 flex-col overflow-y-auto py-1">
        {showNewShell && (
          <NewTerminalButton conversationId={conversationId} onCreated={onExpand} variant="row" />
        )}
        {terminals.map((t) => (
          <div key={terminalTabKey(t)} className="flex items-center gap-1">
            <button
              type="button"
              aria-label={`Open ${terminalDisplayLabel(t)}`}
              className="flex min-h-11 min-w-0 flex-1 items-center gap-2 px-2 py-1.5 text-left hover:bg-accent/60"
              onClick={() => onExpand(terminalTabKey(t))}
            >
              <TerminalIcon className="size-3.5 shrink-0 text-muted-foreground" />
              <span className="min-w-0 flex-1">
                <span className="block truncate text-ui">{terminalDisplayLabel(t)}</span>
                {(t.origin === "user" || t.origin === "agent") && (
                  <span className="block truncate text-sm text-muted-foreground">
                    {t.origin === "user" ? "Opened by you" : "Opened by the agent"}
                  </span>
                )}
              </span>
              <TerminalStatusBadge status={getStatus(t)} />
            </button>
            {canClose && (
              <Button
                variant="ghost"
                size="icon"
                className="min-h-11 min-w-11"
                aria-label={`Close ${terminalDisplayLabel(t)}`}
                onClick={() => {
                  close.reset();
                  setClosing(t);
                }}
              >
                <XIcon className="size-4" />
              </Button>
            )}
          </div>
        ))}
      </div>
      <Dialog
        open={closing !== null}
        onOpenChange={(open) => {
          if (!open && !close.isPending) setClosing(null);
        }}
      >
        <DialogContent showCloseButton={!close.isPending}>
          <DialogHeader>
            <DialogTitle>Close {closing ? terminalDisplayLabel(closing) : "shell"}?</DialogTitle>
            <DialogDescription>
              This ends the shell and any commands running in it.
            </DialogDescription>
          </DialogHeader>
          {close.isError && (
            <p role="alert" className="text-sm text-destructive">
              {close.error?.message}
            </p>
          )}
          <DialogFooter>
            <Button variant="outline" disabled={close.isPending} onClick={() => setClosing(null)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={close.isPending || !canClose}
              onClick={() => {
                if (closing) close.mutate(closing.id, { onSuccess: () => setClosing(null) });
              }}
            >
              Close shell
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
