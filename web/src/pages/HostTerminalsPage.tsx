import { useState } from "react";
import { ArrowLeftIcon, ChevronDownIcon, PlusIcon, XIcon } from "lucide-react";
import { TerminalView } from "@/components/blocks/TerminalView";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useHostTerminals, type HostTerminal } from "@/hooks/useHostTerminals";
import { useHosts } from "@/hooks/useHosts";
import { useIsAdmin } from "@/hooks/useIsAdmin";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { Link, useParams } from "@/lib/routing";
import { ApiError } from "@/lib/sessionsApi";
import { cn } from "@/lib/utils";

export function HostTerminalsPage() {
  const { hostId = "" } = useParams();
  return <HostTerminals key={hostId} hostId={hostId} />;
}

function HostTerminals({ hostId }: { hostId: string }) {
  const hosts = useHosts();
  const host = hosts.data?.find((item) => item.host_id === hostId);
  const available =
    host?.status === "online" && host.capabilities?.includes("host_terminals_v1") === true;
  const isAdmin = useIsAdmin();
  const isMobile = useIsMobileViewport();
  const { terminals, create, close } = useHostTerminals(hostId, available);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [closing, setClosing] = useState<HostTerminal | null>(null);
  const items = terminals.data ?? [];
  const activeId =
    items.find((item) => item.terminal_id === selectedId)?.terminal_id ?? items[0]?.terminal_id;
  const canElevate = available && isAdmin && host.capabilities?.includes("terminal_elevated_v1");
  const denied =
    terminals.error instanceof ApiError && [401, 403, 404].includes(terminals.error.status);
  const label = (terminal: HostTerminal) =>
    `${terminal.elevated ? "Admin" : "Shell"} · ${terminal.terminal_id.slice(0, 6)}`;

  function open(elevated: boolean) {
    close.reset();
    create.mutate(elevated, { onSuccess: (terminal) => setSelectedId(terminal.terminal_id) });
  }

  let unavailable: string | null = null;
  if (hosts.isPending) unavailable = "Loading host…";
  else if (hosts.isError) unavailable = "Unable to load your hosts. Try again.";
  else if (!host) unavailable = "Host not found or you do not own this host.";
  else if (host.status !== "online")
    unavailable = "This host is offline. Terminals will be available when it reconnects.";
  else if (!available) unavailable = "This host does not support host terminals.";

  return (
    <main
      className={cn(
        "flex min-h-0 min-w-0 flex-1 flex-col bg-background",
        isMobile && "fixed inset-0 z-40 h-dvh",
      )}
      style={{
        paddingTop: isMobile
          ? "var(--omnigent-inset-top)"
          : "calc(var(--omnigent-header-height) + var(--omnigent-inset-top))",
        paddingBottom: "var(--omnigent-inset-bottom)",
      }}
    >
      <header className="flex shrink-0 items-center gap-3 border-b px-3 py-2">
        <Button asChild variant="ghost" size="icon">
          <Link to="/" aria-label="Back to sessions">
            <ArrowLeftIcon aria-hidden="true" />
          </Link>
        </Button>
        <h1 className="min-w-0 flex-1 truncate text-ui font-semibold">
          {host?.name || hostId} · Terminals
        </h1>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              variant="outline"
              disabled={!available || create.isPending}
              className="max-md:min-h-10"
            >
              <PlusIcon aria-hidden="true" /> New terminal <ChevronDownIcon aria-hidden="true" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem onSelect={() => open(false)}>Shell</DropdownMenuItem>
            {canElevate && <DropdownMenuItem onSelect={() => open(true)}>Admin</DropdownMenuItem>}
          </DropdownMenuContent>
        </DropdownMenu>
      </header>
      {unavailable ? (
        <div className="p-4">
          <p role="status">{unavailable}</p>
          {hosts.isError && <Button onClick={() => void hosts.refetch()}>Retry</Button>}
        </div>
      ) : (
        <>
          {terminals.isError && (
            <div
              role="alert"
              className="flex shrink-0 items-center gap-2 px-4 py-2 text-ui text-destructive"
            >
              <span>{terminals.error.message}</span>
              <Button variant="outline" size="sm" onClick={() => void terminals.refetch()}>
                Retry
              </Button>
            </div>
          )}
          {(create.isError || close.isError) && (
            <p role="alert" className="shrink-0 px-4 py-2 text-ui text-destructive">
              {create.error?.message ?? close.error?.message}
            </p>
          )}
          {terminals.isPending ? (
            <p role="status" className="p-4 text-ui">
              Loading terminals…
            </p>
          ) : denied || (terminals.isError && items.length === 0) ? null : items.length === 0 ? (
            <div className="flex flex-1 flex-col items-center justify-center gap-3 p-6 text-center text-ui">
              <p>No open terminals. Start a shell in your home directory.</p>
              <Button disabled={create.isPending} onClick={() => open(false)}>
                Open terminal
              </Button>
            </div>
          ) : (
            <Tabs
              value={activeId}
              onValueChange={setSelectedId}
              className="min-h-0 min-w-0 flex-1 gap-0"
            >
              <div className="shrink-0 overflow-x-auto border-b">
                <TabsList
                  variant="pill"
                  aria-label="Host terminals"
                  className="min-w-max justify-start p-1"
                >
                  {items.map((terminal) => (
                    <span key={terminal.terminal_id} className="flex shrink-0 items-center">
                      <TabsTrigger
                        value={terminal.terminal_id}
                        className={cn(
                          "min-h-10 px-3",
                          terminal.elevated && "text-destructive data-active:text-destructive",
                        )}
                      >
                        {label(terminal)}
                      </TabsTrigger>
                      <Button
                        variant="ghost"
                        size="icon"
                        aria-label={`Close ${label(terminal)}`}
                        onClick={() => {
                          close.reset();
                          setClosing(terminal);
                        }}
                      >
                        <XIcon aria-hidden="true" />
                      </Button>
                    </span>
                  ))}
                </TabsList>
              </div>
              {items.map((terminal) => (
                <TabsContent
                  key={terminal.terminal_id}
                  value={terminal.terminal_id}
                  forceMount
                  className="relative flex min-h-0 flex-col overflow-hidden data-[state=inactive]:hidden"
                >
                  <TerminalView
                    hostId={hostId}
                    terminalId={terminal.terminal_id}
                    active={activeId === terminal.terminal_id}
                  />
                </TabsContent>
              ))}
            </Tabs>
          )}
        </>
      )}
      <Dialog
        open={closing !== null}
        onOpenChange={(value) => {
          if (!value && !close.isPending) setClosing(null);
        }}
      >
        <DialogContent showCloseButton={!close.isPending}>
          <DialogHeader>
            <DialogTitle>Close {closing ? label(closing) : "terminal"}?</DialogTitle>
            <DialogDescription>
              This ends the shell and any commands running in this terminal.
            </DialogDescription>
          </DialogHeader>
          {close.isError && (
            <p role="alert" className="text-ui text-destructive">
              {close.error.message}
            </p>
          )}
          <DialogFooter>
            <Button variant="outline" disabled={close.isPending} onClick={() => setClosing(null)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={close.isPending || !available}
              onClick={() => {
                if (closing)
                  close.mutate(closing.terminal_id, { onSuccess: () => setClosing(null) });
              }}
            >
              Close terminal
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </main>
  );
}
