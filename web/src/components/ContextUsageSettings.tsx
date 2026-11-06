import { ChevronRightIcon, PlusIcon, TriangleAlertIcon, XIcon } from "lucide-react";
import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactElement,
  type ReactNode,
  type Ref,
} from "react";

import { AgentBadgeMark } from "@/components/AgentBadge";
import { AGENT_MARK_CLASS, AgentIntegrationMark, AgentNameTag } from "@/components/AgentNameTag";
import { HelpTip } from "@/components/HelpTip";
import { LayeredEditor, LayeredEditorAction } from "@/components/LayeredEditor";
import { LayeredMark } from "@/components/LayeredTable";
import { Button } from "@/components/ui/button";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { Input } from "@/components/ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Segmented } from "@/components/ui/segmented";
import { Switch } from "@/components/ui/switch";
import {
  harnessSection,
  productIdFor,
  registrySourceLabels,
  rowsForAgentIds,
  useAgentRegistry,
  useAgentsWithSessions,
  type RegistryItem,
} from "@/hooks/useAgentRegistry";
import { useAgentBadgePreferences } from "@/hooks/useAgentBadgePreferences";
import { useCodexRateLimits, useHosts, type Host } from "@/hooks/useHosts";
import { useSession } from "@/hooks/useSession";
import { useStableProviderUsageLimits } from "@/hooks/useStableProviderUsageLimits";
import { useUsageContextPreferences } from "@/hooks/useUsageContextPreferences";
import { agentBadgeFor } from "@/lib/agentBadgePreferences";
import { formatTokenCountShort } from "@/lib/formatCost";
import { harnessUnavailableReasonOnHost } from "@/lib/harnessSetup";
import { missingCellText } from "@/lib/layeredValues";
import {
  formatProviderUsageLimits,
  providerUsageLimitsFromCodex,
  type ProviderUsageLimitsSnapshot,
} from "@/lib/providerUsageLimits";
import { useLocation, useNavigate, useSearchParams } from "@/lib/routing";
import {
  addUsageContextEntry,
  fixedBufferFitsCapacity,
  inheritedUsageContext,
  reportedUsageContext,
  resolveUsageContextLevel,
  seedUsageContextList,
  setUsageContextLevel,
  usageContextEntryKey,
  usageContextHasValue,
  usageContextKeysOfEntry,
  usageContextLevelKey,
  usageContextLevelLabel,
  usageContextRecordedEntries,
  usageContextRecordFromKey,
  usageContextRows,
  usageContextSourceFromKey,
  usageContextSourceKey,
  writeUsageContextPreferences,
  type ResolvedUsageContext,
  type UsageContextLevel,
  type UsageContextListEntry,
  type UsageContextOverride,
  type UsageContextPreferences,
  type UsageContextReported,
  type UsageContextSource,
} from "@/lib/usageContextPreferences";
import { cn } from "@/lib/utils";
import { useChatStore } from "@/store/chatStore";

interface Draft {
  context: string;
  buffer: string;
  percent: string;
  mode: "tokens" | "percent";
}

interface UndoState {
  overrides: Record<string, UsageContextOverride>;
  keys: string[];
  removedEntries: UsageContextListEntry[];
  message: string;
}

/** A row of the table: each kind meets each kind of column at exactly one level. */
interface ContextRow {
  id: string;
  testId: string;
  kind: "all" | "agent" | "model";
  entry: UsageContextListEntry;
  label: string;
}

interface ContextGroup {
  id: string;
  agent: ContextRow;
  models: ContextRow[];
}

interface ContextColumn {
  id: string;
  all: boolean;
  hostId: string;
  label: string;
  host: Host | null;
}

interface CellAddress {
  row: string;
  col: string;
}

const NO_HOST_COLUMN = "no-host";
const ALL_COLUMN: ContextColumn = {
  id: "all-computers",
  all: true,
  hostId: "",
  label: "All computers",
  host: null,
};
const ALL_ROW: ContextRow = {
  id: "all-agents",
  testId: "all-agents",
  kind: "all",
  entry: { agentName: "", harness: "", model: "" },
  label: "All agents",
};
const NOTHING_REPORTED: UsageContextReported = { contextWindow: null, autoCompactTokenLimit: null };
const ORDER_HELP =
  "One model on this computer, then that model on all computers, then the agent on this computer, then the agent on all computers, then all agents on this computer, then all agents, then the agent's own value.";

function slug(value: string): string {
  return (
    value
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "") || "none"
  );
}

function positiveNumber(value: string): number | null {
  if (value.trim() === "") return null;
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : null;
}

function bufferNumber(value: string, percent: boolean): number | null {
  if (value.trim() === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) &&
    parsed >= 0 &&
    (percent ? parsed < 100 : Number.isInteger(parsed))
    ? parsed
    : null;
}

function draftOf(override: UsageContextOverride | undefined): Draft {
  return {
    context: override?.contextWindowTokens?.toString() ?? "",
    buffer: override?.autoCompactBufferTokens?.toString() ?? "",
    percent: override?.autoCompactBufferPercent?.toString() ?? "",
    mode: override?.compactBufferMode ?? "tokens",
  };
}

/** Compact only when the shorthand does not round away entered digits. */
function contextTokenText(value: number): string {
  const compact = formatTokenCountShort(value);
  const divisor = { k: 1_000, m: 1_000_000, b: 1_000_000_000 }[compact.at(-1) ?? ""];
  if (!divisor) return compact;
  return Number(compact.slice(0, -1)) * divisor === value
    ? compact.replace(/[mb]$/, (unit) => unit.toUpperCase())
    : String(value);
}

function stateSummary(resolved: ResolvedUsageContext, own: boolean): string {
  const context = resolved.contextWindow.value;
  const buffer = resolved.buffer.value;
  const compact = resolved.autoCompactTokenLimit;
  return [
    `context ${context === null ? "Auto" : `${context} tokens`}`,
    `compact buffer ${buffer === null ? "Auto" : resolved.buffer.mode === "percent" ? `${buffer}%` : `${buffer} tokens`}`,
    compact === null ? "" : `compacts at ${Math.round(compact)} tokens`,
    own ? "saved here" : "",
  ]
    .filter(Boolean)
    .join(", ");
}

function agentRank(entry: UsageContextListEntry): number {
  if (entry.harness === "pi" || entry.harness === "pi-rpc") return 2;
  const section = harnessSection(null, entry.harness);
  return section === "native" ? 0 : section === "sdk" ? 1 : section === "acp" ? 2 : 3;
}

/** An agent's shortest name: the product's own, before any installed copy's suffix. */
function agentLabelFor(items: readonly RegistryItem[], agentName: string, harness: string): string {
  const product = items.find(
    (item) => item.key === `harness:${harness}` && item.name === agentName,
  );
  return product?.label ?? registrySourceLabels(items, agentName, harness).agentLabel;
}

interface RegistryView {
  items: readonly RegistryItem[];
  legacyProducts: Record<string, RegistryItem>;
}

/** The catalog id the agent's badge is saved under. */
function badgeIdFor(registry: RegistryView, agentName: string, harness: string): string | null {
  return productIdFor(
    registry.items.find((item) => item.name === agentName && item.harness === harness),
    registry,
  );
}

function groupIdOf(entry: UsageContextListEntry): string {
  return JSON.stringify([entry.agentName, entry.harness]);
}

