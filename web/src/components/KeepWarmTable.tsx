import {
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactElement,
} from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { AgentNameTag } from "@/components/AgentNameTag";
import { HelpTip } from "@/components/HelpTip";
import { LayeredEditor } from "@/components/LayeredEditor";
import { LayeredTable, type LayeredCell, type LayeredCellLevel } from "@/components/LayeredTable";
import { NumericField, SettingRow } from "@/components/SettingsFields";
import { Button } from "@/components/ui/button";
import { Segmented } from "@/components/ui/segmented";
import { Switch } from "@/components/ui/switch";
import {
  reportedOnAnyHost,
  rowsForAgentIds,
  useAgentRegistry,
  useAgentsWithSessions,
  harnessSection,
  type RegistryItem,
} from "@/hooks/useAgentRegistry";
import { useHosts } from "@/hooks/useHosts";
import { useKeepWarmPreferences } from "@/hooks/useKeepWarmPreferences";
import { useHarnessCatalog, type HarnessCapability } from "@/lib/agentLabels";
import { getOmnigentServerIdentity } from "@/lib/host";
import { authenticatedFetch, getCurrentUserId, subscribeIdentity } from "@/lib/identity";
import {
  KEEP_WARM_COLUMN_FIELDS,
  KEEP_WARM_DEFAULT_MAX_SECONDS,
  clearKeepWarmBelow,
  clearKeepWarmLevel,
  clampHostOfflineArchiveSeconds,
  defaultKeepWarmIntervalSeconds,
  dropKeepWarmCellEquals,
  keepWarmIntervalBoundsSeconds,
  keepWarmFamilyForHarness,
  keepWarmLayeredOptions,
  keepWarmLayeredStore,
  normalizeKeepWarmLayers,
  normalizeKeepWarmPreferences,
  readKeepWarmPreferences,
  resolveKeepWarmRow,
  saveKeepWarmPreferences,
  setKeepWarmHostWins,
  setKeepWarmLayer,
  KEEP_WARM_COLD_AFTER_BOUNDS_SECONDS,
  KEEP_WARM_HOST_OFFLINE_ARCHIVE_BOUNDS_SECONDS,
  KEEP_WARM_MAX_BOUNDS_SECONDS,
  type KeepWarmFamily,
  type KeepWarmLayerField,
  type KeepWarmLayerRecord,
  type KeepWarmLayers,
  type KeepWarmPreferences,
} from "@/lib/keepWarmPreferences";
import {
  crossingClashes,
  inheritedAt,
  levelsBelow,
  missingCellText,
  type LayeredLevel,
} from "@/lib/layeredValues";
import { ApiError, apiErrorFromResponse } from "@/lib/sessionsApi";

const EMPTY_CAPABILITIES: Record<string, HarnessCapability> = {};

interface Conversion {
  layers: KeepWarmLayers;
  already: boolean;
  migratedToLayersAt?: number;
}

async function convert(dryRun: boolean, signal: AbortSignal): Promise<Conversion> {
  const response = await authenticatedFetch("/v1/me/preferences/keep_warm/convert", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ dry_run: dryRun, target: "layers" }),
    signal,
  });
  if (!response.ok) throw await apiErrorFromResponse(response);
  const body = (await response.json()) as Record<string, unknown>;
  const layers = normalizeKeepWarmLayers(body.layers);
  if (!layers) throw new Error("The server returned invalid keep-warm settings.");
  return {
    layers,
    already: body.already === true,
    migratedToLayersAt:
      typeof body.migratedToLayersAt === "number" ? body.migratedToLayersAt : undefined,
  };
}

function scopeKey() {
  return JSON.stringify([getOmnigentServerIdentity(), getCurrentUserId()]);
}

function levelKey(at: LayeredLevel): string {
  if (at.level === "corner") return "corner";
  if (at.level === "row") return `row:${at.row}`;
  if (at.level === "col") return `col:${at.col}`;
  return `cell:${at.row}|${at.col}`;
}

function levelRecord(layers: KeepWarmLayers, at: LayeredLevel): KeepWarmLayerRecord {
  if (at.level === "corner") return layers.corner;
  if (at.level === "row") return layers.agents[at.row] ?? {};
  if (at.level === "col") return layers.hosts[at.col] ?? {};
  return layers.cells[at.col]?.[at.row] ?? {};
}

function levelRow(at: LayeredLevel): string {
  return at.level === "row" || at.level === "cell" ? at.row : "";
}

function recordHasValues(record: KeepWarmLayerRecord): boolean {
  return Object.keys(record).length > 0;
}

function hoursText(seconds: number): string {
  const hours = seconds / 3600;
  return Number.isInteger(hours) ? String(hours) : String(Math.round(hours * 10) / 10);
}

function minutesText(seconds: number): string {
  return String(Math.round(seconds / 60));
}

function fieldText(field: KeepWarmLayerField, value: unknown): string {
  if (field === "main" || field === "child") return value === true ? "On" : "Off";
  if (field === "intervalSeconds") {
    return typeof value === "number" ? `every ${minutesText(value)} min` : "family default";
  }
  if (field === "maxSeconds") {
    return typeof value === "number" ? `up to ${hoursText(value)} h` : "default";
  }
  if (value === 0) return "never cold";
  if (typeof value === "number") return `cold after ${minutesText(value)} min`;
  return "platform rule";
}

