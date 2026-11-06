import { useState, type ReactElement, type ReactNode } from "react";

import { HelpTip } from "@/components/HelpTip";
import { AgentNameTag } from "@/components/AgentNameTag";
import { ClaudeFastModeHelp } from "@/components/SessionDefaultModeSelect";
import { ArrowRightIcon } from "lucide-react";
import { Link } from "@/lib/routing";
import { LayeredEditor, LayeredEditorAction } from "@/components/LayeredEditor";
import { LayeredOwn, LayeredTable } from "@/components/LayeredTable";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Segmented } from "@/components/ui/segmented";
import { Switch } from "@/components/ui/switch";
import type { Host } from "@/hooks/useHosts";
import {
  COLUMN_CONCEPTS,
  callingDefaultHarnesses,
  callingDefaultHasModels,
  callingDefaultsLayers,
  callingHarnessLabel,
  callingLayerOptions,
  callingLevelKeys,
  callingPermissionOptions,
  resolveAgentMasterField,
  sameCallingHost,
  setCallingLayer,
  promoteCallingValueOnAllHosts,
  type CallingDefaultsTable,
  type PermissionOption,
} from "@/lib/callingDefaults";
import type { CallingDefaultsCatalogRow } from "@/lib/callingDefaultsApi";
import { nativeModelLabel, normalizeEffortLabel } from "@/lib/composerModelLabel";
import {
  agentPresentOnHost,
  agentProductHarness,
  agentSettingsPath,
  groupAgentHarnesses,
} from "@/lib/agentSettings";
import {
  crossingClashes,
  inheritedAt,
  levelsBelow,
  missingCellText,
  type LayeredLevel,
} from "@/lib/layeredValues";
import { startEffortLevelsFor, reconcileEffortOnModelChange } from "@/lib/modelEffortOptions";
import { reconcileSpeed } from "@/lib/speedTiers";
import { sessionModeHandler, sessionDefaultModeOptions } from "@/lib/sessionDefaultModes";
import type { HarnessCapabilityMap } from "@/lib/sessionDefaultModes";
import { effortLevelsForFamily } from "@/lib/sessionCapabilities";

export type AgentField = "permission" | "model" | "effort" | "speed";
const LEGACY_EFFORT_FAMILIES: Readonly<Record<string, string>> = {
  "claude-native": "anthropic",
  "claude-sdk": "anthropic",
  codex: "codex-native",
  "codex-native": "codex-native",
  "pi-native": "pi",
  pi: "pi",
  "antigravity-native": "gemini",
  "devin-native": "anthropic",
};

/** Match runtime validation without depending on a host's model catalog. */
function canHoldAgentValue(
  harness: string,
  field: AgentField,
  value: string,
  capabilities?: HarnessCapabilityMap,
): boolean {
  if (field === "speed") {
    return sessionModeHandler(harness, "speed", capabilities) === "codex-service-tier"
      ? /^[a-z][a-z0-9_-]{0,63}$/.test(value)
      : sessionDefaultModeOptions(harness, "speed", [], null, "default", capabilities).some(
          (option) => option.value === value,
        );
  }
  if (field !== "effort") return true;
  const family = capabilities?.[harness]?.effort ?? LEGACY_EFFORT_FAMILIES[harness];
  if (!family || family === "dynamic") return true;
  if ((family === "pi" || family === "codex-native") && ["max", "ultra"].includes(value))
    return true;
  return effortLevelsForFamily(family === "codex-native" ? "openai" : family).includes(value);
}

const FIELDS = [
  { value: "permission", label: "Permission" },
  { value: "model", label: "Model" },
  { value: "effort", label: "Thinking" },
  { value: "speed", label: "Speed" },
] as const;
const CONCEPT_LABELS: Record<string, string> = {
  manual: "Ask every time",
  "edit-automatic": "Accept edits",
  automatic: "Auto",
  "read-only": "Read only",
};

