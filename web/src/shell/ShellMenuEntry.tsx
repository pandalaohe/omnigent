import { useState } from "react";
import type { ReactNode } from "react";
import { TerminalIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Segmented } from "@/components/ui/segmented";
import { showToast } from "@/components/ui/toast";
import { DropdownMenuItem } from "@/components/ui/dropdown-menu";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { useIsPhone } from "@/hooks/useIsPhone";
import { useSessionAgent } from "@/hooks/useAgents";
import { useHosts } from "@/hooks/useHosts";
import { useIsAdmin } from "@/hooks/useIsAdmin";
import { useSession } from "@/hooks/useSession";
import { useCreateLinkedTerminal } from "@/hooks/useLinkedTerminals";
import { terminalTabKey, useCreateTerminal } from "@/hooks/useTerminals";
import type { AgentTier } from "@/lib/terminals";
import { readPreferredShell, resolveDefaultShell, writePreferredShell } from "./preferredShell";

const tierLabels = { off: "Off", read: "Read", read_write: "Read & write" } as const;

function ShellOptions({
  conversationId,
  onCreated,
  onDone,
}: {
  conversationId: string;
  onCreated?: (key: string) => void;
  onDone: () => void;
}) {
  const { session } = useSession(conversationId);
  const { data: hosts = [] } = useHosts();
  const { data: agent } = useSessionAgent(conversationId);
  const isAdmin = useIsAdmin();
  const local = useCreateTerminal(conversationId);
  const linked = useCreateLinkedTerminal(conversationId);
  const shells = agent?.terminals ?? [];
  const [hostId, setHostId] = useState(session?.hostId ?? "");
  const [shell, setShell] = useState(resolveDefaultShell(shells, readPreferredShell()) ?? "");
  const [admin, setAdmin] = useState(false);
  const [tier, setTier] = useState<AgentTier>("read_write");
  const [error, setError] = useState<string | null>(null);
  const host = hosts.find((h) => h.host_id === hostId);
  const isLocal = hostId === (session?.hostId ?? "");
  const reason =
    !host || host.status !== "online"
      ? "This host is offline."
      : !isAdmin
        ? "Administrator access is required."
        : !host.capabilities?.includes("terminal_elevated_v1")
          ? "Admin terminals are not enabled on this host."
          : null;
  const linkSupported = isLocal || host?.capabilities?.includes("host_terminal_links_v1");
  const pending = local.isPending || linked.isPending;
  const open = async () => {
    setError(null);
    try {
      const terminal = isLocal
        ? await local.mutateAsync({ terminal: shell, elevated: admin, agentTier: tier })
        : await linked.mutateAsync({ host_id: hostId, elevated: admin, agent_tier: tier });
      if (isLocal) writePreferredShell(shell);
      onCreated?.(terminalTabKey(terminal));
      onDone();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Couldn't open the shell.");
    }
  };
  return (
    <div className="space-y-3 p-1 text-sm">
      <h3 className="font-semibold">Shell options</h3>
      <label className="flex items-center justify-between gap-4">
        Host
        <select
          aria-label="Host"
          value={hostId}
          className="min-w-0 max-w-56 rounded-md border bg-background px-2 py-1.5"
          onChange={(e) => {
            setHostId(e.target.value);
            setAdmin(false);
            setTier("read_write");
          }}
        >
          {!hosts.some((h) => h.host_id === session?.hostId) && (
            <option value={session?.hostId ?? ""}>Session host</option>
          )}
          {hosts.map((h) => (
            <option
              key={h.host_id}
              value={h.host_id}
              disabled={
                h.status !== "online" ||
                (h.host_id !== session?.hostId &&
                  !h.capabilities?.includes("host_terminal_links_v1"))
              }
            >
              {h.name}
              {h.host_id === session?.hostId ? " · session host" : ""}
              {h.status !== "online"
                ? " · offline"
                : !h.capabilities?.includes("host_terminal_links_v1") &&
                    h.host_id !== session?.hostId
                  ? " · update the host"
                  : ""}
            </option>
          ))}
        </select>
      </label>
      {isLocal ? (
        <label className="flex items-center justify-between gap-4">
          Shell
          <select
            aria-label="Shell"
            value={shell}
            onChange={(e) => setShell(e.target.value)}
            className="rounded-md border bg-background px-2 py-1.5"
          >
            {shells.map((name) => (
              <option key={name}>{name}</option>
            ))}
          </select>
        </label>
      ) : (
        <p className="text-muted-foreground">Uses this host's default shell.</p>
      )}
      <label className="flex items-center justify-between gap-4">
        Administrator
        <input
          type="checkbox"
          checked={admin}
          disabled={Boolean(reason)}
          onChange={(e) => {
            setAdmin(e.target.checked);
            setTier(e.target.checked ? "off" : "read_write");
          }}
        />
      </label>
      {reason && <p className="text-xs text-muted-foreground">{reason}</p>}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span>Agent access</span>
        <Segmented
          label="Initial agent access"
          value={tier}
          onChange={setTier}
          options={(["off", "read", "read_write"] as const).map((value) => ({
            value,
            label: tierLabels[value],
          }))}
        />
      </div>
      {admin && tier === "read_write" && (
        <p className="text-destructive">The agent can run administrator commands.</p>
      )}
      {!linkSupported && (
        <p className="text-muted-foreground">Update the host to open terminals here.</p>
      )}
      {error && (
        <p role="alert" className="text-destructive">
          {error}
        </p>
      )}
      <Button
        className="w-full"
        disabled={
          pending ||
          !linkSupported ||
          host?.status === "offline" ||
          (admin && Boolean(reason)) ||
          (isLocal && !shell)
        }
        onClick={() => void open()}
      >
        {pending ? "Opening…" : "Open shell"}
      </Button>
    </div>
  );
}

