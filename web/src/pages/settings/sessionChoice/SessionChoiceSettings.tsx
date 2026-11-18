import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Link } from "@/lib/routing";
import { Button } from "@/components/ui/button";
import { Combobox, COMBOBOX_OPTION_THRESHOLD } from "@/components/ui/combobox";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { useHosts } from "@/hooks/useHosts";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import { usageBoardAvailable } from "@/lib/capabilities";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import {
  choiceRequest,
  EMPTY_CHOICE,
  useChoiceMeters,
  useChoiceLines,
  useSessionChoiceDocument,
  type ChoiceEnvelope,
  type ChoiceConversionPlan,
  type ChoiceLimit,
  type SessionChoiceDocument,
} from "./sessionChoiceData";
import { QuotaPreferencesSettings } from "./QuotaPreferencesSettings";
import { ChoicePolicySettings } from "./ChoicePolicySettings";
import { ChoiceLimitRules } from "./ChoiceLimitRules";
import { ChoiceConversion } from "./ChoiceConversion";
import { choiceDocumentForSave, limitRuleErrors } from "./choiceLimitEdits";
import { refreshChoiceConsumers, useChoicePreviewEpoch } from "./choiceInvalidation";
import { choiceDocumentLinks, referencedChoicePools } from "./sessionChoiceEdits";

