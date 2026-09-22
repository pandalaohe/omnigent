/**
 * Held / pending / queued peer-message records addressed to the open
 * session (received via `sys_session_send` from another Omnigent session —
 * SCC01-S1 peer messaging). Lets the session's user release or refuse a
 * record and set the session's `peer_inbound` policy (accept / hold /
 * refuse). Polls every 5s while open; the sweeper (server-side) can move a
 * record out of these three states between polls, so releasing/refusing an
 * already-resolved one is expected and handled as a refetch, not an error.
 */

import { useCallback, useEffect, useState } from "react";
import { CheckIcon, XIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useSession } from "@/hooks/useSession";
import {
  actOnPeerMessage,
  listPeerMessages,
  updateSession,
  type PeerMessageRecord,
} from "@/lib/sessionsApi";
import { absoluteTime, relativeTime } from "@/lib/relativeTime";

const POLL_INTERVAL_MS = 5000;
const PEER_INBOUND_LABEL_KEY = "peer_inbound";
const HELD_STATES = ["held", "pending", "queued"] as const;

type PeerInboundPolicy = "accept" | "hold" | "refuse";

const STATE_LABEL: Record<string, string> = {
  held: "Held",
  pending: "Pending",
  queued: "Queued",
};

interface PeerHeldPanelProps {
  sessionId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function PeerHeldPanel({ sessionId, open, onOpenChange }: PeerHeldPanelProps) {
  const { session } = useSession(open ? sessionId : null);
  const [records, setRecords] = useState<PeerMessageRecord[]>([]);
  const [loading, setLoading] = useState(false);
  const [actingId, setActingId] = useState<string | null>(null);
  const [policyPending, setPolicyPending] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setRecords(await listPeerMessages(sessionId, [...HELD_STATES]));
    } catch {
      // Transient fetch failure — the next poll tick retries; keep showing
      // the last known list rather than clearing it out from under the user.
    } finally {
      setLoading(false);
    }
  }, [sessionId]);

  useEffect(() => {
    if (!open) return;
    void refresh();
    const id = setInterval(() => void refresh(), POLL_INTERVAL_MS);
    return () => clearInterval(id);
  }, [open, refresh]);

  const act = async (peerId: string, action: "release" | "refuse") => {
    setActingId(peerId);
    // Optimistic: both actions take the record out of held/pending/queued
    // (release moves it toward delivery, refuse retires it), so drop it from
    // the list immediately rather than waiting on the next poll tick.
    setRecords((current) => current.filter((r) => r.id !== peerId));
    try {
      await actOnPeerMessage(sessionId, peerId, action);
    } catch {
      // 409 (resolved elsewhere between polls) or a transient failure —
      // refetch the true list rather than guessing which it was.
      await refresh();
    } finally {
      setActingId(null);
    }
  };

  const policy =
    (session?.labels?.[PEER_INBOUND_LABEL_KEY] as PeerInboundPolicy | undefined) ?? "accept";
  const setPolicy = async (next: PeerInboundPolicy) => {
    setPolicyPending(true);
    try {
      await updateSession(sessionId, {
        labels: { ...(session?.labels ?? {}), [PEER_INBOUND_LABEL_KEY]: next },
      });
    } finally {
      setPolicyPending(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent data-testid="peer-held-panel">
        <DialogHeader>
          <DialogTitle>Peer messages</DialogTitle>
          <DialogDescription>
            Messages from other Omnigent sessions waiting on this session.
          </DialogDescription>
        </DialogHeader>
        <div className="flex items-center justify-between gap-2 border-b pb-3">
          <span className="text-sm text-muted-foreground">Inbound policy</span>
          <Select
            value={policy}
            onValueChange={(next) => void setPolicy(next as PeerInboundPolicy)}
            disabled={policyPending}
            componentId="peer.held_panel.policy"
            valueHasNoPii
          >
            <SelectTrigger className="w-32" data-testid="peer-inbound-policy">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="accept">Accept</SelectItem>
              <SelectItem value="hold">Hold</SelectItem>
              <SelectItem value="refuse">Refuse</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="flex max-h-80 flex-col gap-2 overflow-y-auto">
          {records.length === 0 && !loading && (
            <p className="py-4 text-center text-sm text-muted-foreground">
              No pending peer messages.
            </p>
          )}
          {records.map((record) => (
            <div
              key={record.id}
              data-testid="peer-held-row"
              data-peer-id={record.id}
              data-peer-state={record.state}
              className="flex flex-col gap-1 rounded-md border p-2"
            >
              <div className="flex items-center justify-between gap-2 text-sm">
                <span className="font-medium">{STATE_LABEL[record.state] ?? record.state}</span>
                <span
                  className="text-muted-foreground"
                  title={absoluteTime(record.createdAtS * 1000)}
                >
                  {relativeTime(record.createdAtS * 1000)}
                </span>
              </div>
              <p className="truncate text-sm text-muted-foreground" title={record.text}>
                From {record.senderSessionId.slice(0, 8)} · ref={record.ref}
              </p>
              {record.expiresAtS != null && (
                <p className="text-sm text-muted-foreground">
                  Expires {absoluteTime(record.expiresAtS * 1000)}
                </p>
              )}
              <div className="flex justify-end gap-2">
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  disabled={actingId === record.id}
                  onClick={() => void act(record.id, "refuse")}
                  componentId="peer.held_panel.refuse"
                >
                  <XIcon className="size-3.5" />
                  Refuse
                </Button>
                <Button
                  type="button"
                  size="sm"
                  disabled={actingId === record.id}
                  onClick={() => void act(record.id, "release")}
                  componentId="peer.held_panel.release"
                >
                  <CheckIcon className="size-3.5" />
                  Release
                </Button>
              </div>
            </div>
          ))}
        </div>
      </DialogContent>
    </Dialog>
  );
}
