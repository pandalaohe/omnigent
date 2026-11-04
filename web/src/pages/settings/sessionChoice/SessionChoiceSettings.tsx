import { useEffect, useState } from "react";
import { Link } from "@/lib/routing";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { useHosts } from "@/hooks/useHosts";
import { usageBoardAvailable } from "@/lib/capabilities";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import {
  choiceRequest,
  EMPTY_CHOICE,
  useChoiceMeters,
  useSessionChoiceDocument,
  type ChoiceEnvelope,
  type ChoiceLimit,
  type SessionChoiceDocument,
} from "./sessionChoiceData";
import { QuotaPreferencesSettings } from "./QuotaPreferencesSettings";

export function SessionChoiceSettings() {
  const info = useServerInfo();
  const document = useSessionChoiceDocument();
  const hosts = useHosts();
  const [draft, setDraft] = useState<SessionChoiceDocument | null>(null);
  const [host, setHost] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const meters = useChoiceMeters(host);
  useEffect(() => {
    if (document.data) setDraft(document.data.settings ?? structuredClone(EMPTY_CHOICE));
  }, [document.data]);
  const selectedHost = hosts.data?.find((item) => item.host_id === host);
  const hostIds = [
    ...new Set([
      ...(hosts.data?.map((item) => item.host_id) ?? []),
      ...Object.keys(draft?.hosts ?? {}),
    ]),
  ];
  useEffect(() => {
    if (!host && hosts.data?.length) setHost(hosts.data[0].host_id);
  }, [host, hosts.data]);
  async function save() {
    if (!draft || !document.data) return;
    setSaving(true);
    setError(null);
    setSaved(null);
    try {
      const result = await choiceRequest<ChoiceEnvelope>("", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ settings: draft, expected_revision: document.data.revision }),
      });
      setDraft(result.settings ?? structuredClone(EMPTY_CHOICE));
      await document.refetch();
      setSaved(
        result.corrected?.length
          ? `Saved. ${result.corrected.length} edited rows marked Adjusted.`
          : "Saved session choice.",
      );
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Could not save session choice.");
    } finally {
      setSaving(false);
    }
  }
  function patch(next: Partial<SessionChoiceDocument>) {
    setDraft((current) => (current ? { ...current, ...next } : current));
    setSaved(null);
  }
  function limit(id: string, update: Partial<ChoiceLimit>) {
    if (draft) patch({ limits: { ...draft.limits, [id]: { ...draft.limits[id], ...update } } });
  }
  async function floor(tile: string, mode: string) {
    setError(null);
    try {
      await choiceRequest("/floor", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tile, off: mode !== "kept", keep: mode === "manual" }),
      });
      await meters.refetch();
      setSaved("Saved reserve mode.");
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Could not change reserve mode.");
    }
  }
  if (document.isPending)
    return <p className="py-3 text-sm text-muted-foreground">Loading session choice…</p>;
  if (!draft)
    return (
      <div role="alert" className="py-3 text-sm">
        Could not load session choice.{" "}
        <Button variant="outline" size="sm" onClick={() => void document.refetch()}>
          Retry
        </Button>
      </div>
    );
  return (
    <section className="space-y-4" aria-label="Session choice settings">
      <div className="flex flex-wrap items-center gap-4">
        <label className="flex items-center gap-2 text-sm font-medium">
          Session choice
          <Switch checked={draft.enabled} onCheckedChange={(enabled) => patch({ enabled })} />
        </label>
        <label className="flex items-center gap-2 text-sm">
          Spending
          <select
            className="h-9 rounded-md border border-input bg-background px-2 text-sm"
            value={draft.strategy}
            onChange={(event) =>
              patch({ strategy: event.target.value as SessionChoiceDocument["strategy"] })
            }
          >
            <option value="drain">Use up, tier by tier</option>
            <option value="balance">Balance by pace</option>
          </select>
        </label>
        <label className="flex items-center gap-2 text-sm">
          Host
          <select
            className="h-9 max-w-56 rounded-md border border-input bg-background px-2 text-sm"
            value={host}
            onChange={(event) => setHost(event.target.value)}
          >
            <option value="" disabled>
              Choose a host
            </option>
            {hostIds.map((id) => (
              <option key={id} value={id}>
                {hosts.data?.find((item) => item.host_id === id)?.name ?? id}
              </option>
            ))}
          </select>
        </label>
      </div>
      {!draft.workTypes.length && (
        <p className="text-sm text-muted-foreground">
          Add work types and presets to choose agents for new sessions.
        </p>
      )}
      {selectedHost && !draft.hosts[host] && (
        <p className="text-sm text-muted-foreground">
          No strategy for {selectedHost.name}. Creates use the current defaults.
        </p>
      )}
      <details className="border-t border-border pt-3">
        <summary className="cursor-pointer text-sm font-medium">Limits</summary>
        <div className="mt-3 overflow-x-auto">
          {meters.isError ? (
            <p role="alert" className="text-sm">
              Could not load meters.{" "}
              <Button variant="outline" size="sm" onClick={() => void meters.refetch()}>
                Retry
              </Button>
            </p>
          ) : (
            <table className="w-full min-w-[640px] text-left text-sm">
              <thead>
                <tr className="border-b border-border text-muted-foreground">
                  {["Tile", "Reserve", "Keeps now", "Floor", "Max running", "Running"].map(
                    (name) => (
                      <th key={name} className="pb-2 pr-3 font-medium">
                        {name}
                      </th>
                    ),
                  )}
                </tr>
              </thead>
              <tbody>
                {meters.data?.meters.map((tile) => {
                  const limits = draft.limits[tile.id] ?? {};
                  const reserve = limits.reserve;
                  const window = tile.windows.find((item) => item.id === reserve?.window);
                  const keeps =
                    reserve &&
                    window?.remaining_pct != null &&
                    window.resets_at &&
                    tile.freshness === "fresh"
                      ? reserve.mode === "total"
                        ? reserve.pct
                        : (reserve.pct * Math.max(0, window.resets_at - Date.now() / 1000)) / 86400
                      : null;
                  const floorMode = meters.data.floors[tile.id];
                  const mode = floorMode?.active ? floorMode.until : "kept";
                  const windows = tile.windows.filter(
                    (item) => reserve?.mode !== "daily" || (item.window_minutes ?? 0) >= 1440,
                  );
                  const updateReserve = (next: Partial<NonNullable<ChoiceLimit["reserve"]>>) =>
                    limit(tile.id, {
                      reserve: {
                        mode: reserve?.mode ?? "total",
                        window: reserve?.window ?? tile.windows[0]?.id ?? "",
                        pct: reserve?.pct ?? 0,
                        ...next,
                      },
                    });
                  return (
                    <tr key={tile.id} className="border-b border-border/60 align-top">
                      <td className="py-3 pr-3 font-medium">{tile.display_name}</td>
                      <td className="py-3 pr-3">
                        <div className="flex items-center gap-1">
                          <select
                            aria-label={`${tile.display_name} reserve mode`}
                            className="h-8 rounded border border-input bg-background px-1"
                            disabled={!tile.windows.length}
                            value={reserve?.mode ?? "none"}
                            onChange={(event) =>
                              event.target.value === "none"
                                ? limit(tile.id, { reserve: null })
                                : updateReserve({
                                    mode: event.target.value as "daily" | "total",
                                    window:
                                      event.target.value === "daily"
                                        ? (tile.windows.find(
                                            (item) => (item.window_minutes ?? 0) >= 1440,
                                          )?.id ?? "")
                                        : tile.windows[0].id,
                                  })
                            }
                          >
                            <option value="none">None</option>
                            <option value="total">Total</option>
                            <option
                              value="daily"
                              disabled={
                                !tile.windows.some((item) => (item.window_minutes ?? 0) >= 1440)
                              }
                            >
                              Daily
                            </option>
                          </select>
                          {reserve && (
                            <>
                              <select
                                aria-label={`${tile.display_name} reserve window`}
                                className="h-8 max-w-24 rounded border border-input bg-background px-1"
                                value={reserve.window}
                                onChange={(event) => updateReserve({ window: event.target.value })}
                              >
                                {windows.map((item) => (
                                  <option key={item.id} value={item.id}>
                                    {item.label ?? item.id}
                                  </option>
                                ))}
                              </select>
                              <Input
                                aria-label={`${tile.display_name} reserve percent`}
                                className="h-8 w-16"
                                type="number"
                                min={0}
                                max={100}
                                step={1}
                                value={reserve.pct}
                                onChange={(event) =>
                                  updateReserve({ pct: Number(event.target.value) })
                                }
                              />
                              <span>%</span>
                            </>
                          )}
                        </div>
                        {reserve && !window && (
                          <p className="mt-1 text-xs text-muted-foreground">
                            This reading has no selected window.
                          </p>
                        )}
                      </td>
                      <td className="py-3 pr-3 tabular-nums">
                        {keeps === null ? "—" : `${keeps.toFixed(1)}%`}
                      </td>
                      <td className="py-3 pr-3">
                        <select
                          aria-label={`${tile.display_name} floor`}
                          className="h-8 rounded border border-input bg-background px-1"
                          value={mode}
                          onChange={(event) => void floor(tile.id, event.target.value)}
                        >
                          <option value="kept">Kept</option>
                          <option value="reset">Off until reset</option>
                          <option value="manual">Use up</option>
                        </select>
                      </td>
                      <td className="py-3 pr-3">
                        <Input
                          aria-label={`${tile.display_name} max running`}
                          className="h-8 w-20"
                          type="number"
                          min={1}
                          step={1}
                          placeholder="No limit"
                          value={limits.maxRunning ?? ""}
                          onChange={(event) =>
                            limit(tile.id, {
                              maxRunning:
                                event.target.value === "" ? null : Number(event.target.value),
                            })
                          }
                        />
                      </td>
                      <td className="py-3 tabular-nums">
                        {meters.data.running[tile.id]?.length ?? 0}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
          {meters.data && !meters.data.meters.length && (
            <p className="mt-2 text-sm text-muted-foreground">No meters reported yet.</p>
          )}
        </div>
      </details>
      <details className="border-t border-border pt-3">
        <summary className="cursor-pointer text-sm font-medium">Quota display</summary>
        <div className="mt-3">
          <QuotaPreferencesSettings />
        </div>
      </details>
      <div className="flex flex-wrap items-center gap-3">
        <Button onClick={() => void save()} disabled={saving}>
          {saving ? "Saving…" : "Save session choice"}
        </Button>
        {saved && (
          <span role="status" className="text-sm text-muted-foreground">
            {saved}
          </span>
        )}
        {usageBoardAvailable(info) && (
          <Link to="/usage/board" className="text-sm text-primary underline underline-offset-2">
            Usage board
          </Link>
        )}
      </div>
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}{" "}
          <button className="underline" type="button" onClick={() => void document.refetch()}>
            Reload
          </button>
        </p>
      )}
    </section>
  );
}