/** One group per agent, its models beneath it, wherever in the list they were added. */
function contextGroups(
  entries: readonly UsageContextListEntry[],
  items: readonly RegistryItem[],
): ContextGroup[] {
  const groups = new Map<string, ContextGroup>();
  const usedTestIds = new Set<string>();
  const rowFor = (entry: UsageContextListEntry, label: string): ContextRow => {
    const base = [entry.agentName, entry.harness, entry.model].filter(Boolean).map(slug).join("-");
    let testId = base;
    let suffix = 2;
    while (usedTestIds.has(testId)) testId = `${base}-${suffix++}`;
    usedTestIds.add(testId);
    return {
      id: usageContextEntryKey(entry),
      testId,
      kind: entry.model ? "model" : "agent",
      entry,
      label,
    };
  };
  const sorted = [...entries].sort(
    (left, right) =>
      left.agentName.localeCompare(right.agentName) ||
      left.harness.localeCompare(right.harness) ||
      left.model.localeCompare(right.model),
  );
  for (const entry of sorted) {
    const id = groupIdOf(entry);
    if (!groups.has(id)) {
      const agentEntry = { agentName: entry.agentName, harness: entry.harness, model: "" };
      groups.set(id, {
        id,
        agent: rowFor(agentEntry, agentLabelFor(items, entry.agentName, entry.harness)),
        models: [],
      });
    }
    if (entry.model) groups.get(id)!.models.push(rowFor(entry, entry.model));
  }
  return [...groups.values()].sort(
    (left, right) =>
      agentRank(left.agent.entry) - agentRank(right.agent.entry) ||
      left.agent.label.localeCompare(right.agent.label),
  );
}

/** The level a row and a column meet at: the table is the resolution order, laid out. */
function levelOf(
  row: ContextRow,
  column: ContextColumn,
): { source: UsageContextSource; level: UsageContextLevel } {
  const { agentName, harness, model } = row.entry;
  const source: UsageContextSource = { hostId: column.hostId, agentName, harness, model };
  if (row.kind === "all") return { source, level: column.all ? "all" : "host" };
  if (row.kind === "agent") return { source, level: column.all ? "column" : "cell" };
  return { source, level: column.all ? "agent-model" : "model" };
}

function hostColumns(
  hosts: readonly Host[],
  preferences: UsageContextPreferences,
  liveHostId: string | null,
): ContextColumn[] {
  const byHostId = new Map<string, ContextColumn>();
  for (const host of hosts) {
    byHostId.set(host.host_id, {
      id: host.host_id,
      all: false,
      hostId: host.host_id,
      label: host.name || host.host_id,
      host,
    });
  }
  const addHost = (hostId: string) => {
    if (!byHostId.has(hostId))
      byHostId.set(hostId, { id: hostId, all: false, hostId, label: hostId, host: null });
  };
  let noHost = liveHostId === "";
  for (const key of Object.keys(preferences.overrides)) {
    const record = usageContextRecordFromKey(key);
    if (!record) continue;
    if (record.level !== "model" && record.level !== "cell" && record.level !== "host") continue;
    if (record.source.hostId) addHost(record.source.hostId);
    else noHost = true;
  }
  if (liveHostId) addHost(liveHostId);
  const columns = [...byHostId.values()].sort((left, right) =>
    left.label.localeCompare(right.label),
  );
  if (noHost)
    columns.push({ id: NO_HOST_COLUMN, all: false, hostId: "", label: "No host", host: null });
  return [ALL_COLUMN, ...columns];
}

function unavailableCell(row: ContextRow, column: ContextColumn): string | undefined {
  if (!column.host || row.kind === "all") return undefined;
  const { harness } = row.entry;
  const reason = harnessUnavailableReasonOnHost(harness, column.host);
  if (reason === "platform-unsupported") return missingCellText(harness, column.host.platform);
  if (reason === "binary-missing" || reason === "unconfigured")
    return missingCellText(harness, null);
  return undefined;
}

/** A level's own value for one field, as a comparable string, or null when it holds none. */
function ownField(
  override: UsageContextOverride | undefined,
  field: "contextWindowTokens" | "buffer",
): string | null {
  if (!override) return null;
  if (field === "contextWindowTokens") {
    const value = positiveNumber(String(override.contextWindowTokens ?? ""));
    return value === null ? null : String(value);
  }
  const mode = override.compactBufferMode ?? "tokens";
  const value =
    mode === "percent" ? override.autoCompactBufferPercent : override.autoCompactBufferTokens;
  return value === null || value === undefined ? null : `${mode}:${value}`;
}

/** Turn drafts into a level's record, or the message they cannot be saved with. */
function recordFromDraft(
  draft: Draft,
  inheritedContext: number | null,
): { record: UsageContextOverride | null } | { error: string } {
  const context = positiveNumber(draft.context);
  if (draft.context.trim() !== "" && context === null)
    return { error: "Enter a positive context total." };
  const active = draft.mode === "percent" ? draft.percent : draft.buffer;
  const buffer = bufferNumber(active, draft.mode === "percent");
  if (active.trim() !== "" && buffer === null)
    return {
      error:
        draft.mode === "percent"
          ? "Enter a percentage from 0 to less than 100."
          : "Enter a non-negative token buffer.",
    };
  if (draft.mode === "tokens" && !fixedBufferFitsCapacity(buffer, context ?? inheritedContext))
    return { error: "Token buffer must be less than the context total." };
  if (context === null && buffer === null) return { record: null };
  const tokens = bufferNumber(draft.buffer, false);
  const percent = bufferNumber(draft.percent, true);
  return {
    record: {
      contextWindowTokens: context,
      autoCompactBufferTokens: tokens,
      ...(percent !== null ? { autoCompactBufferPercent: percent } : {}),
      ...(tokens !== null || percent !== null ? { compactBufferMode: draft.mode } : {}),
    },
  };
}

/** Drop the fields a cell repeats from the value it would inherit anyway. */
function dropRepeatedFields(
  record: UsageContextOverride,
  inherited: ResolvedUsageContext,
  inheritedContext: number | null,
): UsageContextOverride | null {
  const next: UsageContextOverride = { ...record };
  if (next.contextWindowTokens === inheritedContext) next.contextWindowTokens = null;
  const mode = next.compactBufferMode ?? "tokens";
  const active =
    mode === "percent" ? (next.autoCompactBufferPercent ?? null) : next.autoCompactBufferTokens;
  if (active === inherited.buffer.value && mode === inherited.buffer.mode) {
    next.autoCompactBufferTokens = null;
    Reflect.deleteProperty(next, "autoCompactBufferPercent");
    Reflect.deleteProperty(next, "compactBufferMode");
  }
  return usageContextHasValue(next) ? next : null;
}

/** Compare against inheritance with the proposed cell context, but without its buffer. */
function dropRepeatedCellFields(
  preferences: UsageContextPreferences,
  source: UsageContextSource,
  record: UsageContextOverride,
): UsageContextOverride | null {
  const key = usageContextLevelKey(source, "cell");
  const contextOnly: UsageContextOverride = {
    contextWindowTokens: record.contextWindowTokens,
    autoCompactBufferTokens: null,
  };
  const withProposedContext = {
    ...preferences,
    overrides: { ...preferences.overrides, [key]: contextOnly },
  };
  return dropRepeatedFields(
    record,
    resolveUsageContextLevel(withProposedContext, source, "cell"),
    inheritedUsageContext(preferences, source, "cell").contextWindow.value,
  );
}

