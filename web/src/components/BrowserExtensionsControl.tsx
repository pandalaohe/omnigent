import { useEffect, useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { browserLoginBridge } from "@/lib/nativeBridge";

export function BrowserExtensionsControl() {
  const bridge = useMemo(browserLoginBridge, []);
  const [extensions, setExtensions] = useState<
    { id: string; name: string; version: string; enabled: boolean; runtimeId?: string }[]
  >([]);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<Record<string, string>>({});
  useEffect(() => {
    let active = true;
    void bridge
      ?.extensionsList?.()
      .then((result) => {
        if (!active) return;
        if (result.ok) setExtensions(result.extensions ?? []);
        else setStatus({ add: result.error || "Could not list extensions." });
      })
      .catch(() => {
        if (active) setStatus({ add: "Could not list extensions." });
      });
    return () => {
      active = false;
    };
  }, [bridge]);
  if (
    !bridge?.extensionsList ||
    !bridge.extensionsAdd ||
    !bridge.extensionsEnable ||
    !bridge.extensionsRemove
  )
    return null;
  async function run(
    id: string,
    operation: () => Promise<{ ok: boolean; error?: string }>,
    success: string,
  ) {
    setBusy(true);
    setStatus((previous) => ({ ...previous, [id]: "" }));
    try {
      const result = await operation();
      setStatus((previous) => ({
        ...previous,
        [id]: result.ok ? success : result.error || "Extension operation failed.",
      }));
      const refreshed = await bridge!.extensionsList!();
      if (refreshed.ok) setExtensions(refreshed.extensions ?? []);
    } catch {
      setStatus((previous) => ({ ...previous, [id]: "Extension operation failed." }));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="space-y-3" aria-label="Browser extensions">
      <div className="flex items-center justify-between gap-3">
        <p className="text-sm font-medium">Browser extensions</p>
        <Button
          variant="outline"
          disabled={busy}
          onClick={() => void run("add", () => bridge.extensionsAdd!(), "Extension added.")}
        >
          Add extension
        </Button>
      </div>
      <p className="text-sm text-muted-foreground">
        Choose an unpacked Manifest V3 folder or .zip from a publisher you trust. Extensions can
        read pages in Browser, including pages an agent uses. Browser pages reload after changes.
      </p>
      {status.add && (
        <p role="status" className="text-sm">
          {status.add}
        </p>
      )}
      {extensions.length === 0 && (
        <p className="text-sm text-muted-foreground">No extensions installed.</p>
      )}
      {extensions.map((extension) => (
        <div key={extension.id} className="space-y-1 border-t pt-3">
          <div className="flex flex-wrap items-center gap-3">
            <div className="min-w-0 flex-1">
              <p className="text-sm font-medium">
                {extension.name} <span className="text-muted-foreground">{extension.version}</span>
              </p>
              <p className="break-all text-xs text-muted-foreground">
                {extension.runtimeId ?? extension.id}
              </p>
            </div>
            <Switch
              checked={extension.enabled}
              disabled={busy}
              aria-label={`Enable ${extension.name}`}
              onCheckedChange={(enabled) =>
                void run(
                  extension.id,
                  () => bridge.extensionsEnable!(extension.id, enabled),
                  enabled ? "Extension enabled." : "Extension disabled.",
                )
              }
            />
            {bridge.extensionsReview && /\bbitwarden\b/i.test(extension.name) && (
              <Button
                variant="outline"
                disabled={busy}
                onClick={() =>
                  void run(
                    extension.id,
                    () => bridge.extensionsReview!(extension.id),
                    "Extension permissions reviewed.",
                  )
                }
              >
                Review permissions
              </Button>
            )}
            <Button
              variant="outline"
              disabled={busy}
              onClick={() =>
                void run(
                  extension.id,
                  () => bridge.extensionsRemove!(extension.id),
                  "Extension removed.",
                )
              }
            >
              Remove
            </Button>
          </div>
          {status[extension.id] && (
            <p role="status" className="text-sm">
              {status[extension.id]}
            </p>
          )}
        </div>
      ))}
      {Object.entries(status)
        .filter(([id]) => id !== "add" && !extensions.some((extension) => extension.id === id))
        .map(
          ([id, message]) =>
            message && (
              <p key={id} role="status" className="text-sm">
                {message}
              </p>
            ),
        )}
    </section>
  );
}