export function ShellMenuEntry({
  conversationId,
  onCreated,
  disabled,
  status,
  shortcut,
  onDefaultOpen,
}: {
  conversationId: string;
  onCreated?: (key: string) => void;
  disabled?: boolean;
  status?: string;
  shortcut?: ReactNode;
  onDefaultOpen?: (shell: string) => void;
}) {
  const [options, setOptions] = useState(false);
  const isPhone = useIsPhone();
  const { data: agent } = useSessionAgent(conversationId);
  const create = useCreateTerminal(conversationId);
  const shell = resolveDefaultShell(agent?.terminals ?? [], readPreferredShell());
  if (!shell) return null;
  return (
    <div className="flex items-center rounded-sm">
      <DropdownMenuItem
        disabled={disabled || create.isPending}
        className="min-w-0 flex-1 cursor-pointer"
        onSelect={() => {
          if (onDefaultOpen) onDefaultOpen(shell);
          else
            create.launch(shell, {
              onOpen: (t) => onCreated?.(terminalTabKey(t)),
              onError: (e) => showToast(`Couldn't open a shell: ${e.message}`, { duration: 0 }),
            });
        }}
      >
        <TerminalIcon className="size-4" />
        {status === "Reconnecting…" ? status : `Shell (${shell})`}
        {status && status !== "Reconnecting…" && (
          <span className="ml-auto pl-3 text-muted-foreground">{status}</span>
        )}
        {shortcut && <span className="ml-auto pl-3">{shortcut}</span>}
      </DropdownMenuItem>
      {isPhone ? (
        <Dialog open={options} onOpenChange={setOptions}>
          <DialogTrigger asChild>
            <Button size="sm" variant="ghost" aria-label="Shell options">
              Options ›
            </Button>
          </DialogTrigger>
          <DialogContent
            className="max-h-[85dvh] translate-y-0 overflow-y-auto rounded-b-none rounded-t-2xl"
            style={{ top: "auto", bottom: 0 }}
          >
            <DialogTitle className="sr-only">Shell options</DialogTitle>
            <DialogDescription className="sr-only">
              Choose where to open a shell and how the agent can use it.
            </DialogDescription>
            <ShellOptions
              conversationId={conversationId}
              onCreated={onCreated}
              onDone={() => setOptions(false)}
            />
          </DialogContent>
        </Dialog>
      ) : (
        <Popover open={options} onOpenChange={setOptions}>
          <PopoverTrigger asChild>
            <Button
              size="sm"
              variant="ghost"
              aria-label="Shell options"
              className="shrink-0 text-muted-foreground"
            >
              Options ›
            </Button>
          </PopoverTrigger>
          <PopoverContent
            align="end"
            side="right"
            className="w-80 max-w-[calc(100vw-1rem)]"
            onCloseAutoFocus={(e) => e.preventDefault()}
          >
            {options && (
              <ShellOptions
                conversationId={conversationId}
                onCreated={onCreated}
                onDone={() => setOptions(false)}
              />
            )}
          </PopoverContent>
        </Popover>
      )}
    </div>
  );
}
