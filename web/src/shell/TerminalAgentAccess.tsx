import { useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Switch } from "@/components/ui/switch";
import { useSessionTerminalAccess } from "@/hooks/useSessionTerminalAccess";
import { useTerminalAgentAccess, type TerminalInfo } from "@/hooks/useTerminals";

export function TerminalAgentAccess({
  conversationId,
  terminal,
}: {
  conversationId: string;
  terminal: TerminalInfo;
}) {
  const context = useSessionTerminalAccess(conversationId);
  const update = useTerminalAgentAccess(conversationId);
  const [confirmation, setConfirmation] = useState<{ instanceId: string; hostId: string } | null>(
    null,
  );
  if (!terminal.elevated) return null;
  const enabled = terminal.agentAccess === true;
  const canChange =
    context.canManageAgentAccess && terminal.running && Boolean(terminal.instanceId);
  const currentConfirmation =
    confirmation !== null &&
    confirmation.instanceId === terminal.instanceId &&
    confirmation.hostId === context.host?.host_id;
  const change = (next: boolean) => {
    if (!canChange || !terminal.instanceId || !context.host || update.isPending) return;
    if (next) setConfirmation({ instanceId: terminal.instanceId, hostId: context.host.host_id });
    else
      update.mutate({ terminalId: terminal.id, instanceId: terminal.instanceId, enabled: false });
  };
  return (
    <div className="shrink-0 border-b px-3 py-2 text-sm">
      <label className="flex items-center gap-2 max-md:min-h-11">
        <span className="font-medium text-destructive">root</span>
        <Switch
          aria-label="Agent access to root terminal"
          checked={enabled}
          disabled={!canChange || update.isPending}
          onCheckedChange={change}
        />
        Agent access: {enabled ? "On" : "Off"}
      </label>
      {!context.canManageAgentAccess && (
        <p className="text-muted-foreground">Only the host owner can change agent access.</p>
      )}
      {update.isError && (
        <p role="alert" className="text-destructive">
          {update.error.message}
        </p>
      )}
      <Dialog
        open={currentConfirmation}
        onOpenChange={(open) => {
          if (!open && !update.isPending) setConfirmation(null);
        }}
      >
        <DialogContent showCloseButton={!update.isPending}>
          <DialogHeader>
            <DialogTitle>Allow agent access to this root terminal?</DialogTitle>
            <DialogDescription>
              Allow {context.session?.agentName || context.session?.agentId || "this agent"} in{" "}
              {context.session?.title || conversationId} to read and type root commands in terminal{" "}
              {terminal.session || terminal.id} on {context.host?.name || context.host?.host_id}.
              Access lasts until this terminal closes or you turn it off.
            </DialogDescription>
          </DialogHeader>
          {update.isError && (
            <p role="alert" className="text-destructive">
              {update.error.message}
            </p>
          )}
          <DialogFooter>
            <Button
              variant="outline"
              className="max-md:min-h-11"
              disabled={update.isPending}
              onClick={() => setConfirmation(null)}
            >
              Cancel
            </Button>
            <Button
              disabled={!canChange || update.isPending || enabled}
              className="max-md:min-h-11"
              onClick={() => {
                if (!currentConfirmation || !confirmation || !terminal.instanceId) return;
                update.mutate(
                  { terminalId: terminal.id, instanceId: confirmation.instanceId, enabled: true },
                  { onSuccess: () => setConfirmation(null) },
                );
              }}
            >
              Allow agent access
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