const VALUE_ROW = "grid grid-cols-[minmax(0,1fr)_7.5rem] items-center gap-2";
const VALUE_INPUT =
  "h-11 [appearance:textfield] text-base font-medium tabular-nums md:h-9 md:text-base [&::-webkit-inner-spin-button]:appearance-none [&::-webkit-outer-spin-button]:appearance-none";

type DraftField = "context" | "buffer";

/** Context total and Compact buffer, each with its unit and one line about its value. */
function DraftFields({
  draft,
  contextInputRef,
  onChange,
  onSubmit,
  labelFor,
  placeholder,
  note,
  error,
}: {
  draft: Draft;
  contextInputRef?: Ref<HTMLInputElement>;
  onChange: (next: Draft) => void;
  onSubmit: () => void;
  labelFor: (field: DraftField | "unit") => string;
  placeholder: (field: DraftField) => string;
  note?: (field: DraftField) => ReactNode;
  error: string;
}) {
  const submitOnEnter = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    onSubmit();
  };
  return (
    <div className="grid gap-3.5">
      <label className="grid gap-1.5">
        <span className="text-sm font-medium">Context total</span>
        <span className={VALUE_ROW}>
          <Input
            ref={contextInputRef}
            type="number"
            min={1}
            step={1000}
            value={draft.context}
            placeholder={placeholder("context")}
            aria-label={labelFor("context")}
            className={VALUE_INPUT}
            onChange={(event) => onChange({ ...draft, context: event.target.value })}
            onKeyDown={submitOnEnter}
          />
          <span className="px-1 text-sm text-muted-foreground">tokens</span>
        </span>
        {note ? <span className="text-xs text-muted-foreground">{note("context")}</span> : null}
      </label>
      <div className="grid gap-1.5">
        <span className="flex items-center gap-1.5 text-sm font-medium">
          Compact buffer
          <HelpTip label="About Compact buffer" className="-my-3 size-11 md:my-0 md:size-4">
            Compact point = context total minus this buffer.
          </HelpTip>
        </span>
        <div className={VALUE_ROW}>
          <Input
            type="number"
            min={0}
            max={draft.mode === "percent" ? 99.999 : undefined}
            step={draft.mode === "percent" ? "any" : 1}
            value={draft.mode === "percent" ? draft.percent : draft.buffer}
            placeholder={placeholder("buffer")}
            aria-label={labelFor("buffer")}
            className={VALUE_INPUT}
            onChange={(event) =>
              onChange(
                draft.mode === "percent"
                  ? { ...draft, percent: event.target.value }
                  : { ...draft, buffer: event.target.value },
              )
            }
            onKeyDown={submitOnEnter}
          />
          <Segmented
            quiet
            className="justify-self-start"
            label={labelFor("unit")}
            value={draft.mode}
            options={[
              { value: "tokens", label: "Tokens" },
              { value: "percent", label: "%", title: "Percent of the context total" },
            ]}
            onChange={(mode) => onChange({ ...draft, mode })}
          />
        </div>
        {note ? <span className="text-xs text-muted-foreground">{note("buffer")}</span> : null}
      </div>
      {error ? (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      ) : null}
    </div>
  );
}

/** The two fields for one stored level; an empty one shows what it would inherit. */
function LevelFields({
  preferences,
  source,
  level,
  label,
  levelText,
  reported,
  draft,
  onChange,
  onSubmit,
  error,
}: {
  preferences: UsageContextPreferences;
  source: UsageContextSource;
  level: UsageContextLevel;
  label: string;
  levelText: (level: UsageContextLevel) => string;
  reported: UsageContextReported;
  draft: Draft;
  onChange: (next: Draft) => void;
  onSubmit: () => void;
  error: string;
}) {
  const key = usageContextLevelKey(source, level);
  const stored = draftOf(preferences.overrides[key]);
  const inherited = inheritedUsageContext(preferences, source, level, reported);
  const inheritedBuffer = inherited.buffer.value;
  const autoText = (value: number | null) =>
    value === null
      ? "Auto: the agent's own value"
      : `Auto: the agent reports ${contextTokenText(value)}`;
  const fromText = (winner: UsageContextLevel | "reported", auto: number | null) =>
    winner === "reported" ? autoText(auto) : `From ${levelText(winner)}`;
  const valueNote = (
    typed: string,
    saved: string,
    winner: UsageContextLevel | "reported",
    auto: number | null,
  ) => {
    if (typed.trim() === "") return fromText(winner, auto);
    if (typed !== saved) return "Not saved yet";
    return (
      <span className="inline-flex items-center gap-1.5">
        <LayeredMark />
        Saved here
      </span>
    );
  };
  const outcome = recordFromDraft(draft, inherited.contextWindow.value);
  const preview = (() => {
    if ("error" in outcome) return null;
    const overrides = { ...preferences.overrides };
    if (outcome.record) overrides[key] = outcome.record;
    else Reflect.deleteProperty(overrides, key);
    return resolveUsageContextLevel({ ...preferences, overrides }, source, level, reported)
      .autoCompactTokenLimit;
  })();
  const reportedPoint = reported.autoCompactTokenLimit;
  return (
    <DraftFields
      draft={draft}
      onChange={onChange}
      onSubmit={onSubmit}
      error={error}
      labelFor={(field) =>
        field === "unit"
          ? `Buffer unit for ${label}`
          : `${field === "context" ? "Context total" : "Compact buffer"} for ${label}`
      }
      placeholder={(field) =>
        field === "context"
          ? (inherited.contextWindow.value?.toString() ?? "Auto")
          : inheritedBuffer === null
            ? "Auto"
            : `${inheritedBuffer}${inherited.buffer.mode === "percent" ? "%" : ""}`
      }
      note={(field) =>
        field === "context" ? (
          valueNote(
            draft.context,
            stored.context,
            inherited.contextWindow.level,
            reported.contextWindow,
          )
        ) : (
          <span className="grid gap-0.5">
            <span>
              {valueNote(
                draft.mode === "percent" ? draft.percent : draft.buffer,
                draft.mode === "percent" ? stored.percent : stored.buffer,
                inherited.buffer.level,
                inherited.buffer.level === "reported" ? inheritedBuffer : null,
              )}
            </span>
            {preview !== null ? (
              <span data-testid="context-compact-point">
                Compacts at {contextTokenText(Math.round(preview))}
                {reportedPoint !== null && reportedPoint !== preview ? (
                  <span className="text-warning">
                    {" "}
                    · the agent reports {contextTokenText(reportedPoint)}
                  </span>
                ) : null}
              </span>
            ) : null}
          </span>
        )
      }
    />
  );
}

/** Validate a draft and turn it into the record its level stores. */
function recordForLevel(
  preferences: UsageContextPreferences,
  source: UsageContextSource,
  level: UsageContextLevel,
  reported: UsageContextReported,
  draft: Draft,
): { record: UsageContextOverride | null } | { error: string } {
  const inheritedContext = inheritedUsageContext(preferences, source, level, reported).contextWindow
    .value;
  const outcome = recordFromDraft(draft, inheritedContext);
  if ("error" in outcome || level !== "cell" || !outcome.record) return outcome;
  return { record: dropRepeatedCellFields(preferences, source, outcome.record) };
}