function stateWord(main: boolean, child: boolean): string {
  if (main && child) return "On";
  if (main) return "Main only";
  if (child) return "Children only";
  return "Off";
}

function fieldsShown(at: LayeredLevel, family: KeepWarmFamily | null): KeepWarmLayerField[] {
  if (at.level === "corner" || at.level === "col") {
    return ["main", "child", "maxSeconds", "coldAfterSeconds"];
  }
  const fields: KeepWarmLayerField[] = ["main", "child"];
  if (family && family !== "pi") fields.push("intervalSeconds");
  fields.push("maxSeconds");
  if (family && family !== "pi") fields.push("coldAfterSeconds");
  return fields;
}

function differsAt(
  layers: KeepWarmLayers,
  at: LayeredLevel,
  field: KeepWarmLayerField,
  family: KeepWarmFamily | null,
  crossAgent: boolean,
): boolean {
  const record = levelRecord(layers, at);
  if (record[field] === undefined) return false;
  const options = keepWarmLayeredOptions(family, { crossAgent });
  if (options.canHold && !options.canHold(levelRow(at), field, record[field])) return false;
  const store = keepWarmLayeredStore(layers);
  const own = options.normalize
    ? options.normalize(levelRow(at), field, record[field])
    : record[field];
  return !Object.is(own, inheritedAt(store, at, field, options).value);
}

function productIdFor(
  item: RegistryItem | undefined,
  registry: ReturnType<typeof useAgentRegistry>,
) {
  if (!item || !item.key.startsWith("harness:")) return item?.key ?? null;
  return (
    Object.values(registry.legacyProducts).find((product) => product.harness === item.harness)
      ?.key ?? null
  );
}

function rowRank(item: RegistryItem, capabilities: Record<string, HarnessCapability>): number {
  const harness = item.harness;
  if (harness === "pi" || harness === "pi-rpc") return 2;
  const mode = harness ? capabilities[harness]?.integration_mode : undefined;
  const section = mode ? harnessSection(mode, harness) : item.section;
  if (section === "native") return 0;
  if (section === "sdk") return 1;
  if (section === "acp") return 2;
  return 3;
}

function Mark() {
  return <span aria-hidden="true" className="size-1.5 shrink-0 rounded-full bg-pink-500" />;
}

function FieldNote({
  field,
  own,
  inheritedValue,
  inheritedSource,
  onUseInherited,
}: {
  field: KeepWarmLayerField;
  own: unknown;
  inheritedValue: unknown;
  inheritedSource: string | null;
  onUseInherited: () => void;
}) {
  if (own === undefined) {
    return (
      <p className="text-xs text-muted-foreground">
        {inheritedSource ? `From ${inheritedSource}` : "Default"}
      </p>
    );
  }
  if (own === null) {
    return (
      <p className="text-xs text-muted-foreground">
        Saved here · platform rule
        {inheritedSource && (
          <>
            {" · "}
            <button type="button" className="underline underline-offset-2" onClick={onUseInherited}>
              Use {fieldText(field, inheritedValue)} from {inheritedSource}
            </button>
          </>
        )}
      </p>
    );
  }
  return (
    <p className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
      <Mark />
      <span>Saved here</span>
      {inheritedSource && (
        <>
          <span aria-hidden="true">·</span>
          <button type="button" className="underline underline-offset-2" onClick={onUseInherited}>
            Use {fieldText(field, inheritedValue)} from {inheritedSource}
          </button>
        </>
      )}
    </p>
  );
}

