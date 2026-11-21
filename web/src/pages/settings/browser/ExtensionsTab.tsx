import { useEffect, useMemo, useState } from "react";
import { CheckIcon, MoreHorizontalIcon, PuzzleIcon, XIcon } from "lucide-react";
import { browserLoginBridge } from "@/lib/nativeBridge";
import { SettingsPager } from "@/components/SettingsPager";
import { usePagedRows } from "@/hooks/usePagedRows";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { Input } from "@/components/ui/input";
import { TooltipProvider } from "@/components/ui/tooltip";
import { StatusIcon } from "@/components/ui/status-icon";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { BrowserGroup, Confirm, UpdateReason } from "./browserSettingsParts";

interface Extension {
  id: string;
  name: string;
  version: string;
  enabled: boolean;
  runtimeId?: string;
}
export function ExtensionsTab() {
  const bridge = useMemo(browserLoginBridge, []);
  const [rows, setRows] = useState<Extension[]>([]),
    [login, setLogin] = useState<boolean | null>(null),
    [busy, setBusy] = useState(false),
    [query, setQuery] = useState(""),
    [status, setStatus] = useState(""),
    [removing, setRemoving] = useState<Extension | null>(null);
  const filter = query.trim().toLowerCase();
  const visibleRows = useMemo(
    () =>
      rows.filter((extension) =>
        `${extension.name} ${extension.id} ${extension.runtimeId ?? ""}`
          .toLowerCase()
          .includes(filter),
      ),
    [rows, filter],
  );
  const { pageRows, ...pager } = usePagedRows(visibleRows, { resetKey: filter });
  const supported =
    !!bridge?.extensionsList &&
    !!bridge.extensionsAdd &&
    !!bridge.extensionsEnable &&
    !!bridge.extensionsRemove;
  useEffect(() => {
    void bridge
      ?.get()
      .then((result) => {
        if (result.ok) setLogin(result.enabled === true);
      })
      .catch(() => setStatus("Could not read browser sign-ins."));
    void bridge
      ?.extensionsList?.()
      .then((result) => {
        if (result.ok) setRows(result.extensions ?? []);
        else setStatus("Could not list extensions.");
      })
      .catch(() => setStatus("Could not list extensions."));
  }, [bridge]);
  async function run(operation: () => Promise<{ ok: boolean; error?: string }>, success: string) {
    setBusy(true);
    try {
      const result = await operation();
      setStatus(result.ok ? success : (result.error ?? "Extension operation failed."));
      if (result.ok) {
        const refreshed = await bridge?.extensionsList?.();
        if (refreshed?.ok) setRows(refreshed.extensions ?? []);
        return true;
      }
    } catch {
      setStatus("Extension operation failed.");
    } finally {
      setBusy(false);
    }
    return false;
  }
  return (
    <BrowserGroup title="Extensions">
      {login === false && (
        <div className="mb-4 flex flex-wrap items-center gap-3 rounded-lg border border-warning/30 bg-warning/10 p-3">
          <p className="flex-1 text-sm">
            Extensions run only when Keep in-app browser sign-ins is on.
          </p>
          <Button
            variant="outline"
            disabled={busy}
            onClick={() =>
              void run(async () => {
                const result = await bridge!.set(true);
                if (result.ok) setLogin(true);
                return result;
              }, "Browser sign-ins enabled.")
            }
          >
            Turn on
          </Button>
        </div>
      )}
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <SettingsNote />
        <Button
          variant="outline"
          disabled={!supported || busy}
          onClick={() => void run(() => bridge!.extensionsAdd!(), "Extension added.")}
        >
          Add extension…
        </Button>
      </div>
      {!supported && <UpdateReason verb="manage extensions" />}
      {rows.length > 0 && (
        <div className="space-y-2">
          <Input
            aria-label="Search extensions"
            placeholder="Search extensions"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
          <p className="text-xs text-muted-foreground">
            Showing {pageRows.length} of {rows.length}
          </p>
        </div>
      )}
      <div className={login === false ? "opacity-50" : undefined}>
        {pageRows.map((extension) => (
          <div key={extension.id} className="flex items-center gap-3 border-t py-3">
            <span className="rounded-lg bg-muted p-2">
              <PuzzleIcon className="size-5" />
            </span>
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm font-medium">
                {extension.name}{" "}
                <span className="rounded bg-muted px-1 text-xs font-normal text-muted-foreground">
                  {extension.version}
                </span>
              </p>
              <p className="text-xs text-muted-foreground">
                <TooltipProvider>
                  <StatusIcon
                    tone={extension.enabled ? "ok" : "neutral"}
                    label={extension.enabled ? "Enabled" : "Disabled"}
                    icon={extension.enabled ? <CheckIcon /> : <XIcon />}
                  />
                </TooltipProvider>{" "}
                ·{" "}
                <span title={extension.runtimeId ?? extension.id}>
                  ID …{(extension.runtimeId ?? extension.id).slice(-6)}
                </span>
              </p>
            </div>
            <Switch
              checked={extension.enabled}
              disabled={!supported || busy || login !== true}
              aria-label={`Enable ${extension.name}`}
              onCheckedChange={(enabled) =>
                void run(
                  () => bridge!.extensionsEnable!(extension.id, enabled),
                  enabled ? "Extension enabled." : "Extension disabled.",
                )
              }
            />
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  aria-label={`Actions for ${extension.name}`}
                  disabled={busy || !supported}
                >
                  <MoreHorizontalIcon className="size-4" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent>
                {bridge?.extensionsReview && /\bbitwarden\b/i.test(extension.name) && (
                  <DropdownMenuItem
                    onSelect={() =>
                      void run(
                        () => bridge.extensionsReview!(extension.id),
                        "Extension permissions reviewed.",
                      )
                    }
                  >
                    Review permissions
                  </DropdownMenuItem>
                )}
                <DropdownMenuItem
                  className="text-destructive"
                  onSelect={() => setRemoving(extension)}
                >
                  Remove…
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        ))}
      </div>
      <SettingsPager label="Extension pages" {...pager} />
      {!rows.length && (
        <p className="py-4 text-sm text-muted-foreground">
          No extensions installed. Add an extension from a publisher you trust.
        </p>
      )}
      <p role="status" className="mt-3 text-sm">
        {status}
      </p>
      <Confirm
        title={`Remove ${removing?.name ?? "extension"}?`}
        description="Its Browser data will be deleted, including Bitwarden’s local vault cache if present."
        action="Remove extension"
        open={!!removing}
        busy={busy}
        onClose={() => setRemoving(null)}
        onConfirm={() => {
          if (removing)
            void run(() => bridge!.extensionsRemove!(removing.id), "Extension removed.").then(
              (ok) => {
                if (ok) setRemoving(null);
              },
            );
        }}
      />
    </BrowserGroup>
  );
}
function SettingsNote() {
  return (
    <p className="max-w-sm text-sm text-muted-foreground">
      Extensions can read Browser pages, including pages an agent uses. Pages reload after changes.
    </p>
  );
}