function ReserveExemptWorkTypes({
  tileId,
  workTypes,
  selected,
  onToggle,
}: {
  tileId: string;
  workTypes: SessionChoiceDocument["workTypes"];
  selected: readonly string[];
  onToggle: (id: string, checked: boolean) => void;
}) {
  const [query, setQuery] = useState("");
  const visible = workTypes.filter((work) =>
    `${work.label} ${work.id}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()),
  );
  return (
    <div className="mt-1 space-y-1.5">
      {workTypes.length > COMBOBOX_OPTION_THRESHOLD && (
        <>
          <Input
            aria-label={`Search exempt work types for ${tileId}`}
            placeholder="Search work types"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
          <p className="text-xs text-muted-foreground">
            Showing {visible.length} of {workTypes.length}
          </p>
        </>
      )}
      <div className="flex max-h-48 flex-wrap gap-2 overflow-auto">
        {visible.map((work) => (
          <label key={work.id} className="flex items-center gap-1 text-xs">
            <input
              type="checkbox"
              checked={selected.includes(work.id)}
              onChange={(event) => onToggle(work.id, event.target.checked)}
            />
            {work.label}
          </label>
        ))}
      </div>
      {visible.length === 0 && <p className="text-xs text-muted-foreground">No matches.</p>}
    </div>
  );
}

export function SessionChoiceSettings() {
  const info = useServerInfo();
  const queryClient = useQueryClient();
  const dirty = useRef(false);
  const draftRevision = useRef("");
  const draftBaseline = useRef<SessionChoiceDocument | null>(null);
  const previewEpoch = useChoicePreviewEpoch();
  const document = useSessionChoiceDocument({ refreshRuntime: true });
  const hosts = useHosts();
  const [draft, setDraft] = useState<SessionChoiceDocument | null>(null);
  const unsupportedDocument = Boolean(
    document.data?.configurationError ||
    (draft?.schemaVersion !== undefined && ![1, 2].includes(draft.schemaVersion)),
  );
  const agents = useAvailableAgents({
    pinnedAgentIds: [
      ...new Set(
        choiceDocumentLinks(!unsupportedDocument && draft ? draft : EMPTY_CHOICE).map(
          (link) => link.agent,
        ),
      ),
    ],
  });
  const [host, setHost] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [conversion, setConversion] = useState<{
    plan: ChoiceConversionPlan;
    source: SessionChoiceDocument;
    pending: boolean;
  } | null>(null);
  const meters = useChoiceMeters(host);
  const lines = useChoiceLines();
  const savedSettings = document.data?.settings;
  const savedRevision = document.data?.revision;
  useEffect(() => {
    if (savedSettings !== undefined && savedRevision !== undefined && !dirty.current) {
      draftBaseline.current = savedSettings;
      setDraft(savedSettings ?? structuredClone(EMPTY_CHOICE));
      draftRevision.current = savedRevision;
    }
  }, [savedSettings, savedRevision]);
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
    if (!draft || !wireDraft || !document.data) return;
    if (conversion?.pending && JSON.stringify(draft) !== JSON.stringify(conversion.plan.settings)) {
      setError("Save the conversion before making other edits, or discard the conversion draft.");
      return;
    }
    const errors = limitRuleErrors(draft);
    if (errors.length) {
      setError(errors.join(" "));
      return;
    }
    if (draft.schemaVersion === 2 && !document.data.capabilities?.limitRules) {
      setError("This server cannot save scoped rules. Your draft is preserved.");
      return;
    }
    setSaving(true);
    setError(null);
    setSaved(null);
    try {
      const result = await choiceRequest<ChoiceEnvelope>("", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          settings: wireDraft,
          expected_revision: draftRevision.current,
          ...(conversion?.pending
            ? {
                conversion_proof: {
                  source_revision: conversion.plan.sourceRevision,
                  mapping_fingerprint: conversion.plan.mappingFingerprint,
                  replaced_paths: conversion.plan.replacedPaths,
                  new_rule_ids: conversion.plan.newRuleIds,
                },
              }
            : {}),
        }),
      });
      setDraft(result.settings ?? structuredClone(EMPTY_CHOICE));
      dirty.current = false;
      draftRevision.current = result.revision;
      draftBaseline.current = result.settings;
      if (conversion?.pending) setConversion({ ...conversion, pending: false });
      await document.refetch();
      await refreshChoiceConsumers(queryClient);
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
    dirty.current = true;
    setDraft((current) => (current ? { ...current, ...next } : current));
    setSaved(null);
  }
  function limit(id: string, update: Partial<ChoiceLimit>) {
    if (draft) patch({ limits: { ...draft.limits, [id]: { ...draft.limits[id], ...update } } });
  }
  const windowIds = [
    ...new Set([
      ...(meters.data?.meters.flatMap((tile) => tile.windows.map((window) => window.id)) ?? []),
      ...Object.values(draft?.limits ?? {}).flatMap((configured) =>
        configured.reserve?.window ? [configured.reserve.window] : [],
      ),
    ]),
  ].sort();
  async function floor(tile: string, mode: string) {
    setError(null);
    try {
      await choiceRequest("/floor", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tile, off: mode !== "kept", keep: mode === "manual" }),
      });
      await refreshChoiceConsumers(queryClient);
      setSaved("Saved quota release mode.");
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
  if (unsupportedDocument)
    return (
      <div role="alert" className="space-y-2 text-sm">
        <p>
          Session choice configuration cannot be edited by this client. The saved document is
          preserved.
        </p>
        <p className="break-words">
          {document.data?.configurationError?.message ??
            `Unsupported version: ${draft.schemaVersion}`}
        </p>
        <Button variant="outline" size="sm" onClick={() => void document.refetch()}>
          Reload
        </Button>
      </div>
    );
  const wireDraft = choiceDocumentForSave(draft, draftBaseline.current);
  const { pools: referencedPools, complete: poolReferencesComplete } = referencedChoicePools(
    draft,
    agents.isPlaceholderData || agents.isPending ? [] : (agents.data ?? []),
    meters.data?.products ?? {},
  );
  const availablePoolIds = [...referencedPools].filter((id) => !Object.hasOwn(draft.limits, id));
  return (
    <section className="space-y-4" aria-label="Session choice settings">
      <fieldset disabled={saving} className="min-w-0 space-y-4">
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
            {hostIds.length > COMBOBOX_OPTION_THRESHOLD ? (
              <Combobox
                mode="select"
                options={hostIds.map((id) => ({
                  key: id,
                  label: hosts.data?.find((item) => item.host_id === id)?.name ?? id,
                  description:
                    hosts.data?.find((item) => item.host_id === id)?.status === "offline"
                      ? "Offline"
                      : undefined,
                }))}
                value={host}
                onValueChange={setHost}
                placeholder="Choose a host"
                aria-label="Host"
                className="h-auto w-56 max-w-full rounded-none! bg-transparent p-0"
              />
            ) : (
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
            )}
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
        <ChoicePolicySettings
          key={host}
          document={draft}
          savedChoice={document.data}
          host={host}
          meters={meters.data?.meters ?? []}
          products={meters.data?.products ?? {}}
          onChange={(next) => {
            dirty.current = true;
            setDraft(
              draft.schemaVersion === 2 && next.schemaVersion !== 2
                ? {
                    ...draft,
                    ...next,
                    schemaVersion: 2,
                    limitRules: draft.limitRules,
                    limits: draft.limits,
                  }
                : next,
            );
            setSaved(null);
          }}
        />
        <ChoiceLimitRules
          document={draft}
          previewDocument={wireDraft ?? draft}
          previewEpoch={previewEpoch}
          host={host}
          revision={draftRevision.current}
          canEdit={Boolean(
            document.data?.capabilities?.limitRules &&
            document.data.capabilities.documentVersions.includes(2),
          )}
          onChange={(next) => {
            dirty.current = true;
            setDraft(next);
            setSaved(null);
          }}
        />
        {document.data?.capabilities?.limitRules && (
          <ChoiceConversion
            host={host}
            revision={draftRevision.current}
            dirty={dirty.current}
            onApply={(plan) => {
              if (!plan.settings || plan.sourceRevision !== draftRevision.current || dirty.current)
                return;
              setConversion({ plan, source: structuredClone(draft), pending: true });
              dirty.current = true;
              setDraft(plan.settings);
              setSaved("Conversion applied to draft. Save session choice to confirm.");
            }}
          />
        )}
        {conversion && (
          <Button
            size="sm"
            variant="outline"
            onClick={() => {
              if (conversion.pending) {
                setDraft(conversion.source);
                dirty.current = false;
                setConversion(null);
                setSaved("Conversion draft discarded.");
                return;
              }
              const changed = conversion.plan.newRules.some((rule) => {
                const current = draft.limitRules?.find((item) => item.id === rule.id);
                return current && JSON.stringify(current) !== JSON.stringify(rule);
              });
              if (
                changed ||
                conversion.plan.replacedPaths.some((path) => Object.hasOwn(draft.limits, path[1]))
              ) {
                setError(
                  "Converted constraints were edited. Resolve them before undoing conversion.",
                );
                return;
              }
              const limits = { ...draft.limits };
              for (const path of conversion.plan.replacedPaths)
                limits[path[1]] = conversion.source.limits[path[1]];
              setDraft({
                ...draft,
                schemaVersion: 2,
                limits,
                limitRules:
                  draft.limitRules?.filter(
                    (rule) => !conversion.plan.newRuleIds.includes(rule.id),
                  ) ?? [],
              });
              dirty.current = true;
              setConversion(null);
              setSaved("Undo applied to draft. Save session choice to restore legacy constraints.");
            }}
          >
            {conversion.pending ? "Discard conversion draft" : "Undo last conversion"}
          </Button>
        )}
        <details className="border-t border-border pt-3">
          <summary className="cursor-pointer text-sm font-medium">
            Legacy pool limits and current readings
          </summary>
          <label className="mt-3 flex flex-wrap items-center gap-2 text-sm">
            Add legacy pool limit
            {availablePoolIds.length > COMBOBOX_OPTION_THRESHOLD ? (
              <Combobox
                mode="select"
                options={availablePoolIds.map((id) => ({
                  key: id,
                  label: meters.data?.meters.find((tile) => tile.id === id)?.display_name ?? id,
                  description: id,
                }))}
                value=""
                onValueChange={(value) => {
                  if (value) limit(value, {});
                }}
                placeholder="Choose a quota counter"
                aria-label="Add legacy pool limit"
                className="h-auto w-64 max-w-full rounded-none! bg-transparent p-0"
              />
            ) : (
              <select
                aria-label="Add legacy pool limit"
                className="h-9 max-w-full rounded border border-input bg-background px-2"
                value=""
                onChange={(event) => {
                  if (event.target.value) limit(event.target.value, {});
                }}
              >
                <option value="">Choose a quota counter</option>
                {availablePoolIds.map((id) => (
                  <option key={id} value={id}>
                    {meters.data?.meters.find((tile) => tile.id === id)?.display_name ?? id}
                  </option>
                ))}
              </select>
            )}
          </label>
          {!Object.keys(draft.limits).length && (
            <p className="mt-2 text-sm text-muted-foreground">
              No legacy pool limits configured. Unlisted counters have no legacy policy restriction.
            </p>
          )}
          <div className="mt-3 overflow-x-auto">
            {meters.isError ? (
              <p role="alert" className="text-sm">
                Could not load meters.{" "}
                <Button variant="outline" size="sm" onClick={() => void meters.refetch()}>
                  Retry
                </Button>
              </p>
            ) : (
              <table className="w-full min-w-[1000px] table-fixed text-left text-sm">
                <colgroup>
                  <col style={{ width: 180 }} />
                  <col style={{ width: 300 }} />
                  <col style={{ width: 150 }} />
                  <col style={{ width: 140 }} />
                  <col style={{ width: 130 }} />
                  <col style={{ width: 100 }} />
                </colgroup>
                <thead>
                  <tr className="border-b border-border text-muted-foreground">
                    {["Tile", "Quota rule", "Current lines", "Floor", "Max running", "Running"].map(
                      (name) => (
                        <th key={name} className="pb-2 pr-3 font-medium">
                          {name}
                        </th>
                      ),
                    )}
                  </tr>
                </thead>
                <tbody>
                  {Object.keys(draft.limits)
                    .map(
                      (id) =>
                        meters.data?.meters.find((tile) => tile.id === id) ?? {
                          id,
                          display_name: id,
                          windows: [],
                          freshness: "missing",
                        },
                    )
                    .map((tile) => {
                      const limits = draft.limits[tile.id] ?? {};
                      const reserve = limits.reserve;
                      const window = tile.windows.find((item) => item.id === reserve?.window);
                      const line = lines.data?.tiles[tile.id];
                      const draftStop = reserve?.stopPct;
                      const now = Date.now() / 1000;
                      const projectedStop =
                        reserve &&
                        draftStop != null &&
                        window &&
                        !window.past_reset &&
                        window.remaining_pct != null &&
                        tile.freshness === "fresh" &&
                        (window.resets_at == null || window.resets_at > now) &&
                        (reserve.mode === "total" ||
                          ((window.window_minutes ?? 0) >= 1440 && window.resets_at != null))
                          ? draftStop *
                            (reserve.mode === "daily"
                              ? Math.ceil((window.resets_at! - now) / 86400)
                              : 1)
                          : null;
                      const floorMode = meters.data?.floors[tile.id];
                      const mode = floorMode?.active ? floorMode.until : "kept";
                      const updateReserve = (
                        next: Partial<NonNullable<ChoiceLimit["reserve"]>>,
                      ) => {
                        const updated = {
                          mode: reserve?.mode ?? "total",
                          window: reserve?.window ?? tile.windows[0]?.id ?? "",
                          pct: reserve?.pct ?? null,
                          stopPct: reserve?.stopPct ?? null,
                          ...next,
                        };
                        limit(tile.id, { reserve: updated });
                      };
                      return (
                        <tr key={tile.id} className="border-b border-border/60 align-top">
                          <td className="py-3 pr-3 font-medium">
                            <span className="block truncate" title={tile.display_name}>
                              {tile.display_name}
                            </span>
                            {poolReferencesComplete && !referencedPools.has(tile.id) && (
                              <p className="mt-1 text-xs font-normal text-muted-foreground">
                                Not referenced by current links. Saved limit retained.
                              </p>
                            )}
                            <button
                              type="button"
                              className="mt-1 block text-xs underline"
                              aria-label={`Remove ${tile.display_name} legacy limit`}
                              onClick={() =>
                                patch({
                                  limits: Object.fromEntries(
                                    Object.entries(draft.limits).filter(([id]) => id !== tile.id),
                                  ),
                                })
                              }
                            >
                              Remove limit
                            </button>
                          </td>
                          <td className="py-3 pr-3">
                            <div className="flex flex-wrap items-center gap-2">
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
                                <option value="none">Use up</option>
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
                                  {windowIds.length > COMBOBOX_OPTION_THRESHOLD ? (
                                    <Combobox
                                      mode="select"
                                      options={windowIds.map((id) => {
                                        const reported = tile.windows.find(
                                          (item) => item.id === id,
                                        );
                                        const reason = !reported
                                          ? "Not reported by this meter"
                                          : reserve.mode === "daily" &&
                                              (reported.window_minutes ?? 0) < 1440
                                            ? "Daily needs a window of at least 24 hours"
                                            : undefined;
                                        return {
                                          key: id,
                                          label: id,
                                          description: reason,
                                          disabled: Boolean(reason),
                                        };
                                      })}
                                      value={reserve.window}
                                      onValueChange={(windowId) =>
                                        updateReserve({ window: windowId })
                                      }
                                      placeholder="Choose a quota window"
                                      aria-label={`${tile.display_name} reserve window`}
                                      className="h-auto min-w-40 max-w-full rounded-none! bg-transparent p-0"
                                    />
                                  ) : (
                                    <select
                                      aria-label={`${tile.display_name} reserve window`}
                                      className="h-8 min-w-40 max-w-full rounded border border-input bg-background px-1"
                                      title={reserve.window || "Choose a reported quota window"}
                                      value={reserve.window}
                                      onChange={(event) =>
                                        updateReserve({ window: event.target.value })
                                      }
                                    >
                                      <option value="" disabled>
                                        Choose a quota window
                                      </option>
                                      {windowIds.map((id) => {
                                        const reported = tile.windows.find(
                                          (item) => item.id === id,
                                        );
                                        const reason = !reported
                                          ? "Not reported by this meter"
                                          : reserve.mode === "daily" &&
                                              (reported.window_minutes ?? 0) < 1440
                                            ? "Daily needs a window of at least 24 hours"
                                            : null;
                                        return (
                                          <option
                                            key={id}
                                            value={id}
                                            disabled={Boolean(reason)}
                                            title={reason ?? undefined}
                                          >
                                            {id}
                                          </option>
                                        );
                                      })}
                                    </select>
                                  )}
                                  <span className="basis-full break-all text-xs text-muted-foreground">
                                    Window: {reserve.window || "None selected"}. Unavailable options
                                    are disabled: this meter must report the window; Daily needs at
                                    least 24 hours.
                                  </span>
                                  {(
                                    [
                                      ["Warn", "pct"],
                                      ["Stop", "stopPct"],
                                    ] as const
                                  ).map(([label, key]) => (
                                    <label key={key} className="flex items-center gap-1 text-xs">
                                      {label}
                                      <Input
                                        aria-label={`${tile.display_name} ${label.toLowerCase()} percent`}
                                        className="h-8 w-16"
                                        type="number"
                                        min={0}
                                        max={100}
                                        step="any"
                                        placeholder="Unset"
                                        value={reserve[key] ?? ""}
                                        onChange={(event) =>
                                          updateReserve({
                                            [key]:
                                              event.target.value === ""
                                                ? null
                                                : Number(event.target.value),
                                          })
                                        }
                                      />
                                      %
                                    </label>
                                  ))}
                                </>
                              )}
                            </div>
                            {projectedStop !== null &&
                              window!.remaining_pct! <= projectedStop &&
                              !meters.data?.floors[tile.id]?.active &&
                              !lines.isError &&
                              line?.running &&
                              line.running.length > 0 && (
                                <p className="mt-1 text-xs text-destructive">
                                  Saving cuts {line.running.length} running sessions
                                </p>
                              )}
                            {reserve && !window && (
                              <p className="mt-1 text-xs text-muted-foreground">
                                This meter does not report the selected window. Choose an enabled
                                quota window above, or use Use up if no reported window is
                                available.
                              </p>
                            )}
                            {reserve && draft.workTypes.length > 0 && (
                              <details className="mt-1">
                                <summary className="cursor-pointer text-xs text-muted-foreground">
                                  Exempt work types
                                </summary>
                                <ReserveExemptWorkTypes
                                  tileId={tile.id}
                                  workTypes={draft.workTypes}
                                  selected={limits.reserveExempt ?? []}
                                  onToggle={(workId, checked) =>
                                    limit(tile.id, {
                                      reserveExempt: checked
                                        ? [...(limits.reserveExempt ?? []), workId]
                                        : limits.reserveExempt?.filter((id) => id !== workId),
                                    })
                                  }
                                />
                              </details>
                            )}
                          </td>
                          <td className="py-3 pr-3 tabular-nums">
                            {lines.isError ? (
                              <span role="alert">
                                Could not load quota lines.{" "}
                                <button
                                  type="button"
                                  className="underline"
                                  onClick={() => void lines.refetch()}
                                >
                                  Retry
                                </button>
                              </span>
                            ) : line ? (
                              <>
                                <div>
                                  Warn:{" "}
                                  {line.warn_line_pct == null ? "—" : `${line.warn_line_pct}%`}
                                </div>
                                <div>
                                  Stop:{" "}
                                  {line.stop_line_pct == null ? "—" : `${line.stop_line_pct}%`}
                                </div>
                                <div>
                                  {
                                    {
                                      ok: "Available",
                                      warn: "Warning",
                                      stop: "Stopped",
                                      exhausted: "Used up",
                                      unknown: "Unknown",
                                    }[line.state]
                                  }
                                  {line.released ? " · Released" : ""}
                                </div>
                              </>
                            ) : (
                              "—"
                            )}
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
                            {meters.data?.running[tile.id]?.length ?? 0}
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
          <Button
            variant="ghost"
            disabled={saving}
            onClick={() => {
              dirty.current = true;
              setConversion(null);
              setDraft(
                draft.schemaVersion === 2
                  ? { ...structuredClone(EMPTY_CHOICE), schemaVersion: 2, limitRules: [] }
                  : structuredClone(EMPTY_CHOICE),
              );
              setSaved(null);
            }}
          >
            Clear
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
            <button
              className="underline"
              type="button"
              onClick={async () => {
                const result = await document.refetch();
                if (result.data) {
                  dirty.current = false;
                  draftRevision.current = result.data.revision;
                  draftBaseline.current = result.data.settings;
                  setConversion(null);
                  setDraft(result.data.settings ?? structuredClone(EMPTY_CHOICE));
                }
              }}
            >
              Reload
            </button>
          </p>
        )}
      </fieldset>
    </section>
  );
}