function KeepWarmLevelEditor({
  at,
  trigger,
  open,
  onOpenChange,
  layers,
  family,
  rowLabel,
  colLabel,
  hostWins,
  busy,
  error,
  onSave,
  onClear,
  onClearBelow,
  onToggleHostWins,
}: {
  at: LayeredLevel;
  trigger: ReactElement;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  layers: KeepWarmLayers;
  family: KeepWarmFamily | null;
  rowLabel: string;
  colLabel: string;
  hostWins: boolean;
  busy: boolean;
  error?: string;
  onSave: (record: KeepWarmLayerRecord) => Promise<boolean>;
  onClear: () => void;
  onClearBelow: () => void;
  onToggleHostWins: (on: boolean) => void;
}) {
  const own = levelRecord(layers, at);
  const [draft, setDraft] = useState<KeepWarmLayerRecord>(() => ({ ...own }));
  const ownKey = JSON.stringify(own);
  useEffect(() => {
    if (!open) setDraft(JSON.parse(ownKey) as KeepWarmLayerRecord);
  }, [open, ownKey]);

  const store = keepWarmLayeredStore(layers);
  const crossAgent = at.level === "corner" || at.level === "col";
  const options = keepWarmLayeredOptions(family, { crossAgent });
  const fields = fieldsShown(at, family);
  const belowCount = levelsBelow(store, at).length;

  const levelName = (source: LayeredLevel | null): string | null => {
    if (!source) return null;
    if (source.level === "corner") return "All";
    if (source.level === "row") return rowLabel;
    if (source.level === "col") return colLabel;
    return `${rowLabel} on ${colLabel}`;
  };

  const title =
    at.level === "corner"
      ? "All agents, all hosts"
      : at.level === "col"
        ? `${colLabel} · all agents`
        : at.level === "row"
          ? `${rowLabel} · all hosts`
          : `${rowLabel} on ${colLabel}`;

  const order =
    at.level === "corner"
      ? "The top level. Everything below inherits it."
      : at.level === "cell"
        ? `This cell, then ${hostWins ? `${colLabel}, then ${rowLabel}` : `${rowLabel}, then ${colLabel}`}, then All.`
        : "Overrides All. Cells below inherit it.";

  const setField = (field: KeepWarmLayerField, value: unknown) => {
    const next = { ...draft };
    if (value === undefined) Reflect.deleteProperty(next, field);
    else (next as Record<string, unknown>)[field] = value;
    setDraft(next);
  };

  const shownOwn = (field: KeepWarmLayerField) => {
    if (draft[field] === undefined) return undefined;
    return options.normalize ? options.normalize(levelRow(at), field, draft[field]) : draft[field];
  };

  const commit = async () => {
    const record =
      at.level === "cell"
        ? dropKeepWarmCellEquals(draft, layers, at.row, at.col, family)
        : { ...draft };
    if (await onSave(record)) onOpenChange(false);
  };

  return (
    <LayeredEditor
      trigger={trigger}
      title={title}
      open={open}
      onOpenChange={onOpenChange}
      onClear={at.level !== "corner" && recordHasValues(own) ? onClear : undefined}
      clearBelowCount={belowCount}
      onClearBelow={onClearBelow}
    >
      <p className="text-xs text-muted-foreground">{order}</p>
      {at.level === "col" && (
        <label className="flex items-start justify-between gap-2 rounded-md border border-border px-2 py-1.5">
          <span className="min-w-0 text-sm">
            <span className="block font-medium">Host wins over agents</span>
            <span className="block text-xs text-muted-foreground">
              On: values set here beat agent rows. Cells still win.
            </span>
          </span>
          <Switch
            aria-label="Host wins over agents"
            checked={hostWins}
            disabled={busy}
            onCheckedChange={onToggleHostWins}
          />
        </label>
      )}
      {fields.includes("main") && (
        <div className="space-y-1">
          <SettingRow label="Main sessions" hint="Keep non-archived main sessions warm.">
            <Segmented
              label="Main sessions"
              value={draft.main === undefined ? "inherit" : draft.main ? "on" : "off"}
              options={[
                { value: "inherit", label: "Inherit" },
                { value: "on", label: "On" },
                { value: "off", label: "Off" },
              ]}
              onChange={(value) =>
                setField("main", value === "inherit" ? undefined : value === "on")
              }
            />
          </SettingRow>
          {at.level !== "corner" && (
            <FieldNote
              field="main"
              own={draft.main}
              inheritedValue={inheritedAt(store, at, "main", options).value}
              inheritedSource={levelName(inheritedAt(store, at, "main", options).source)}
              onUseInherited={() => setField("main", undefined)}
            />
          )}
        </div>
      )}
      {fields.includes("child") && (
        <div className="space-y-1">
          <SettingRow label="Children" hint="Keep active child sessions warm.">
            <Segmented
              label="Children"
              value={draft.child === undefined ? "inherit" : draft.child ? "on" : "off"}
              options={[
                { value: "inherit", label: "Inherit" },
                { value: "on", label: "On" },
                { value: "off", label: "Off" },
              ]}
              onChange={(value) =>
                setField("child", value === "inherit" ? undefined : value === "on")
              }
            />
          </SettingRow>
          {at.level !== "corner" && (
            <FieldNote
              field="child"
              own={draft.child}
              inheritedValue={inheritedAt(store, at, "child", options).value}
              inheritedSource={levelName(inheritedAt(store, at, "child", options).source)}
              onUseInherited={() => setField("child", undefined)}
            />
          )}
        </div>
      )}
      {crossAgent && (
        <p className="text-xs text-muted-foreground">
          Ping every is set per agent: its range depends on the agent.
        </p>
      )}
      {fields.includes("intervalSeconds") && family && family !== "pi" && (
        <div className="space-y-1">
          <SettingRow
            label="Ping every"
            hint="Time between cache pings. Each ping costs one cache read."
          >
            <NumericField
              ariaLabel="Ping every in minutes"
              value={
                draft.intervalSeconds === undefined
                  ? null
                  : (shownOwn("intervalSeconds") as number) / 60
              }
              min={keepWarmIntervalBoundsSeconds(family).min / 60}
              max={keepWarmIntervalBoundsSeconds(family).max / 60}
              disabled={busy}
              placeholder={minutesText(
                (inheritedAt(store, at, "intervalSeconds", options).value as number | undefined) ??
                  defaultKeepWarmIntervalSeconds(family),
              )}
              onCommit={(minutes) => setField("intervalSeconds", minutes * 60)}
              onClear={() => setField("intervalSeconds", undefined)}
            />
            <span className="text-sm text-muted-foreground">min</span>
          </SettingRow>
          <FieldNote
            field="intervalSeconds"
            own={shownOwn("intervalSeconds")}
            inheritedValue={inheritedAt(store, at, "intervalSeconds", options).value}
            inheritedSource={levelName(inheritedAt(store, at, "intervalSeconds", options).source)}
            onUseInherited={() => setField("intervalSeconds", undefined)}
          />
        </div>
      )}
      {fields.includes("maxSeconds") && (
        <div className="space-y-1">
          <SettingRow
            label="Up to"
            hint={
              family === "pi"
                ? "Pi's own warmer stops after 30 minutes."
                : "Stop warming this long after the last real turn."
            }
          >
            {family === "pi" ? (
              <span className="text-sm">30 min (Pi limit)</span>
            ) : (
              <>
                <NumericField
                  ariaLabel="Keep warm up to in hours"
                  value={
                    draft.maxSeconds === undefined
                      ? null
                      : (shownOwn("maxSeconds") as number) / 3600
                  }
                  min={KEEP_WARM_MAX_BOUNDS_SECONDS.min / 3600}
                  max={KEEP_WARM_MAX_BOUNDS_SECONDS.max / 3600}
                  disabled={busy}
                  placeholder={hoursText(
                    (inheritedAt(store, at, "maxSeconds", options).value as number | undefined) ??
                      KEEP_WARM_DEFAULT_MAX_SECONDS,
                  )}
                  onCommit={(hours) => setField("maxSeconds", hours * 3600)}
                  onClear={() => setField("maxSeconds", undefined)}
                />
                <span className="text-sm text-muted-foreground">hours</span>
              </>
            )}
          </SettingRow>
          {family !== "pi" && at.level !== "corner" && (
            <FieldNote
              field="maxSeconds"
              own={shownOwn("maxSeconds")}
              inheritedValue={inheritedAt(store, at, "maxSeconds", options).value}
              inheritedSource={levelName(inheritedAt(store, at, "maxSeconds", options).source)}
              onUseInherited={() => setField("maxSeconds", undefined)}
            />
          )}
        </div>
      )}
      {fields.includes("coldAfterSeconds") && (
        <div className="space-y-1">
          <SettingRow
            label="Cold after"
            hint="How long until a quiet session counts as cold. Blank follows the platform rule; 0 keeps it warm."
          >
            <NumericField
              ariaLabel="Cold after in minutes"
              value={
                draft.coldAfterSeconds === undefined || draft.coldAfterSeconds === null
                  ? null
                  : (shownOwn("coldAfterSeconds") as number) / 60
              }
              min={0}
              max={KEEP_WARM_COLD_AFTER_BOUNDS_SECONDS.max / 60}
              disabled={busy}
              placeholder={
                inheritedAt(store, at, "coldAfterSeconds", options).value === null
                  ? "auto"
                  : minutesText(inheritedAt(store, at, "coldAfterSeconds", options).value as number)
              }
              onCommit={(minutes) => setField("coldAfterSeconds", minutes * 60)}
              onClear={() => setField("coldAfterSeconds", undefined)}
            />
            <span className="text-sm text-muted-foreground">min</span>
          </SettingRow>
          {at.level !== "corner" && (
            <FieldNote
              field="coldAfterSeconds"
              own={shownOwn("coldAfterSeconds")}
              inheritedValue={inheritedAt(store, at, "coldAfterSeconds", options).value}
              inheritedSource={levelName(
                inheritedAt(store, at, "coldAfterSeconds", options).source,
              )}
              onUseInherited={() => setField("coldAfterSeconds", undefined)}
            />
          )}
        </div>
      )}
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
      <div className="flex justify-end gap-2 border-t border-border pt-2.5">
        <Button variant="outline" size="sm" disabled={busy} onClick={() => onOpenChange(false)}>
          Cancel
        </Button>
        <Button size="sm" disabled={busy} onClick={() => void commit()}>
          {busy ? "Saving…" : "Save"}
        </Button>
      </div>
    </LayeredEditor>
  );
}

