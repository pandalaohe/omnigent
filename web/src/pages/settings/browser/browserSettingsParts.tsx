import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { toast } from "sonner";
import { SearchIcon, MoreHorizontalIcon, MonitorIcon, ServerIcon } from "lucide-react";
import { SettingsGroup } from "@/components/SettingsGroup";
import { Button } from "@/components/ui/button";
import { InputGroup, InputGroupInput, InputGroupAddon } from "@/components/ui/input-group";
import {
  Dialog,
  DialogContent,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
} from "@/components/ui/dropdown-menu";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import {
  browserPlacesBridge,
  type PlacesResult,
  type BrowserPlace,
} from "@/lib/browserPlacesBridge";
import { getSettingsReturnPath } from "@/shell/settingsNav";
import { useNavigate } from "@/lib/routing";
import { openUrlInSessionBrowser } from "@/lib/openLinkInApp";

export function ScopeChip({ server = false }: { server?: boolean }) {
  const Icon = server ? ServerIcon : MonitorIcon;
  return (
    <span
      title={
        server
          ? "Each connected server keeps its own copy"
          : "Shared by every server on this desktop"
      }
      className="inline-flex items-center gap-1 rounded-md bg-muted px-2 py-1 text-xs font-normal text-muted-foreground"
    >
      <Icon className="size-3" />
      {server ? `This server · ${window.location.host}` : "This desktop"}
    </span>
  );
}
export function BrowserGroup({
  title,
  server = false,
  wide = false,
  children,
}: {
  title: string;
  server?: boolean;
  wide?: boolean;
  children: ReactNode;
}) {
  return (
    <SettingsGroup title={title} className={wide ? "max-w-none" : undefined}>
      <div className="mb-4 flex justify-end">
        <ScopeChip server={server} />
      </div>
      {children}
    </SettingsGroup>
  );
}
export function UpdateReason({ verb }: { verb: string }) {
  return (
    <p className="text-sm text-muted-foreground">
      Update the desktop app to {verb}.{" "}
      <a className="underline" href="/settings/updates">
        Check for updates
      </a>
    </p>
  );
}
export function Confirm({
  title,
  description,
  action,
  open,
  onClose,
  onConfirm,
  busy = false,
  children,
}: {
  title: string;
  description: string;
  action: string;
  open: boolean;
  onClose: () => void;
  onConfirm: () => void;
  busy?: boolean;
  children?: ReactNode;
}) {
  return (
    <Dialog
      open={open}
      onOpenChange={(value) => {
        if (!value && !busy) onClose();
      }}
    >
      <DialogContent>
        <DialogTitle>{title}</DialogTitle>
        <DialogDescription>{description}</DialogDescription>
        {children}
        <DialogFooter>
          <Button variant="outline" disabled={busy} onClick={onClose}>
            Cancel
          </Button>
          <Button variant="destructive" disabled={busy} onClick={onConfirm}>
            {action}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
export function SearchPages({
  query,
  setQuery,
  total,
  count,
  kind,
}: {
  query: string;
  setQuery: (value: string) => void;
  total: number;
  count: number;
  kind: string;
}) {
  return (
    <div className="flex min-w-0 flex-1 flex-wrap items-center gap-3">
      <InputGroup className="min-w-40 max-w-sm">
        <InputGroupAddon>
          <SearchIcon />
        </InputGroupAddon>
        <InputGroupInput
          aria-label={`Search ${kind}`}
          placeholder={`Search ${kind}`}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        {query && (
          <InputGroupAddon align="inline-end">
            <Button variant="ghost" size="sm" onClick={() => setQuery("")}>
              Clear search
            </Button>
          </InputGroupAddon>
        )}
      </InputGroup>
      <span className="text-xs text-muted-foreground">
        {query ? `${count} of ${total}` : total} pages
      </span>
    </div>
  );
}
export function Highlight({ text, query }: { text: string; query: string }) {
  const index = query ? text.toLowerCase().indexOf(query.toLowerCase()) : -1;
  return index < 0 ? (
    text
  ) : (
    <>
      {text.slice(0, index)}
      <mark>{text.slice(index, index + query.length)}</mark>
      {text.slice(index + query.length)}
    </>
  );
}
export function PageTile({ url }: { url: string }) {
  const host = new URL(url).hostname;
  const hue = [...host].reduce((n, c) => n + c.charCodeAt(0), 0) % 360;
  return (
    <span
      aria-hidden
      className="grid size-9 shrink-0 place-items-center rounded-lg text-sm font-semibold"
      style={{ backgroundColor: `hsl(${hue} 35% 65% / .15)`, color: `hsl(${hue} 45% 48%)` }}
    >
      {host[0]?.toUpperCase()}
    </span>
  );
}
export function RowActions({
  actions,
}: {
  actions: { label: string; icon: ReactNode; run: () => void; disabled?: boolean }[];
}) {
  const mobile = useIsMobileViewport();
  return mobile ? (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon" aria-label="Page actions">
          <MoreHorizontalIcon className="size-4" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent>
        {actions.map((action) => (
          <DropdownMenuItem key={action.label} disabled={action.disabled} onSelect={action.run}>
            {action.icon}
            {action.label}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  ) : (
    <div className="flex shrink-0 opacity-0 group-hover:opacity-100 group-focus-within:opacity-100">
      {actions.map((action) => (
        <Button
          key={action.label}
          variant="ghost"
          size="icon"
          aria-label={action.label}
          title={action.label}
          disabled={action.disabled}
          onClick={action.run}
        >
          {action.icon}
        </Button>
      ))}
    </div>
  );
}
export function useOpenPage() {
  const navigate = useNavigate();
  return (url: string) => {
    const path = getSettingsReturnPath(),
      match = path.match(/^\/c\/([^/?#]+)/);
    if (match) {
      navigate(path);
      void openUrlInSessionBrowser(match[1], url);
    } else window.open(url, "_blank", "noopener,noreferrer");
  };
}
export function usePlaces() {
  const bridge = useMemo(browserPlacesBridge, []);
  const [history, setHistory] = useState<BrowserPlace[]>([]),
    [bookmarks, setBookmarks] = useState<BrowserPlace[]>([]),
    [busy, setBusy] = useState(false),
    [message, setMessage] = useState("");
  const refresh = useCallback(async () => {
    if (!bridge) return;
    const result = await bridge.browserPlaces({ action: "list" });
    if (!result.ok) throw new Error(result.error);
    setHistory(result.history ?? []);
    setBookmarks(result.bookmarks ?? []);
  }, [bridge]);
  useEffect(() => {
    void refresh().catch(() => setMessage("Could not read saved pages."));
  }, [refresh]);
  async function run(args: Record<string, unknown>): Promise<PlacesResult> {
    if (!bridge) return { ok: false };
    setBusy(true);
    try {
      const result = await bridge.browserPlaces(args);
      if (!result.ok) {
        setMessage(result.error ?? "Could not save changes.");
        return result;
      }
      await refresh();
      setMessage("Saved");
      return result;
    } catch {
      setMessage("Could not save changes.");
      return { ok: false };
    } finally {
      setBusy(false);
    }
  }
  return {
    history,
    bookmarks,
    busy,
    message,
    run,
    bridge,
    has: (feature: string) => bridge?.browserPlacesFeatures?.includes(feature) === true,
  };
}
export function undoToast(message: string, undo: () => Promise<unknown>) {
  toast(message, {
    duration: 10000,
    action: {
      label: "Undo",
      onClick: () => {
        void undo().catch(() => toast.error("Could not restore."));
      },
    },
  });
}
export async function copyPage(url: string) {
  try {
    await navigator.clipboard.writeText(url);
    toast("Link copied.");
  } catch {
    toast.error("Could not copy link.");
  }
}
