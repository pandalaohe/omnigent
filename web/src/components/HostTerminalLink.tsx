import { TerminalIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { DropdownMenuItem } from "@/components/ui/dropdown-menu";
import type { Host } from "@/hooks/useHosts";
import { Link } from "@/lib/routing";

/** Callers pass only hosts returned by the owner-scoped /v1/hosts endpoint. */
export function HostTerminalLink({ host, inMenu = false }: { host: Host; inMenu?: boolean }) {
  if (host.status !== "online" || !host.capabilities?.includes("host_terminals_v1")) {
    return null;
  }
  const link = (
    <Link to={`/hosts/${encodeURIComponent(host.host_id)}/terminals`}>
      <TerminalIcon className="size-3.5" aria-hidden="true" />
      Terminal
    </Link>
  );
  return inMenu ? (
    <DropdownMenuItem asChild className="shrink-0 max-md:min-h-10">
      {link}
    </DropdownMenuItem>
  ) : (
    <Button asChild variant="outline" size="sm" className="max-md:min-h-10">
      {link}
    </Button>
  );
}