function EditorButtons({ onCancel, onSave }: { onCancel: () => void; onSave: () => void }) {
  return (
    <>
      <Button
        type="button"
        variant="outline"
        size="sm"
        className="min-h-11 md:min-h-7"
        onClick={onCancel}
      >
        Cancel
      </Button>
      <Button type="button" size="sm" className="min-h-11 md:min-h-7" onClick={onSave}>
        Save
      </Button>
    </>
  );
}

/** The editor of one cell: one level, written whole on Save. */
function ValueEditor({
  trigger,
  title,
  note,
  open,
  onOpenChange,
  preferences,
  source,
  level,
  label,
  levelText,
  reported,
  clearBelowCount,
  onClearBelow,
}: {
  trigger: ReactElement;
  title: string;
  note?: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  preferences: UsageContextPreferences;
  source: UsageContextSource;
  level: UsageContextLevel;
  label: string;
  levelText: (level: UsageContextLevel) => string;
  reported: UsageContextReported;
  clearBelowCount: number;
  onClearBelow: () => void;
}) {
  const key = usageContextLevelKey(source, level);
  const own = preferences.overrides[key];
  const [draft, setDraft] = useState<Draft>(() => draftOf(own));
  const [error, setError] = useState("");
  const storedSignature = JSON.stringify(own ?? null);
  useEffect(() => {
    setDraft(draftOf(own));
    setError("");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, storedSignature]);
  const save = () => {
    const outcome = recordForLevel(preferences, source, level, reported, draft);
    if ("error" in outcome) {
      setError(outcome.error);
      return;
    }
    setUsageContextLevel(preferences, key, outcome.record);
    onOpenChange(false);
  };
  const clear = usageContextHasValue(own)
    ? () => setUsageContextLevel(preferences, key, null)
    : undefined;
  return (
    <LayeredEditor
      trigger={trigger}
      title={title}
      note={note}
      help={ORDER_HELP}
      open={open}
      onOpenChange={onOpenChange}
      useInheritedLabel={level === "all" && clear ? "Use Auto" : undefined}
      onUseInherited={level === "all" ? clear : undefined}
      onClear={level === "all" ? undefined : clear}
      clearBelowCount={clearBelowCount}
      onClearBelow={clearBelowCount > 0 ? onClearBelow : undefined}
      footer={<EditorButtons onCancel={() => onOpenChange(false)} onSave={save} />}
    >
      <LevelFields
        preferences={preferences}
        source={source}
        level={level}
        label={label}
        levelText={levelText}
        reported={reported}
        draft={draft}
        onChange={setDraft}
        onSubmit={save}
        error={error}
      />
    </LayeredEditor>
  );
}

interface PickerOption {
  key: string;
  /** What the search matches. */
  value: string;
  entry: UsageContextListEntry;
  content: ReactNode;
}

/** A searchable list of entries to add; `typedEntry` turns the search itself into one. */
function EntryPicker({
  trigger,
  label,
  placeholder,
  heading,
  options,
  typedEntry,
  emptyText,
  testId,
  opensEditor = false,
  onAdd,
}: {
  trigger: ReactElement;
  label: string;
  placeholder: string;
  heading: string;
  options: readonly PickerOption[];
  /** The entry the search itself would add, or null when it adds nothing. */
  typedEntry?: (typed: string) => UsageContextListEntry | null;
  emptyText: (typed: string) => string;
  testId: string;
  /** The choice opens a cell editor, which keeps the focus the closing picker would take back. */
  opensEditor?: boolean;
  onAdd: (entry: UsageContextListEntry) => void;
}) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const chosenRef = useRef(false);
  const typed = search.trim().slice(0, 256);
  const typedChoice = typed && typedEntry ? typedEntry(typed) : null;
  const choose = (entry: UsageContextListEntry) => {
    chosenRef.current = true;
    setOpen(false);
    setSearch("");
    onAdd(entry);
  };
  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (!next) setSearch("");
      }}
    >
      <PopoverTrigger asChild>{trigger}</PopoverTrigger>
      <PopoverContent
        align="start"
        data-pop=""
        className="w-[min(24rem,calc(100vw-1rem))] p-0"
        data-testid={testId}
        onCloseAutoFocus={(event) => {
          if (opensEditor && chosenRef.current) event.preventDefault();
          chosenRef.current = false;
        }}
      >
        <Command label={label}>
          <CommandInput value={search} onValueChange={setSearch} placeholder={placeholder} />
          <CommandList>
            <CommandEmpty>{emptyText(typed)}</CommandEmpty>
            {options.length > 0 ? (
              <CommandGroup heading={heading}>
                {options.map((option) => (
                  <CommandItem
                    key={option.key}
                    value={option.value}
                    onSelect={() => choose(option.entry)}
                  >
                    {option.content}
                  </CommandItem>
                ))}
              </CommandGroup>
            ) : null}
            {typedChoice ? (
              <CommandGroup forceMount>
                <CommandItem forceMount value={`add ${typed}`} onSelect={() => choose(typedChoice)}>
                  <span className="min-w-0 wrap-anywhere">
                    Add <span className="font-medium">{typed}</span>
                  </span>
                </CommandItem>
              </CommandGroup>
            ) : null}
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}

/** Two value slots on one grid, shared with the column's header so everything lines up. */
const SLOT_GRID = "grid w-full grid-cols-[repeat(2,minmax(3.5rem,1fr))] gap-x-2";
const CELL_PADDING = "pr-1 pl-3.5";
/** Room for the warning mark, kept on every cell so a warning never moves a number. */
const WARNING_GUTTER = "flex w-11 shrink-0 items-center justify-center md:w-5";

function ValueSlot({
  slot,
  text,
  own,
  fromAgent,
}: {
  slot: "total" | "buffer";
  text: string;
  own: boolean;
  fromAgent: boolean;
}) {
  return (
    <span
      data-context-slot={slot}
      className={cn(
        "relative whitespace-nowrap tabular-nums",
        own ? "font-semibold text-foreground" : "text-muted-foreground",
        fromAgent && "italic",
      )}
    >
      {own ? (
        <span className="absolute top-1/2 -left-2.5 flex -translate-y-1/2">
          <LayeredMark />
        </span>
      ) : null}
      {text}
    </span>
  );
}

function ValueSlots({
  resolved,
  level,
}: {
  resolved: ResolvedUsageContext;
  level: UsageContextLevel;
}) {
  const context = resolved.contextWindow.value;
  const buffer = resolved.buffer.value;
  const bufferPercent = resolved.buffer.mode === "percent" && buffer !== null;
  return (
    <span className={SLOT_GRID}>
      <ValueSlot
        slot="total"
        text={context === null ? "Auto" : contextTokenText(context)}
        own={resolved.contextWindow.level === level}
        fromAgent={resolved.contextWindow.level === "reported"}
      />
      <ValueSlot
        slot="buffer"
        text={buffer === null ? "Auto" : bufferPercent ? `${buffer}%` : contextTokenText(buffer)}
        own={resolved.buffer.level === level}
        fromAgent={resolved.buffer.level === "reported"}
      />
    </span>
  );
}

