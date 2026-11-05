import { TerminalIcon } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { useHosts } from "@/hooks/useHosts";
import { findAgentTerminal, terminalTabKey, useTerminals } from "@/hooks/useTerminals";
import { useSessionTerminalAccess } from "@/hooks/useSessionTerminalAccess";
import { isEditorLevel } from "@/lib/permissionsApi";
import { useLocation, useNavigate } from "@/lib/routing";
import { InlineTerminalsSection } from "./InlineTerminalsSection";
import { useTerminalFirst } from "./TerminalFirstContext";

export function TerminalsSheet({
  open,
  onOpenChange,
  conversationId,
  onSelectTerminal,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  conversationId?: string;
  onSelectTerminal: (key: string) => void;
}) {
  const { data: hosts, isPending, isError } = useHosts({ enabled: open });
  const navigate = useNavigate();
  const location = useLocation();
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="max-h-[85dvh] overflow-y-auto sm:max-w-lg"
        data-testid="terminals-sheet"
      >
        <DialogHeader>
          <DialogTitle>Terminals</DialogTitle>
          <DialogDescription>
            Open a session terminal or a terminal on your hosts.
          </DialogDescription>
        </DialogHeader>
        {open && (
          <>
            {conversationId && (
              <SessionTerminals
                conversationId={conversationId}
                onSelect={(key) => {
                  onSelectTerminal(key);
                  onOpenChange(false);
                }}
              />
            )}
            <section aria-label="Hosts">
              <h3 className="mb-2 text-ui font-semibold">Hosts</h3>
              {isPending && (
                <p role="status" className="text-sm text-muted-foreground">
                  Loading hosts…
                </p>
              )}
              {isError && (
                <p role="alert" className="text-sm text-destructive">
                  Unable to load hosts.
                </p>
              )}
              {hosts
                ?.filter((host) => host.capabilities?.includes("host_terminals_v1"))
                .map((host) => (
                  <button
                    key={host.host_id}
                    type="button"
                    disabled={host.status !== "online"}
                    className="flex min-h-11 w-full items-center gap-2 border-b border-border py-2 text-left text-ui disabled:opacity-50"
                    onClick={() => {
                      onOpenChange(false);
                      navigate(`/hosts/${encodeURIComponent(host.host_id)}/terminals`, {
                        state: { returnTo: location.pathname + location.search + location.hash },
                      });
                    }}
                  >
                    <TerminalIcon className="size-4 shrink-0" />
                    <span className="min-w-0 flex-1 truncate">{host.name}</span>
                    {host.status !== "online" && (
                      <span className="text-sm text-muted-foreground">Offline</span>
                    )}
                  </button>
                ))}
              {!isPending &&
                !isError &&
                !hosts?.some((host) => host.capabilities?.includes("host_terminals_v1")) && (
                  <p className="text-sm text-muted-foreground">No hosts support terminals.</p>
                )}
            </section>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}

function SessionTerminals({
  conversationId,
  onSelect,
}: {
  conversationId: string;
  onSelect: (key: string) => void;
}) {
  const { terminals } = useTerminals(conversationId);
  const { session } = useSessionTerminalAccess(conversationId);
  const agentTerminal = findAgentTerminal(terminals);
  const terminalFirst = useTerminalFirst();
  return (
    <section aria-label="This session">
      <h3 className="mb-2 text-ui font-semibold">This session</h3>
      {(agentTerminal || terminalFirst?.isTerminalFirst) && (
        <button
          type="button"
          className="flex min-h-11 w-full items-center gap-2 border-b border-border py-2 text-left text-ui"
          onClick={() => onSelect(agentTerminal ? terminalTabKey(agentTerminal) : "")}
        >
          <TerminalIcon className="size-4" /> Session terminal
        </button>
      )}
      <InlineTerminalsSection
        conversationId={conversationId}
        onExpand={onSelect}
        showNewShell
        canClose={isEditorLevel(session?.permissionLevel ?? null)}
      />
    </section>
  );
}