export interface AgentDefaultsTableProps {
  hosts: readonly Host[];
  master: CallingDefaultsTable;
  catalogs: readonly CallingDefaultsCatalogRow[];
  capabilities?: HarnessCapabilityMap;
  field?: AgentField;
  harnesses?: readonly string[];
  names?: Readonly<Record<string, string>>;
  agentIds?: Readonly<Record<string, string>>;
  linkToDetails?: boolean;
  visibleHostId?: string;
  onChange: (table: CallingDefaultsTable) => void;
  onLoadCatalog?: (host: string, harness: string) => void;
}

function AgentLayerEditor({
  trigger,
  title,
  field,
  harness,
  initial,
  inherited,
  from,
  options,
  header,
  startOnlyEffort,
  hostWins,
  skipped,
  canPromote,
  onOpen,
  onSave,
  onClear,
  onPromote,
  belowCount,
  onClearBelow,
}: {
  trigger: ReactElement;
  title: string;
  field: AgentField;
  harness: string;
  initial?: string;
  inherited?: string;
  from: string;
  options: readonly PermissionOption[];
  header: boolean;
  startOnlyEffort?: boolean;
  hostWins?: boolean;
  skipped: (value: string) => string[];
  canPromote: boolean;
  onOpen: () => void;
  onSave: (value: string | null, hostWins?: boolean) => void;
  onClear: () => void;
  onPromote: (value: string) => void;
  belowCount: number;
  onClearBelow: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState(initial ?? "");
  const [marked, setMarked] = useState(hostWins ?? false);
  const [custom, setCustom] = useState(false);
  const effective = value || inherited;
  const skippedRows = header && effective ? skipped(effective) : [];
  const valid =
    !value ||
    (field === "model" ? !/\s/.test(value) : options.some((option) => option.value === value));
  return (
    <LayeredEditor
      trigger={trigger}
      title={title}
      open={open}
      onOpenChange={(next) => {
        if (next) {
          setValue(initial ?? "");
          setMarked(hostWins ?? false);
          setCustom(!!initial && !options.some((option) => option.value === initial));
          onOpen();
        }
        setOpen(next);
      }}
      from={from}
      savedHere={initial !== undefined && initial === inherited}
      onUseInherited={() => {
        onSave(null, marked);
        setOpen(false);
      }}
      useInheritedLabel="Use inherited"
      clearBelowCount={belowCount}
      onClearBelow={() => {
        onClearBelow();
        setOpen(false);
      }}
      onClear={() => {
        onClear();
        setOpen(false);
      }}
      actions={
        canPromote && effective ? (
          <LayeredEditorAction
            onClick={() => {
              onPromote(effective);
              setOpen(false);
            }}
          >
            Use on all hosts
          </LayeredEditorAction>
        ) : undefined
      }
      footer={
        <>
          <Button variant="outline" size="sm" onClick={() => setOpen(false)}>
            Cancel
          </Button>
          <Button
            size="sm"
            disabled={!valid}
            onClick={() => {
              onSave(value || null, marked);
              setOpen(false);
            }}
          >
            Save
          </Button>
        </>
      }
    >
      <div className="flex min-w-0 flex-col gap-1.5">
        <span className="inline-flex items-center gap-1.5 text-sm font-medium">
          {FIELDS.find((item) => item.value === field)?.label}
          {startOnlyEffort && (
            <HelpTip label="About this thinking setting">Applies when a chat starts.</HelpTip>
          )}
          {field === "speed" && ["claude-native", "claude-sdk"].includes(harness) && (
            <ClaudeFastModeHelp />
          )}
        </span>
        {field === "model" && (custom || options.length === 0) ? (
          <Input
            aria-label="Model"
            value={value}
            placeholder={inherited || "Agent's default"}
            onChange={(event) => setValue(event.target.value.trim())}
          />
        ) : (
          <Select
            value={value || "__inherit__"}
            onValueChange={(next) => setValue(next === "__inherit__" ? "" : next)}
          >
            <SelectTrigger
              aria-label={FIELDS.find((item) => item.value === field)?.label}
              className="w-full min-w-0"
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="__inherit__">
                Use inherited
                {inherited
                  ? ` (${options.find((option) => option.value === inherited)?.label ?? inherited})`
                  : ""}
              </SelectItem>
              {value && !options.some((option) => option.value === value) && (
                <SelectItem value={value} disabled>
                  Unavailable ({value})
                </SelectItem>
              )}
              {options.map((option) => (
                <SelectItem key={option.value} value={option.value}>
                  {option.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}
      </div>
      {field === "permission" && harness.startsWith("acp:") && value && (
        <p className="text-sm text-muted-foreground">
          {options.find((option) => option.value === value)?.description ??
            "Sets the agent's own mode. What it covers is up to the agent."}
        </p>
      )}
      {field === "model" && options.length > 0 && (
        <LayeredEditorAction onClick={() => setCustom(!custom)}>
          {custom ? "Choose a model" : "Enter another model"}
        </LayeredEditorAction>
      )}
      {hostWins !== undefined && (
        <label className="flex min-h-11 items-center justify-between gap-3 text-sm">
          <span>Host wins</span>
          <Switch aria-label="Host wins" checked={marked} onCheckedChange={setMarked} />
        </label>
      )}
      {skippedRows.length > 0 && (
        <p className="text-sm text-muted-foreground">Skipped on: {skippedRows.join(", ")}</p>
      )}
      {!valid && (
        <p role="alert" className="text-sm text-destructive">
          Choose an available value{field === "model" ? " without spaces" : ""}.
        </p>
      )}
    </LayeredEditor>
  );
}

/** Agents are rows, hosts are columns; only permission has cross-agent totals. */
export function AgentDefaultsTable({
  hosts,
  master,
  catalogs,
  capabilities,
  field: fixedField,
  harnesses: suppliedHarnesses,
  names,
  agentIds,
  linkToDetails = false,
  visibleHostId,
  onChange,
  onLoadCatalog,
}: AgentDefaultsTableProps) {
  const [selectedField, setSelectedField] = useState<AgentField>("permission");
  const [undo, setUndo] = useState<{
    before: CallingDefaultsTable;
    after: CallingDefaultsTable;
  } | null>(null);
  const [status, setStatus] = useState("");
  const field = fixedField ?? selectedField;
  const hostList = hosts.filter((host) => host.host_id !== "*");
  const harnesses = groupAgentHarnesses(
    suppliedHarnesses ?? [
      ...new Set([
        ...callingDefaultHarnesses(capabilities),
        ...hostList.flatMap((host) =>
          Object.keys(host.configured_harnesses ?? {}).filter((row) => row.startsWith("acp:")),
        ),
        ...Object.values(master).flatMap((entries) => Object.keys(entries)),
      ]),
    ],
    capabilities,
  );
  const layers = callingDefaultsLayers(master);
  const catalog = (row: string, col?: string) =>
    catalogs.filter((item) => item.harness === row && (!col || item.host_id === col));
  const permissions = (row: string, col?: string) =>
    callingPermissionOptions(row, capabilities, catalog(row, col)[0]?.permission_options);
  const models = (row: string, col?: string) => [
    ...new Map(
      catalog(row, col)
        .flatMap((item) => item.models)
        .map((item) => [item.id, item]),
    ).values(),
  ];
  const efforts = (row: string, col?: string) => [
    ...new Set(
      (col ? [col] : hostList.map((host) => host.host_id)).flatMap((host) => {
        const rows = models(row, host);
        const model = resolveAgentMasterField(master, host, row, "model", capabilities).value;
        return startEffortLevelsFor(row, rows, typeof model === "string" ? model : null);
      }),
    ),
  ];
  const speeds = (row: string, col?: string) => [
    ...new Map(
      (col ? [col] : hostList.map((host) => host.host_id))
        .flatMap((host) => {
          const model = resolveAgentMasterField(master, host, row, "model", capabilities).value;
          return sessionDefaultModeOptions(
            row,
            "speed",
            models(row, host),
            typeof model === "string" ? model : null,
            "default",
            capabilities,
          );
        })
        .map((option) => [option.value, option]),
    ).values(),
  ];
  const optionsFor = (row: string, col?: string) =>
    field === "permission"
      ? permissions(row, col)
      : field === "model"
        ? models(row, col).map((item) => ({ value: item.id, label: nativeModelLabel(item) }))
        : field === "speed"
          ? speeds(row, col)
          : efforts(row, col).map((value) => ({ value, label: normalizeEffortLabel(value) }));
  const layerOptions = (col?: string) =>
    callingLayerOptions(
      (row) => permissions(row, col),
      (row, key, value) => canHoldAgentValue(row, key, value, capabilities),
    );
  const resolve = (row: string, col: string) =>
    resolveAgentMasterField(master, col, row, field, capabilities, layerOptions(col));
  const label = (row: string) => names?.[row] ?? callingHarnessLabel(row, capabilities);
  const dependent = (row: string) =>
    agentProductHarness(row, capabilities) !== row &&
    harnesses.includes(agentProductHarness(row, capabilities));
  const hostLabel = (col: string) =>
    hostList.find((candidate) => candidate.host_id === col)?.name || col;
  const sourceLabel = (source: LayeredLevel | null) =>
    !source
      ? "Agent's default"
      : source.level === "corner"
        ? "All agents and hosts"
        : source.level === "row"
          ? `${label(source.row)} on all hosts`
          : source.level === "col"
            ? hostLabel(source.col)
            : "This cell";
  const valueLabel = (value: unknown, row?: string, col?: string) => {
    if (typeof value !== "string") return "Agent's default";
    return (
      (row
        ? optionsFor(row, col).find((option) => option.value === value)?.label
        : CONCEPT_LABELS[value]) ?? value
    );
  };
  const displayValue = (value: unknown, row: string, col?: string) => {
    const labelText = valueLabel(value, row, col);
    const fullAccess =
      field === "permission" &&
      typeof value === "string" &&
      permissions(row, col).some(
        (option) => option.value === value && option.concept === "full-access",
      );
    return fullAccess ? <span className="text-destructive">{labelText}</span> : labelText;
  };
  function save(next: CallingDefaultsTable, message = "Saved") {
    setUndo({ before: master, after: next });
    onChange(next);
    setStatus(message);
  }
  function undoSave() {
    if (!undo) return;
    const changed = Object.keys(undo.after).filter(
      (host) => !sameCallingHost(undo.before[host], undo.after[host]),
    );
    if (changed.some((host) => !sameCallingHost(master[host], undo.after[host]))) {
      setStatus("Changed since saved. Open the setting to review it.");
      setUndo(null);
      return;
    }
    onChange({
      ...master,
      ...Object.fromEntries(changed.map((host) => [host, undo.before[host] ?? {}])),
    });
    setUndo(null);
    setStatus("Undone");
  }
  function renderEditor(at: LayeredLevel, trigger: ReactElement) {
    const header = at.level === "corner" || at.level === "col";
    if (header && field !== "permission")
      return (
        <div className="flex min-h-11 items-center px-2.5 py-1.75">
          {(trigger.props as { children: ReactNode }).children}
        </div>
      );
    const [col, row] = callingLevelKeys(at);
    if (!header && field === "model" && !callingDefaultHasModels(row, capabilities))
      return (
        <div className="px-2.5 py-1.75">
          {(trigger.props as { children: ReactNode }).children}
          <span className="text-sm text-muted-foreground">No model options</span>
        </div>
      );
    const current = master[col]?.[row]?.[field];
    const inherited =
      at.level === "cell"
        ? resolveAgentMasterField(
            setCallingLayer(master, at, field, null),
            col,
            row,
            field,
            capabilities,
            layerOptions(col),
          )
        : inheritedAt(layers, at, field, header ? { colFields: ["permission"] } : layerOptions());
    const inheritedValue = header
      ? (inherited.value as { concept?: string } | undefined)?.concept
      : inherited.value;
    const options = header
      ? COLUMN_CONCEPTS.map((value) => ({ value, label: CONCEPT_LABELS[value] }))
      : optionsFor(row, col === "*" ? undefined : col);
    if (!header && field !== "model" && options.length === 0 && current === undefined)
      return (
        <div className="px-2.5 py-1.75">
          {(trigger.props as { children: ReactNode }).children}
          <span className="block text-sm text-muted-foreground">
            No advertised {field === "effort" ? "thinking" : field} options
          </span>
        </div>
      );
    const affectedRows = header
      ? harnesses.filter(
          (candidate) =>
            at.level !== "col" ||
            agentPresentOnHost(
              candidate,
              hostList.find((candidateHost) => candidateHost.host_id === col),
            ),
        )
      : [];
    const below = levelsBelow(layers, at).filter((level) => {
      const [host, harness] = callingLevelKeys(level);
      return (
        master[host]?.[harness]?.[field] !== undefined ||
        (field === "permission" && level.level === "col" && layers.hostWins?.[host])
      );
    });
    const title =
      at.level === "corner"
        ? "All agents and hosts"
        : at.level === "col"
          ? hostLabel(col)
          : at.level === "row"
            ? `${label(row)} on all hosts`
            : `${label(row)} on ${hostLabel(col)}`;
    return (
      <AgentLayerEditor
        key={field}
        trigger={trigger}
        title={title}
        field={field}
        harness={row}
        initial={current}
        inherited={typeof inheritedValue === "string" ? inheritedValue : undefined}
        from={
          "parent" in inherited && typeof inherited.parent === "string"
            ? label(inherited.parent)
            : sourceLabel(inherited.source)
        }
        options={options}
        header={header}
        startOnlyEffort={
          field === "effort" &&
          row.startsWith("acp:") &&
          models(row, col === "*" ? undefined : col).some(
            (model) => (model.start_efforts?.length ?? 0) > 0 && !model.efforts?.length,
          )
        }
        hostWins={at.level === "col" ? !!layers.hostWins?.[col] : undefined}
        skipped={(value) =>
          affectedRows
            .filter(
              (candidate) =>
                !layerOptions(col === "*" ? undefined : col).canHold(candidate, "permission", {
                  concept: value,
                }),
            )
            .map(label)
        }
        canPromote={
          at.level === "cell" && hostList.filter((host) => agentPresentOnHost(row, host)).length > 1
        }
        belowCount={below.length}
        onClearBelow={() => {
          let next = master;
          for (const level of below)
            next = setCallingLayer(
              next,
              level,
              field,
              null,
              field === "permission" && level.level === "col" ? false : undefined,
            );
          save(next, "Cleared values below");
        }}
        onOpen={() => {
          if (!header)
            for (const host of hostList.filter(
              (candidate) => col === "*" || col === candidate.host_id,
            ))
              onLoadCatalog?.(host.host_id, row);
        }}
        onSave={(value, marked) => {
          let next = setCallingLayer(master, at, field, value, marked);
          if (field === "model" && at.level === "cell") {
            const own = master[col]?.[row];
            const choices = models(row, col);
            if (own?.effort)
              next = setCallingLayer(
                next,
                at,
                "effort",
                reconcileEffortOnModelChange(row, choices, value, own.effort),
              );
            if (
              own?.speed &&
              sessionModeHandler(row, "speed", capabilities) === "codex-service-tier"
            )
              next = setCallingLayer(next, at, "speed", reconcileSpeed(own.speed, choices, value));
          }
          save(next);
        }}
        onClear={() =>
          save(
            setCallingLayer(master, at, field, null, at.level === "col" ? false : undefined),
            "Cleared",
          )
        }
        onPromote={(value) =>
          save(promoteCallingValueOnAllHosts(master, row, field, value), "Saved on all hosts")
        }
      />
    );
  }
  return (
    <div className="flex min-w-0 flex-col gap-2" data-testid="agent-defaults-table">
      <div className="flex flex-wrap items-center gap-2">
        {!fixedField && (
          <Segmented
            label="Agent default setting"
            options={FIELDS}
            value={field}
            onChange={setSelectedField}
          />
        )}
        <HelpTip label="About agent defaults">
          A cell beats its headers. The agent row beats an unmarked host; Host wins reverses those
          headers. A project can override these in its settings.
        </HelpTip>
      </div>
      <LayeredTable
        label={`${FIELDS.find((item) => item.value === field)?.label} defaults`}
        cornerLabel={field === "permission" ? "All agents" : "Agent"}
        cornerDetail={
          field === "permission" ? valueLabel(master["*"]?.["*"]?.permission) : undefined
        }
        cornerMark={
          field === "permission" &&
          COLUMN_CONCEPTS.some((concept) => concept === master["*"]?.["*"]?.permission)
        }
        rows={harnesses.map((row) => ({
          id: row,
          label: label(row),
          content: (
            <span className={dependent(row) ? "pl-4" : undefined}>
              <AgentNameTag
                agentId={agentIds?.[row] ?? null}
                name={label(row)}
                harness={row}
                markOnly={dependent(row)}
              />
              {dependent(row) && <span className="sr-only">{label(row)}</span>}
            </span>
          ),
          action: linkToDetails ? (
            <Link
              to={agentSettingsPath(row, visibleHostId)}
              aria-label={`${label(row)} details`}
              className="flex min-h-11 w-11 shrink-0 items-center justify-center rounded-sm text-muted-foreground hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring"
              componentId="settings.agents.detail"
            >
              <ArrowRightIcon className="size-4" aria-hidden />
            </Link>
          ) : undefined,
          detail: master["*"]?.[row]?.[field]
            ? displayValue(master["*"][row][field], row)
            : undefined,
          mark:
            master["*"]?.[row]?.[field] !== undefined &&
            layerOptions().canHold(row, field, master["*"][row][field]) &&
            layerOptions().normalize(row, field, master["*"][row][field]) !==
              inheritedAt(layers, { level: "row", row }, field, layerOptions()).value,
        }))}
        columns={hostList
          .filter((host) => !visibleHostId || host.host_id === visibleHostId)
          .map((host) => ({
            id: host.host_id,
            label: host.name || host.host_id,
            content: (
              <span className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
                {host.name || host.host_id}
                {field === "permission" && layers.hostWins?.[host.host_id] && (
                  <LayeredOwn>Host wins</LayeredOwn>
                )}
              </span>
            ),
            detail:
              field === "permission" && master[host.host_id]?.["*"]?.permission
                ? valueLabel(master[host.host_id]["*"].permission)
                : undefined,
            mark:
              field === "permission" &&
              COLUMN_CONCEPTS.some(
                (concept) => concept === master[host.host_id]?.["*"]?.permission,
              ) &&
              master[host.host_id]?.["*"]?.permission !== master["*"]?.["*"]?.permission,
          }))}
        renderEditor={renderEditor}
        renderCell={(row, col) => {
          const host = hostList.find((candidate) => candidate.host_id === col);
          if (!agentPresentOnHost(row, host))
            return { content: null, unavailable: missingCellText(row, host?.platform) };
          const resolved = resolve(row, col);
          const clashes = crossingClashes(layers, row, col, [field], layerOptions(col));
          const inherited = resolveAgentMasterField(
            setCallingLayer(master, { level: "cell", row, col }, field, null),
            col,
            row,
            field,
            capabilities,
            layerOptions(col),
          );
          const own = master[col]?.[row]?.[field];
          return {
            content: (
              <>
                {displayValue(resolved.value, resolved.parent ?? row, col)}
                {resolved.parent && (
                  <span className="block text-xs text-muted-foreground">
                    Follows {label(resolved.parent)}
                  </span>
                )}
              </>
            ),
            mark:
              own !== undefined &&
              resolved.source?.level === "cell" &&
              resolved.value !== inherited.value,
            summary: `From ${resolved.parent ? label(resolved.parent) : sourceLabel(resolved.source)}`,
            clash: clashes.length
              ? `${label(row)} and ${hostLabel(col)} differ. ${clashes[0].winner === "col" ? hostLabel(col) : label(row)} wins.`
              : undefined,
          };
        }}
        // oxlint-disable-next-line react/no-unstable-nested-components -- render prop, not a component
        selectionActions={(selected, clear) => (
          <Button
            variant="ghost"
            size="sm"
            disabled={!selected.length}
            onClick={() => {
              let next = master;
              for (const at of selected) next = setCallingLayer(next, at, field, null);
              save(next, "Cleared selected values");
              clear();
            }}
          >
            Clear selected
          </Button>
        )}
      />
      <div className="flex min-h-8 items-center gap-2" aria-live="polite">
        <span role="status" className="text-sm text-muted-foreground">
          {status}
        </span>
        {undo && (
          <Button variant="ghost" size="sm" onClick={undoSave}>
            Undo
          </Button>
        )}
      </div>
    </div>
  );
}