/** What a cell should tell about itself: disagreement with the agent, its headers, or itself. */
function cellWarning(
  preferences: UsageContextPreferences,
  row: ContextRow,
  column: ContextColumn,
  resolved: ResolvedUsageContext,
  reported: UsageContextReported,
  agentLabel: string,
): string | undefined {
  if (row.kind === "all") return undefined;
  const { source, level } = levelOf(row, column);
  const lines: string[] = [];
  const reportedWindow = reported.contextWindow;
  if (
    resolved.contextWindow.level !== "reported" &&
    resolved.contextWindow.value !== null &&
    reportedWindow !== null &&
    resolved.contextWindow.value !== reportedWindow
  ) {
    lines.push(
      `Context ${contextTokenText(resolved.contextWindow.value)} here; the agent reports ${contextTokenText(reportedWindow)}.`,
    );
  }
  const reportedPoint = reported.autoCompactTokenLimit;
  if (
    resolved.buffer.level !== "reported" &&
    resolved.autoCompactTokenLimit !== null &&
    reportedPoint !== null &&
    resolved.autoCompactTokenLimit !== reportedPoint
  ) {
    lines.push(
      `Compacts at ${contextTokenText(Math.round(resolved.autoCompactTokenLimit))} here; the agent reports ${contextTokenText(reportedPoint)}.`,
    );
  }
  if (resolved.ignored.some((item) => item.level === level))
    lines.push("The buffer saved here is ignored: it is not below the context total.");
  if (level === "cell") {
    const cell = preferences.overrides[usageContextLevelKey(source, "cell")];
    const fromRow = preferences.overrides[usageContextLevelKey(source, "column")];
    const fromHost = preferences.overrides[usageContextLevelKey(source, "host")];
    const fields = [
      { key: "contextWindowTokens" as const, label: "Context total" },
      { key: "buffer" as const, label: "Compact buffer" },
    ];
    for (const field of fields) {
      if (ownField(cell, field.key) !== null) continue;
      const rowValue = ownField(fromRow, field.key);
      const hostValue = ownField(fromHost, field.key);
      if (rowValue === null || hostValue === null || rowValue === hostValue) continue;
      const winner =
        field.key === "contextWindowTokens" ? resolved.contextWindow.level : resolved.buffer.level;
      const winnerLabel =
        winner === "column"
          ? agentLabel
          : winner === "host"
            ? column.label
            : usageContextLevelLabel(winner, { hostLabel: column.label, agentLabel });
      lines.push(`${field.label}: ${agentLabel} and ${column.label} differ; ${winnerLabel} wins.`);
    }
  }
  return lines.length ? lines.join("\n") : undefined;
}

const TABLE_CELL = "h-px border-b border-border bg-background p-0 text-left align-middle";
const FIRST_COLUMN =
  "lt-c sticky left-0 z-10 min-w-44 max-w-44 border-r font-normal md:min-w-44 md:max-w-56";
const BODY_COLUMN = "border-l-0";
const ROW_HEIGHT = "min-h-11 md:min-h-8";
const ROW_ACTION =
  "flex h-11 w-8 shrink-0 items-center justify-center rounded-sm text-muted-foreground opacity-100 transition-opacity hover:text-foreground focus-visible:opacity-100 md:size-6 md:opacity-0 md:group-hover/label:opacity-100";