function BulkEditor({
  selected,
  familyForRow,
  busy,
  onApply,
}: {
  selected: readonly LayeredCellLevel[];
  familyForRow: (row: string) => KeepWarmFamily | null;
  busy: boolean;
  onApply: (selected: readonly LayeredCellLevel[], record: KeepWarmLayerRecord) => void;
}) {
  const families = new Set(selected.map(({ row }) => familyForRow(row)));
  const shared = families.size === 1 ? [...families][0] : null;
  const fields: KeepWarmLayerField[] = ["main", "child"];
  if (shared && shared !== "pi") fields.push("intervalSeconds");
  if (shared !== "pi") fields.push("maxSeconds", "coldAfterSeconds");
  const [draft, setDraft] = useState<KeepWarmLayerRecord>({});
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (!open) setDraft({});
  }, [open]);
  const setField = (field: KeepWarmLayerField, value: unknown) => {
    const next = { ...draft };
    if (value === undefined) Reflect.deleteProperty(next, field);
    else (next as Record<string, unknown>)[field] = value;
    setDraft(next);
  };
  return (
    <LayeredEditor
      trigger={
        <Button variant="outline" size="sm" disabled={busy || selected.length === 0}>
          Set values
        </Button>
      }
      title={`${selected.length} selected ${selected.length === 1 ? "cell" : "cells"}`}
      open={open}
      onOpenChange={setOpen}
    >
      <p className="text-xs text-muted-foreground">A blank field stays as each cell has it.</p>
      {fields.includes("main") && (
        <SettingRow label="Main sessions">
          <Segmented
            label="Main sessions on selected cells"
            value={draft.main === undefined ? "keep" : draft.main ? "on" : "off"}
            options={[
              { value: "keep", label: "No change" },
              { value: "on", label: "On" },
              { value: "off", label: "Off" },
            ]}
            onChange={(value) => setField("main", value === "keep" ? undefined : value === "on")}
          />
        </SettingRow>
      )}
      {fields.includes("child") && (
        <SettingRow label="Children">
          <Segmented
            label="Children on selected cells"
            value={draft.child === undefined ? "keep" : draft.child ? "on" : "off"}
            options={[
              { value: "keep", label: "No change" },
              { value: "on", label: "On" },
              { value: "off", label: "Off" },
            ]}
            onChange={(value) => setField("child", value === "keep" ? undefined : value === "on")}
          />
        </SettingRow>
      )}
      {fields.includes("intervalSeconds") && shared && shared !== "pi" && (
        <SettingRow label="Ping every">
          <NumericField
            ariaLabel="Ping every in minutes for selected cells"
            value={draft.intervalSeconds === undefined ? null : draft.intervalSeconds / 60}
            min={keepWarmIntervalBoundsSeconds(shared).min / 60}
            max={keepWarmIntervalBoundsSeconds(shared).max / 60}
            disabled={busy}
            placeholder="No change"
            onCommit={(minutes) => setField("intervalSeconds", minutes * 60)}
            onClear={() => setField("intervalSeconds", undefined)}
          />
          <span className="text-sm text-muted-foreground">min</span>
        </SettingRow>
      )}
      {fields.includes("maxSeconds") && (
        <SettingRow label="Up to">
          <NumericField
            ariaLabel="Keep warm up to in hours for selected cells"
            value={draft.maxSeconds === undefined ? null : draft.maxSeconds / 3600}
            min={KEEP_WARM_MAX_BOUNDS_SECONDS.min / 3600}
            max={KEEP_WARM_MAX_BOUNDS_SECONDS.max / 3600}
            disabled={busy}
            placeholder="No change"
            onCommit={(hours) => setField("maxSeconds", hours * 3600)}
            onClear={() => setField("maxSeconds", undefined)}
          />
          <span className="text-sm text-muted-foreground">hours</span>
        </SettingRow>
      )}
      {fields.includes("coldAfterSeconds") && (
        <SettingRow label="Cold after">
          <NumericField
            ariaLabel="Cold after in minutes for selected cells"
            value={
              draft.coldAfterSeconds === undefined || draft.coldAfterSeconds === null
                ? null
                : draft.coldAfterSeconds / 60
            }
            min={0}
            max={KEEP_WARM_COLD_AFTER_BOUNDS_SECONDS.max / 60}
            disabled={busy}
            placeholder="No change"
            onCommit={(minutes) => setField("coldAfterSeconds", minutes * 60)}
            onClear={() => setField("coldAfterSeconds", undefined)}
          />
          <span className="text-sm text-muted-foreground">min</span>
        </SettingRow>
      )}
      <div className="flex justify-end gap-2 border-t border-border pt-2.5">
        <Button variant="outline" size="sm" disabled={busy} onClick={() => setOpen(false)}>
          Cancel
        </Button>
        <Button
          size="sm"
          disabled={busy}
          onClick={() => {
            onApply(selected, draft);
            setOpen(false);
          }}
        >
          Save to {selected.length} {selected.length === 1 ? "cell" : "cells"}
        </Button>
      </div>
    </LayeredEditor>
  );
}

export function KeepWarmTable() {
  const scope = useSyncExternalStore(subscribeIdentity, scopeKey, () => "");
  return <ScopedKeepWarmTable key={scope} scope={scope} />;
}

function ScopedKeepWarmTable({ scope }: { scope: string }) {
  const preferences = useKeepWarmPreferences();
  const registry = useAgentRegistry();
  const sessions = useAgentsWithSessions();
  const hostsQuery = useHosts({ includeSandbox: true });
  const capabilities = useHarnessCatalog(
    (catalog) => catalog.capabilitiesByHarness,
    EMPTY_CAPABILITIES,
  );
  const client = useQueryClient();
  const queryKey = ["keep-warm-layers", scope];
  const conversion = useQuery({
    queryKey,
    queryFn: ({ signal }) => convert(true, signal),
    retry: false,
    refetchOnWindowFocus: false,
    staleTime: Infinity,
  });
  const [pending, setPending] = useState<KeepWarmPreferences | null>(null);
  const [saveError, setSaveError] = useState<Error | null>(null);
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const [showAll, setShowAll] = useState(false);
  const [undo, setUndo] = useState<{ layers: KeepWarmLayers; label: string } | null>(null);
  const saving = useRef(false);
  const lifetime = useRef<AbortController | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    lifetime.current = controller;
    return () => controller.abort();
  }, []);

  const preferencesWithLayers = pending ?? {
    ...preferences,
    ...(conversion.data
      ? {
          layers: preferences.layers ?? conversion.data.layers,
          migratedToLayersAt: preferences.migratedToLayersAt ?? conversion.data.migratedToLayersAt,
        }
      : {}),
  };
  const layers = preferencesWithLayers.layers ?? null;
  const error = saveError ?? conversion.error;
  const unavailable = error instanceof ApiError && error.status === 404;
  const editable = !!conversion.data && !unavailable;
  const rejectedSave = saveError && "status" in saveError && saveError.status === 422;

  const byKey = useMemo(
    () =>
      new Map(
        [...registry.items, ...Object.values(registry.legacyProducts)].map((item) => [
          item.key,
          item,
        ]),
      ),
    [registry.items, registry.legacyProducts],
  );
  const familyForRow = (rowKey: string): KeepWarmFamily | null => {
    const item = byKey.get(rowKey);
    const harness = item?.harness ?? (rowKey.startsWith("harness:") ? rowKey.slice(8) : null);
    return keepWarmFamilyForHarness(harness, capabilities);
  };
  const familyForItem = (item: RegistryItem) =>
    keepWarmFamilyForHarness(item.harness, capabilities);

  const sessionRows = sessions.data
    ? rowsForAgentIds(registry.items, registry.legacyProducts, sessions.data)
    : null;
  const unknown =
    registry.isLoading ||
    registry.isError ||
    hostsQuery.isError ||
    sessions.isPending ||
    sessions.isError ||
    sessionRows === null;
  const isShown = (item: RegistryItem) => {
    if (unknown) return true;
    if (!item.deployed) return false;
    return showAll || !!sessionRows?.has(item.key);
  };
  const channelItems = registry.items.filter((item) => familyForItem(item) !== null);
  const visibleRows = channelItems.filter(isShown);
  const hiddenCount = sessionRows
    ? channelItems.filter(
        (item) => item.deployed && familyForItem(item) !== null && !sessionRows.has(item.key),
      ).length
    : 0;
  const noChannel = registry.items.filter(
    (item) => familyForItem(item) === null && isShown(item) && item.deployed,
  );
  visibleRows.sort(
    (one, two) =>
      rowRank(one, capabilities) - rowRank(two, capabilities) || one.label.localeCompare(two.label),
  );
  const nameCounts = new Map<string, number>();
  for (const item of [...visibleRows, ...noChannel]) {
    nameCounts.set(item.label, (nameCounts.get(item.label) ?? 0) + 1);
  }
  const fullIdFor = (item: RegistryItem) => productIdFor(item, registry) ?? item.key;
  const shortIdFor = (item: RegistryItem) => {
    const id = fullIdFor(item);
    return id.startsWith("harness:") ? id.slice(8) : id.slice(-6);
  };
  const rowNameFor = (item: RegistryItem) =>
    (nameCounts.get(item.label) ?? 0) > 1 ? `${item.label} · …${shortIdFor(item)}` : item.label;
  const badgeIdFor = (item: RegistryItem) => productIdFor(item, registry) ?? null;
  const rowItemFor = (rowKey: string) => byKey.get(rowKey);

  const knownHosts = new Map((hostsQuery.data ?? []).map((host) => [host.host_id, host]));
  const hosts = [
    ...new Set([
      "",
      ...(hostsQuery.data ?? [])
        .filter((host) => !host.sandbox_provider)
        .map((host) => host.host_id),
      ...Object.keys(layers?.hosts ?? {}),
      ...Object.keys(layers?.cells ?? {}),
      ...Object.keys(layers?.hostWins ?? {}),
    ]),
  ];
  const hostName = (id: string) => (id === "" ? "No host" : (knownHosts.get(id)?.name ?? id));
  const hostDetail = (id: string) => {
    if (id === "") return undefined;
    const host = knownHosts.get(id);
    if (!host) return "Unavailable host";
    return host.status === "online" ? "Online" : "Offline";
  };
  const rowLabel = (rowKey: string) => {
    const item = rowItemFor(rowKey);
    return item ? rowNameFor(item) : rowKey;
  };

  function discardPending() {
    setPending(null);
    setSaveError(null);
    setEditing(null);
  }

  async function save(
    change: (current: KeepWarmPreferences) => KeepWarmPreferences,
  ): Promise<boolean> {
    if (saving.current || !conversion.data || unavailable || scope !== scopeKey()) return false;
    const signal = lifetime.current?.signal;
    if (!signal || signal.aborted) return false;
    saving.current = true;
    setBusy(true);
    setSaveError(null);
    setPending(change(preferencesWithLayers));
    try {
      let authoritative = conversion.data;
      if (!authoritative.already) {
        authoritative = await convert(false, signal);
        if (signal.aborted || scope !== scopeKey()) return false;
        client.setQueryData(queryKey, { ...authoritative, already: true });
      }
      const current = readKeepWarmPreferences();
      const base = normalizeKeepWarmPreferences({
        ...current,
        layers: authoritative.layers,
        migratedToLayersAt: authoritative.migratedToLayersAt ?? current.migratedToLayersAt,
      });
      const next = change(base);
      setPending(next);
      await saveKeepWarmPreferences(
        next,
        [...byKey.values()].map((item) => ({ id: item.key, harness: item.harness })),
        signal,
      );
      if (signal.aborted || scope !== scopeKey()) return false;
      client.setQueryData(queryKey, {
        layers: next.layers,
        already: true,
        migratedToLayersAt: next.migratedToLayersAt,
      });
      setPending(null);
      return true;
    } catch (caught) {
      if (!signal.aborted && scope === scopeKey()) {
        setSaveError(
          caught instanceof Error ? caught : new Error("Couldn't save keep-warm settings."),
        );
      }
      return false;
    } finally {
      saving.current = false;
      if (!signal.aborted) setBusy(false);
    }
  }

  async function saveAtLevel(at: LayeredLevel, record: KeepWarmLayerRecord) {
    const ok = await save((base) => ({
      ...base,
      layers: setKeepWarmLayer(base.layers!, at, record),
    }));
    return ok;
  }

  function clearAtLevel(at: LayeredLevel) {
    const before = layers;
    void (async () => {
      const ok = await save((base) => ({
        ...base,
        layers: clearKeepWarmLevel(base.layers!, at),
      }));
      if (ok && before) setUndo({ layers: before, label: "Cleared this level." });
    })();
  }

  function clearBelow(at: LayeredLevel) {
    const before = layers;
    const count = layers ? levelsBelow(keepWarmLayeredStore(layers), at).length : 0;
    void (async () => {
      const ok = await save((base) => ({
        ...base,
        layers: clearKeepWarmBelow(base.layers!, at),
      }));
      if (ok && before) {
        setUndo({
          layers: before,
          label: `Cleared ${count} ${count === 1 ? "value" : "values"} below.`,
        });
      }
    })();
  }

  function setHostWins(host: string, on: boolean) {
    void save((base) => ({
      ...base,
      layers: setKeepWarmHostWins(base.layers!, host, on),
    }));
  }

  function clearSelectedCells(selected: readonly LayeredCellLevel[]) {
    const before = layers;
    void (async () => {
      const ok = await save((base) => {
        let next = base.layers!;
        for (const { row, col } of selected) {
          next = setKeepWarmLayer(next, { level: "cell", row, col }, {});
        }
        return { ...base, layers: next };
      });
      if (ok && before) {
        setUndo({
          layers: before,
          label: `Cleared ${selected.length} ${selected.length === 1 ? "cell" : "cells"}.`,
        });
      }
    })();
  }

  function applyToSelection(selected: readonly LayeredCellLevel[], record: KeepWarmLayerRecord) {
    const before = layers;
    void (async () => {
      const ok = await save((base) => {
        let next = base.layers!;
        for (const { row, col } of selected) {
          const existing = next.cells[col]?.[row] ?? {};
          const family = familyForRow(row);
          const merged = { ...existing, ...record };
          next = setKeepWarmLayer(
            next,
            { level: "cell", row, col },
            dropKeepWarmCellEquals(merged, next, row, col, family),
          );
        }
        return { ...base, layers: next };
      });
      if (ok && before) {
        setUndo({
          layers: before,
          label: `Set values on ${selected.length} ${selected.length === 1 ? "cell" : "cells"}.`,
        });
      }
    })();
  }

  function undoLast() {
    if (!undo) return;
    const target = undo.layers;
    void (async () => {
      const ok = await save((base) => ({ ...base, layers: target }));
      if (ok) setUndo(null);
    })();
  }

  function renderCell(rowKey: string, host: string): LayeredCell {
    if (!layers) return { content: null };
    const item = rowItemFor(rowKey);
    const harness = item?.harness ?? null;
    const machine = knownHosts.get(host);
    const installed =
      host === "" ||
      (machine ? reportedOnAnyHost(harness, capabilities[harness ?? ""], [machine]) : false);
    if (!installed) {
      return { content: null, unavailable: missingCellText(harness, machine?.platform) };
    }
    const family = familyForRow(rowKey);
    const resolved = resolveKeepWarmRow(layers, rowKey, host, family);
    const at: LayeredCellLevel = { level: "cell", row: rowKey, col: host };
    const options = keepWarmLayeredOptions(family);
    const store = keepWarmLayeredStore(layers);
    const fields = fieldsShown(at, family);
    const differing = fields.filter((field) => differsAt(layers, at, field, family, false));
    const ownRecord = levelRecord(layers, at);
    const parts = differing
      .filter((field) => field !== "main" && field !== "child")
      .map((field) => {
        const own = ownRecord[field];
        const shown = options.normalize ? options.normalize(rowKey, field, own) : own;
        return fieldText(field, shown);
      });
    const clashes = crossingClashes(store, rowKey, host, fields, options);
    const clash = clashes
      .map(
        ({ field, winner }) =>
          `${rowLabel(rowKey)} and ${hostName(host)} both set ${field}; ${
            winner === "row" ? rowLabel(rowKey) : hostName(host)
          } wins. Click this cell to set it here.`,
      )
      .join(" ");
    return {
      content: (
        <span className="flex flex-col gap-0.5">
          <span>{stateWord(resolved.main, resolved.child)}</span>
          {parts.map((part) => (
            <span key={part} className="text-xs text-muted-foreground">
              {part}
            </span>
          ))}
        </span>
      ),
      mark: differing.length > 0,
      clash: clash || undefined,
    };
  }

  function renderEditor(at: LayeredLevel, trigger: ReactElement) {
    if (!layers) return trigger;
    const family = at.level === "row" || at.level === "cell" ? familyForRow(at.row) : null;
    const row = at.level === "cell" || at.level === "row" ? rowLabel(at.row) : "";
    const col = at.level === "cell" ? hostName(at.col) : at.level === "col" ? hostName(at.col) : "";
    return (
      <KeepWarmLevelEditor
        key={levelKey(at)}
        at={at}
        trigger={trigger}
        open={editing === levelKey(at)}
        onOpenChange={(open) => setEditing(open ? levelKey(at) : null)}
        layers={layers}
        family={family}
        rowLabel={row}
        colLabel={col}
        hostWins={at.level === "col" || at.level === "cell" ? !!layers.hostWins[at.col] : false}
        busy={busy}
        error={editing === levelKey(at) ? saveError?.message : undefined}
        onSave={(record) => saveAtLevel(at, record)}
        onClear={() => clearAtLevel(at)}
        onClearBelow={() => clearBelow(at)}
        onToggleHostWins={(on) => {
          if (at.level === "col") setHostWins(at.col, on);
        }}
      />
    );
  }

  return (
    <div className="space-y-4" data-testid="keep-warm-table">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1">
          <p className="flex items-center gap-1.5 text-sm font-medium">
            Keep sessions warm
            <HelpTip label="Keep sessions warm">
              A cell beats its row and column; those beat the corner. An unset level inherits the
              one above. Every ping costs one cache read.
            </HelpTip>
          </p>
          <p className="text-xs text-muted-foreground">
            Click the corner, a header or a cell to change it.
          </p>
        </div>
        {!unknown && hiddenCount > 0 && (
          <Button variant="outline" size="sm" onClick={() => setShowAll((current) => !current)}>
            {showAll
              ? "Hide agents with no sessions"
              : `Show agents with no sessions (${hiddenCount})`}
          </Button>
        )}
      </div>
      {conversion.isPending && (
        <p role="status" className="text-sm text-muted-foreground">
          Loading keep-warm settings…
        </p>
      )}
      {conversion.data && !conversion.data.already && (
        <p className="rounded-md border bg-muted/40 px-3 py-2 text-sm">
          Converted from your previous settings · saved when you first edit
        </p>
      )}
      {error && (
        <div
          role="alert"
          className="space-y-2 rounded-md border border-destructive/40 px-3 py-2 text-sm"
        >
          <p>
            {unavailable
              ? "Server update needed. Keep-warm settings are read-only until the server is updated."
              : `Save wasn't confirmed: ${error.message}`}
          </p>
          {!unavailable && !conversion.data && (
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                void conversion.refetch();
              }}
            >
              Retry
            </Button>
          )}
          {rejectedSave && !busy && (
            <Button variant="outline" size="sm" onClick={discardPending}>
              Discard unsaved changes
            </Button>
          )}
        </div>
      )}
      {layers && conversion.data ? (
        <>
          <LayeredTable
            label="Keep-warm by agent and host"
            cornerLabel="All agents, all hosts"
            cornerMark={false}
            rows={visibleRows.map((item) => ({
              id: item.key,
              label: rowNameFor(item),
              content: (
                <span title={fullIdFor(item)}>
                  <AgentNameTag
                    agentId={badgeIdFor(item)}
                    name={rowNameFor(item)}
                    harness={item.harness}
                  />
                </span>
              ),
              mark: fieldsShown({ level: "row", row: item.key }, familyForItem(item)).some(
                (field) =>
                  differsAt(
                    layers,
                    { level: "row", row: item.key },
                    field,
                    familyForItem(item),
                    false,
                  ),
              ),
            }))}
            columns={hosts.map((host) => ({
              id: host,
              label: hostName(host),
              content: hostName(host),
              detail: hostDetail(host),
              hostWins: !!layers.hostWins[host],
              mark:
                !!layers.hostWins[host] ||
                KEEP_WARM_COLUMN_FIELDS.some((field) =>
                  differsAt(layers, { level: "col", col: host }, field, null, true),
                ),
            }))}
            renderCell={renderCell}
            renderEditor={renderEditor}
            // oxlint-disable-next-line react/no-unstable-nested-components -- render prop, not a component
            selectionActions={(selected, clear) => (
              <>
                <BulkEditor
                  selected={selected}
                  familyForRow={familyForRow}
                  busy={busy || !editable}
                  onApply={applyToSelection}
                />
                <Button
                  variant="outline"
                  size="sm"
                  disabled={busy || !editable || selected.length === 0}
                  onClick={() => {
                    clearSelectedCells(selected);
                    clear();
                  }}
                >
                  Use inherited
                </Button>
              </>
            )}
          />
          {undo && (
            <div
              role="status"
              className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-muted/40 px-3 py-2 text-sm"
            >
              <span>{undo.label}</span>
              <Button variant="outline" size="sm" disabled={busy} onClick={() => undoLast()}>
                Undo
              </Button>
            </div>
          )}
          {noChannel.length > 0 && (
            <p className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
              <span>Keep-warm is not available for:</span>
              {noChannel.map((item) => (
                <span key={item.key} title={fullIdFor(item)}>
                  <AgentNameTag
                    agentId={badgeIdFor(item)}
                    name={rowNameFor(item)}
                    harness={item.harness}
                  />
                </span>
              ))}
              <HelpTip label="About agents with no keep-warm channel">
                These agents have no cache channel to ping, and they show no warm or cold state.
              </HelpTip>
            </p>
          )}
        </>
      ) : (
        !conversion.isPending &&
        !error && <p className="text-sm text-muted-foreground">Loading keep-warm settings…</p>
      )}
      <div className="border-t pt-4">
        <SettingRow
          label="Archive children of an offline host after"
          hint="Children whose host stays offline this long are archived automatically. 0 turns auto-archive off."
        >
          <NumericField
            ariaLabel="Archive children of an offline host after in hours"
            value={(preferencesWithLayers.hostOfflineArchiveSeconds ?? 0) / 3600}
            min={0}
            max={KEEP_WARM_HOST_OFFLINE_ARCHIVE_BOUNDS_SECONDS.max / 3600}
            disabled={!editable || busy}
            onCommit={(hours) => {
              void save((base) => ({
                ...base,
                hostOfflineArchiveSeconds: clampHostOfflineArchiveSeconds(hours * 3600),
              }));
            }}
          />
          <span className="text-sm text-muted-foreground">hours (0 = off)</span>
        </SettingRow>
      </div>
    </div>
  );
}