export function ContextUsageSettings() {
  const preferences = useUsageContextPreferences();
  const badges = useAgentBadgePreferences();
  const registry = useAgentRegistry();
  const sessions = useAgentsWithSessions();
  const { data: hosts = [] } = useHosts();
  const conversationId = useChatStore((state) => state.conversationId);
  const liveAgentName = useChatStore((state) => state.boundAgentName);
  const liveHarness = useChatStore((state) => state.sessionHarness);
  const liveModel = useChatStore((state) => state.llmModel);
  const sessionProviderUsageLimits = useChatStore((state) => state.providerUsageLimits);
  const { session } = useSession(conversationId);
  const liveHostId: string | null = conversationId ? (session?.hostId ?? "") : null;
  const [openEditor, setOpenEditor] = useState<CellAddress | null>(null);
  const [openGroups, setOpenGroups] = useState<readonly string[]>([]);
  const [missingSource, setMissingSource] = useState(false);
  const [undo, setUndo] = useState<UndoState | null>(null);
  const searchParams = useSearchParams()[0];
  const navigate = useNavigate();
  const location = useLocation();
  const consumedRef = useRef<string | null>(null);
  const focusEditorRef = useRef(false);
  const revealRowRef = useRef<string | null>(null);

  const sessionRows = sessions.data
    ? rowsForAgentIds(registry.items, registry.legacyProducts, sessions.data)
    : null;
  const inUseKnown = !registry.isLoading && !registry.isError && sessionRows !== null;
  const inUseFailed = registry.isError || sessions.isError;
  const inUse: UsageContextListEntry[] = registry.items.flatMap((item) =>
    item.deployed && item.name && item.harness && sessionRows?.has(item.key)
      ? [{ agentName: item.name, harness: item.harness, model: "" }]
      : [],
  );

  // The list starts from what is in use, once; after that only the user changes it.
  useEffect(() => {
    if (inUseKnown && !preferences.contextList) seedUsageContextList(preferences, inUse);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [inUseKnown, preferences.contextList]);

  const { groups, columns, rowById, entries } = useMemo(() => {
    const listed = usageContextRows(preferences);
    const grouped = contextGroups(listed, registry.items);
    const rows = [ALL_ROW, ...grouped.flatMap((group) => [group.agent, ...group.models])];
    return {
      entries: listed,
      groups: grouped,
      columns: hostColumns(hosts, preferences, liveHostId),
      rowById: new Map(rows.map((row) => [row.id, row])),
    };
  }, [registry.items, preferences, hosts, liveHostId]);

  const severalOpen = preferences.contextList?.severalOpen ?? false;
  const labelOfEntry = (entry: UsageContextListEntry) =>
    agentLabelFor(registry.items, entry.agentName, entry.harness);
  const badgeIdOfEntry = (entry: UsageContextListEntry) =>
    badgeIdFor(registry, entry.agentName, entry.harness);
  const reportedAt = (row: ContextRow, column: ContextColumn): UsageContextReported =>
    row.kind === "all"
      ? NOTHING_REPORTED
      : reportedUsageContext(preferences, {
          ...row.entry,
          hostId: column.all ? null : column.hostId,
        });
  const resolveAt = (row: ContextRow, column: ContextColumn): ResolvedUsageContext => {
    const { source, level } = levelOf(row, column);
    return resolveUsageContextLevel(preferences, source, level, reportedAt(row, column));
  };
  const cellTitle = (row: ContextRow, column: ContextColumn): string => {
    if (row.kind === "all")
      return column.all ? "All agents, all computers" : `All agents on ${column.label}`;
    const agent = labelOfEntry(row.entry);
    const name = row.kind === "model" ? `${agent} · ${row.entry.model}` : agent;
    return column.all ? `${name}, all computers` : `${name} on ${column.label}`;
  };
  const levelText = (row: ContextRow, column: ContextColumn) => (level: UsageContextLevel) =>
    usageContextLevelLabel(level, {
      hostLabel: column.all ? "No host" : column.label,
      agentLabel: labelOfEntry(row.entry),
    });

  /** Every saved value this cell's row and column reach, itself excluded. */
  const keysBelow = (row: ContextRow, column: ContextColumn): string[] => {
    const { source, level } = levelOf(row, column);
    const own = usageContextLevelKey(source, level);
    return Object.keys(preferences.overrides).filter((key) => {
      if (key === own) return false;
      const record = usageContextRecordFromKey(key);
      if (!record || !usageContextHasValue(preferences.overrides[key])) return false;
      const sameRow =
        row.kind === "all" ||
        (record.source.agentName === row.entry.agentName &&
          record.source.harness === row.entry.harness &&
          (row.kind === "agent" || record.source.model === row.entry.model));
      const hostLevel =
        record.level === "model" || record.level === "cell" || record.level === "host";
      return sameRow && (column.all || (hostLevel && record.source.hostId === column.hostId));
    });
  };

  const applyChange = (
    overrides: Record<string, UsageContextOverride>,
    message: string,
    removedEntries: UsageContextListEntry[] = [],
  ) => {
    const keys = [
      ...new Set([...Object.keys(preferences.overrides), ...Object.keys(overrides)]),
    ].filter((key) => preferences.overrides[key] !== overrides[key]);
    setUndo({ overrides: preferences.overrides, keys, removedEntries, message });
    const removed = new Set(removedEntries.map(usageContextEntryKey));
    writeUsageContextPreferences({
      ...preferences,
      overrides,
      ...(preferences.contextList && removed.size > 0
        ? {
            contextList: {
              ...preferences.contextList,
              entries: preferences.contextList.entries.filter(
                (entry) => !removed.has(usageContextEntryKey(entry)),
              ),
            },
          }
        : {}),
    });
  };
  /** Puts back only what the action changed, so later edits to other rows survive. */
  const restoreUndo = () => {
    if (!undo) return;
    const overrides = { ...preferences.overrides };
    for (const key of undo.keys) {
      if (Object.hasOwn(undo.overrides, key)) overrides[key] = undo.overrides[key]!;
      else Reflect.deleteProperty(overrides, key);
    }
    writeUsageContextPreferences({
      ...preferences,
      overrides,
      ...(preferences.contextList
        ? {
            contextList: {
              ...preferences.contextList,
              entries: [...preferences.contextList.entries, ...undo.removedEntries],
            },
          }
        : {}),
    });
    setUndo(null);
  };
  const clearBelow = (row: ContextRow, column: ContextColumn) => {
    const overrides = { ...preferences.overrides };
    const keys = keysBelow(row, column);
    for (const key of keys) Reflect.deleteProperty(overrides, key);
    applyChange(overrides, `Cleared ${keys.length} ${keys.length === 1 ? "value" : "values"}.`);
  };
  /** A row and its saved values go together, or the value would keep applying unseen. */
  const removeRow = (row: ContextRow) => {
    const overrides = { ...preferences.overrides };
    const keys = usageContextKeysOfEntry(preferences, row.entry);
    for (const key of keys) Reflect.deleteProperty(overrides, key);
    const removedEntries = (preferences.contextList?.entries ?? []).filter((entry) =>
      row.kind === "agent"
        ? groupIdOf(entry) === groupIdOf(row.entry)
        : usageContextEntryKey(entry) === row.id,
    );
    const saved = keys.length
      ? ` and ${keys.length} saved ${keys.length === 1 ? "value" : "values"}`
      : "";
    const name = row.kind === "agent" ? row.label : `${labelOfEntry(row.entry)} · ${row.label}`;
    applyChange(overrides, `Removed ${name}${saved}.`, removedEntries);
  };

  const toggleGroup = (id: string) => {
    setOpenGroups((current) => {
      if (current.includes(id)) return current.filter((item) => item !== id);
      return severalOpen ? [...current, id] : [id];
    });
  };
  const showGroup = (id: string) => {
    setOpenGroups((current) => {
      if (current.includes(id)) return current;
      return severalOpen ? [...current, id] : [id];
    });
  };
  const addEntry = (entry: UsageContextListEntry) => {
    addUsageContextEntry(preferences, entry);
    if (entry.model) showGroup(groupIdOf(entry));
    revealRowRef.current = usageContextEntryKey(entry);
  };
  /** A model added from its agent's name opens straight into its values on all computers. */
  const addModel = (entry: UsageContextListEntry) => {
    addEntry(entry);
    focusEditorRef.current = true;
    setOpenEditor({ row: usageContextEntryKey(entry), col: ALL_COLUMN.id });
  };
  const setSeveralOpen = (next: boolean) => {
    if (!preferences.contextList) return;
    writeUsageContextPreferences({
      ...preferences,
      contextList: { ...preferences.contextList, severalOpen: next },
    });
    if (!next) setOpenGroups((current) => current.slice(-1));
  };

  const sessionKey = usageContextSourceKey({
    hostId: liveHostId ?? "",
    agentName: liveAgentName ?? "",
    harness: liveHarness ?? "",
    model: liveModel ?? "",
  });

  // The ring's entry: open this session's model on all computers, then drop `ctx`
  // from the URL so a reload does not reopen it. Waits for the seed, also while what
  // is in use is loading, since adding the session's row would pre-empt it; only a
  // failed load, which never seeds, lets the entry go first.
  useEffect(() => {
    const param = searchParams.get("ctx");
    if (!param) {
      consumedRef.current = null;
      return;
    }
    if (consumedRef.current === param) return;
    if (!preferences.contextList && !inUseFailed) return;
    consumedRef.current = param;
    const source = usageContextSourceFromKey(param);
    if (source?.agentName && source.harness) {
      const entry = { agentName: source.agentName, harness: source.harness, model: source.model };
      addUsageContextEntry(preferences, entry);
      showGroup(groupIdOf(entry));
      focusEditorRef.current = true;
      setOpenEditor({ row: usageContextEntryKey(entry), col: ALL_COLUMN.id });
      setMissingSource(false);
    } else {
      setMissingSource(true);
    }
    const next = new URLSearchParams(searchParams);
    next.delete("ctx");
    navigate({ search: next.toString(), hash: location.hash }, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams, preferences.contextList, inUseFailed]);

  // Bring the cell the open editor belongs to on screen: a float anchored past the
  // viewport edge cannot be reached.
  useEffect(() => {
    if (!openEditor) return;
    const focusFirstField = focusEditorRef.current;
    focusEditorRef.current = false;
    const row = rowById.get(openEditor.row);
    if (!row) return;
    const timer = window.setTimeout(() => {
      document
        .querySelector(`[data-testid="context-cell-${row.testId}-${openEditor.col}"]`)
        ?.scrollIntoView?.({ block: "center", inline: "center" });
      if (focusFirstField)
        document.querySelector<HTMLInputElement>('[data-pop][data-state="open"] input')?.focus();
    }, 0);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openEditor]);

  // An added row may land off screen: show where it went.
  useEffect(() => {
    const id = revealRowRef.current;
    const row = id ? rowById.get(id) : null;
    if (!row) return;
    revealRowRef.current = null;
    document
      .querySelector(`[data-testid="context-row-${row.testId}"]`)
      ?.scrollIntoView?.({ block: "nearest" });
  }, [rowById, openGroups]);

  const codexSession = liveHarness === "codex" || liveHarness === "codex-native";
  const { data: codexRateLimits, isLoading: rateLimitsLoading } = useCodexRateLimits(
    session?.hostId ?? null,
    codexSession && session?.hostId != null,
  );
  const providerLimits: ProviderUsageLimitsSnapshot | null = useStableProviderUsageLimits({
    preferences,
    sourceKey: sessionKey,
    fresh: codexSession
      ? providerUsageLimitsFromCodex(codexRateLimits, liveModel)
      : sessionProviderUsageLimits,
    agentName: liveAgentName,
    harness: liveHarness,
  });
  const currentProviderLimits = formatProviderUsageLimits(providerLimits) ? providerLimits : null;
  const harnessName = registrySourceLabels(registry.items, liveAgentName, liveHarness).harnessLabel;
  const providerStatus = currentProviderLimits
    ? `${currentProviderLimits.provider} · ${currentProviderLimits.windows.length}`
    : rateLimitsLoading
      ? "Checking…"
      : liveHarness
        ? "No data"
        : "Open a session";
  const providerStatusDetail = currentProviderLimits
    ? `${currentProviderLimits.provider} usage limits for this session.`
    : rateLimitsLoading
      ? "Checking usage limits."
      : liveHarness
        ? `No usage limits received from ${harnessName} for this session.`
        : "Open a session to view usage limits.";

  const renderCell = (row: ContextRow, column: ContextColumn) => {
    const unavailable = unavailableCell(row, column);
    if (unavailable) {
      return (
        <td key={column.id} className={cn(TABLE_CELL, BODY_COLUMN)}>
          <span
            className={cn(
              "flex size-full items-center py-1 text-xs text-muted-foreground",
              ROW_HEIGHT,
              CELL_PADDING,
            )}
          >
            {unavailable}
          </span>
        </td>
      );
    }
    const { source, level } = levelOf(row, column);
    const resolved = resolveAt(row, column);
    const reported = reportedAt(row, column);
    const own = resolved.contextWindow.level === level || resolved.buffer.level === level;
    const summary = stateSummary(resolved, own);
    const title = cellTitle(row, column);
    const address = { row: row.id, col: column.id };
    const warning = cellWarning(
      preferences,
      row,
      column,
      resolved,
      reported,
      labelOfEntry(row.entry),
    );
    const belowCount = keysBelow(row, column).length;
    return (
      <td key={column.id} className={cn(TABLE_CELL, BODY_COLUMN)}>
        <span className="flex size-full">
          <ValueEditor
            trigger={
              <button
                type="button"
                aria-label={`Edit ${title}: ${summary}`}
                title={summary}
                data-testid={`context-cell-${row.testId}-${column.id}`}
                className={cn(
                  "flex min-w-0 flex-1 items-center py-1 text-left text-sm outline-none transition-colors hover:bg-muted focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring aria-expanded:bg-muted",
                  ROW_HEIGHT,
                  CELL_PADDING,
                )}
              >
                <ValueSlots resolved={resolved} level={level} />
              </button>
            }
            title={title}
            note={row.kind === "agent" ? "All models" : undefined}
            open={openEditor?.row === row.id && openEditor.col === column.id}
            onOpenChange={(next) => {
              focusEditorRef.current = false;
              setOpenEditor(next ? address : null);
            }}
            preferences={preferences}
            source={source}
            level={level}
            label={title}
            levelText={levelText(row, column)}
            reported={reported}
            clearBelowCount={belowCount}
            onClearBelow={() => clearBelow(row, column)}
          />
          <span className={WARNING_GUTTER}>
            {warning ? (
              <HelpTip
                label={`Warning: ${warning}`}
                icon={<TriangleAlertIcon className="size-3.5" />}
                className="size-11 text-warning hover:text-warning md:size-4"
              >
                <span className="block whitespace-pre-line">{warning}</span>
              </HelpTip>
            ) : null}
          </span>
        </span>
      </td>
    );
  };

  const groupWarns = (group: ContextGroup) =>
    group.models.some((row) =>
      columns.some(
        (column) =>
          !unavailableCell(row, column) &&
          !!cellWarning(
            preferences,
            row,
            column,
            resolveAt(row, column),
            reportedAt(row, column),
            labelOfEntry(row.entry),
          ),
      ),
    );

  const removeButton = (row: ContextRow) => (
    <button
      type="button"
      aria-label={`Remove ${row.kind === "agent" ? row.label : `${labelOfEntry(row.entry)} · ${row.label}`}`}
      title="Remove from this list"
      className={ROW_ACTION}
      onClick={() => removeRow(row)}
    >
      <XIcon aria-hidden className="size-3.5" />
    </button>
  );

  const listedKeys = new Set(entries.map(usageContextEntryKey));
  const listedGroups = new Set(groups.map((group) => group.id));
  const recorded = usageContextRecordedEntries(preferences);
  const uniqueOf = (items: readonly UsageContextListEntry[]) => [
    ...new Map(items.map((item) => [usageContextEntryKey(item), item])).values(),
  ];
  const agentOptions: PickerOption[] = uniqueOf([
    ...inUse,
    ...recorded.map((item) => ({ ...item, model: "" })),
  ])
    .filter((item) => !listedGroups.has(groupIdOf(item)))
    .map((agent) => ({
      key: usageContextEntryKey(agent),
      value: `agent ${labelOfEntry(agent)} ${agent.harness}`,
      entry: agent,
      content: (
        <AgentNameTag
          agentId={badgeIdOfEntry(agent)}
          name={labelOfEntry(agent)}
          harness={agent.harness}
        />
      ),
    }));
  const modelOptions = (group: ContextGroup): PickerOption[] =>
    uniqueOf(
      recorded.filter(
        (item) =>
          item.model && groupIdOf(item) === group.id && !listedKeys.has(usageContextEntryKey(item)),
      ),
    ).map((item) => ({
      key: usageContextEntryKey(item),
      value: `model ${item.model}`,
      entry: item,
      content: <span className="min-w-0 wrap-anywhere">{item.model}</span>,
    }));

  const agentHeader = (group: ContextGroup, open: boolean) => {
    const label = group.agent.label;
    const models = group.models.length;
    const options = modelOptions(group);
    const badge = agentBadgeFor(badges, badgeIdOfEntry(group.agent.entry));
    return (
      <span className={cn("group/label flex items-center gap-1 pr-1 pl-1", ROW_HEIGHT)}>
        {models > 0 ? (
          <button
            type="button"
            aria-expanded={open}
            aria-label={`${open ? "Collapse" : "Expand"} ${label}`}
            className="flex h-11 w-7 shrink-0 items-center justify-center rounded-sm text-muted-foreground outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring md:h-8 md:w-6"
            onClick={() => toggleGroup(group.id)}
          >
            <ChevronRightIcon
              aria-hidden
              className={cn("size-4 transition-transform", open && "rotate-90")}
            />
          </button>
        ) : (
          <span aria-hidden className="w-7 shrink-0 md:w-6" />
        )}
        <span className="flex min-w-0 flex-1 flex-col items-start gap-0.5 py-1">
          <EntryPicker
            trigger={
              <button
                type="button"
                aria-label={`Add a model to ${label}`}
                title={`Add a model to ${label}`}
                data-testid={`context-add-model-${group.agent.testId}`}
                className="group/name flex max-w-full min-w-0 items-center gap-1.5 rounded-sm text-left text-ui font-medium text-foreground underline-offset-2 outline-none hover:underline focus-visible:ring-2 focus-visible:ring-ring aria-expanded:underline"
              >
                {badge ? <AgentBadgeMark badge={badge} data-testid="agent-name-badge" /> : null}
                <span className="min-w-0 wrap-anywhere">{label}</span>
                <PlusIcon
                  aria-hidden
                  className="hidden size-3 shrink-0 text-muted-foreground opacity-0 transition-opacity group-hover/name:opacity-100 group-focus-visible/name:opacity-100 group-aria-expanded/name:opacity-100 md:block"
                />
              </button>
            }
            label={`Add a model to ${label}`}
            placeholder="Type a model"
            heading="Used before"
            options={options}
            typedEntry={(typed) => {
              const entry = { ...group.agent.entry, model: typed };
              if (listedKeys.has(usageContextEntryKey(entry))) return null;
              return options.some((option) => option.entry.model === typed) ? null : entry;
            }}
            emptyText={(typed) => (typed ? `${typed} is already listed.` : "Type a model name.")}
            testId="context-model-picker"
            opensEditor
            onAdd={addModel}
          />
          <span className="flex flex-wrap items-center gap-1">
            <AgentIntegrationMark harness={group.agent.entry.harness} />
            {models > 0 ? (
              <span
                role="img"
                aria-label={`${models} ${models === 1 ? "model" : "models"}`}
                title={`${models} ${models === 1 ? "model" : "models"}`}
                className={cn(AGENT_MARK_CLASS, "tabular-nums")}
                data-testid="context-model-count"
              >
                {models}
              </span>
            ) : null}
            {!open && groupWarns(group) ? (
              <TriangleAlertIcon
                role="img"
                aria-label="A model here differs from what the agent reports"
                className="size-3.5 shrink-0 text-warning"
              />
            ) : null}
          </span>
        </span>
        {removeButton(group.agent)}
      </span>
    );
  };

  return (
    <div className="grid gap-5">
      <div className="flex items-start justify-between gap-6">
        <div className="flex min-w-0 flex-col">
          <span className="text-ui font-medium">Usage limits</span>
          <span
            className="mt-1 text-sm text-muted-foreground"
            data-testid="provider-usage-source-status"
            title={providerStatusDetail}
          >
            {providerStatus}
          </span>
        </div>
        <Switch
          checked={preferences.showProviderUsageLimits}
          onCheckedChange={(showProviderUsageLimits) =>
            writeUsageContextPreferences({ ...preferences, showProviderUsageLimits })
          }
          aria-label="Show usage limits"
        />
      </div>

      {missingSource ? (
        <p className="text-sm text-muted-foreground" data-testid="context-missing-source">
          This session's agent is not listed here.
        </p>
      ) : null}

      <div className="flex min-w-0 border-t border-border pt-4" data-testid="context-limits">
        {/* As wide as the table: the controls and key below wrap to it instead of widening it. */}
        <div className="flex w-fit max-w-full min-w-0 flex-col gap-2">
          <div className="max-w-full overflow-x-auto rounded-lg border border-border">
            <table
              aria-label="Context limits"
              className="w-full min-w-max border-separate border-spacing-0 text-sm"
            >
              <thead>
                <tr>
                  <th className={cn("z-20", TABLE_CELL, FIRST_COLUMN, "px-3 py-2")}>
                    <span className="flex items-center gap-1.5 text-sm font-normal text-muted-foreground">
                      Agent or model
                      <HelpTip label="Which value wins" className="-my-3 size-11 md:my-0 md:size-4">
                        {ORDER_HELP}
                      </HelpTip>
                    </span>
                  </th>
                  {columns.map((column) => (
                    <th
                      key={column.id}
                      scope="col"
                      data-testid={`context-col-${column.id}`}
                      className={cn(TABLE_CELL, BODY_COLUMN, "py-2 font-normal")}
                    >
                      <span className="flex">
                        <span className={cn("min-w-0 flex-1", CELL_PADDING)}>
                          <span className="block text-ui font-medium text-foreground wrap-anywhere">
                            {column.label}
                          </span>
                          <span className={cn(SLOT_GRID, "mt-0.5 text-xs text-muted-foreground")}>
                            <span>Context</span>
                            <span>Buffer</span>
                          </span>
                        </span>
                        <span aria-hidden className={WARNING_GUTTER} />
                      </span>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                <tr data-testid={`context-row-${ALL_ROW.testId}`}>
                  <th scope="row" className={cn(TABLE_CELL, FIRST_COLUMN)}>
                    <span className={cn("flex items-center gap-2 px-3 py-1", ROW_HEIGHT)}>
                      <span className="text-ui font-medium text-foreground">{ALL_ROW.label}</span>
                      <span className="text-xs text-muted-foreground">Default</span>
                    </span>
                  </th>
                  {columns.map((column) => renderCell(ALL_ROW, column))}
                </tr>
              </tbody>
              {groups.map((group) => {
                const open = openGroups.includes(group.id);
                return (
                  <tbody key={group.id} data-testid={`context-group-${group.agent.testId}`}>
                    <tr data-testid={`context-row-${group.agent.testId}`}>
                      <th scope="row" className={cn(TABLE_CELL, FIRST_COLUMN)}>
                        {agentHeader(group, open)}
                      </th>
                      {columns.map((column) => renderCell(group.agent, column))}
                    </tr>
                    {open
                      ? group.models.map((row) => (
                          <tr key={row.id} data-testid={`context-row-${row.testId}`}>
                            <th scope="row" className={cn(TABLE_CELL, FIRST_COLUMN)}>
                              <span
                                className={cn(
                                  "group/label flex items-center gap-1 pr-1 pl-9 md:pl-8",
                                  ROW_HEIGHT,
                                )}
                              >
                                <span className="min-w-0 flex-1 py-1 text-sm wrap-anywhere text-foreground">
                                  {row.label}
                                </span>
                                {removeButton(row)}
                              </span>
                            </th>
                            {columns.map((column) => renderCell(row, column))}
                          </tr>
                        ))
                      : null}
                  </tbody>
                );
              })}
            </table>
          </div>
          <div className="flex min-w-0 flex-wrap items-center gap-x-4 gap-y-2 [contain:inline-size]">
            <EntryPicker
              trigger={
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="min-h-11 md:min-h-7"
                  data-testid="context-add-entry"
                >
                  <PlusIcon aria-hidden className="size-3.5" />
                  Add agent
                </Button>
              }
              label="Add agent"
              placeholder="Search agents"
              heading="Agents in use"
              options={agentOptions}
              emptyText={() => "Every agent in use is listed."}
              testId="context-add-picker"
              onAdd={addEntry}
            />
            {undo ? (
              <span
                className="flex min-w-0 flex-wrap items-center gap-2 text-sm text-muted-foreground"
                role="status"
                data-testid="context-undo-status"
              >
                {undo.message}
                <LayeredEditorAction onClick={restoreUndo}>Undo</LayeredEditorAction>
              </span>
            ) : null}
            <label className="ml-auto flex min-h-11 items-center gap-2 text-sm text-muted-foreground md:min-h-7">
              Keep several open
              <Switch
                size="sm"
                aria-label="Keep several open"
                checked={severalOpen}
                disabled={!preferences.contextList}
                onCheckedChange={setSeveralOpen}
              />
            </label>
          </div>
          <p className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-muted-foreground [contain:inline-size]">
            <span className="inline-flex items-center gap-1.5">
              <PlusIcon aria-hidden className="size-3.5" />
              Click an agent to add a model
            </span>
            <span className="inline-flex items-center gap-1.5">
              <LayeredMark />
              Saved here
            </span>
            <span>
              <span className="italic">Auto</span>: the agent's own value
            </span>
            <span className="inline-flex items-center gap-1.5">
              <TriangleAlertIcon aria-hidden className="size-3.5 text-warning" />
              Differs from what the agent reports
            </span>
          </p>
        </div>
      </div>
    </div>
  );
}
