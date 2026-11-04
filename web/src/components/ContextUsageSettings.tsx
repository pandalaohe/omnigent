import { ChevronRightIcon } from "lucide-react";
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

import { AgentNameTag } from "@/components/AgentNameTag";
import { HelpTip } from "@/components/HelpTip";
import { LayeredEditor, LayeredEditorAction } from "@/components/LayeredEditor";
import {
  LayeredChip,
  LayeredMark,
  LayeredOwn,
  LayeredTable,
  type LayeredCellLevel,
} from "@/components/LayeredTable";
import { SavedContextSources } from "@/components/SavedContextSources";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Segmented } from "@/components/ui/segmented";
import { Switch } from "@/components/ui/switch";
import {
  harnessSection,
  registrySourceLabels,
  useAgentRegistry,
  type RegistryItem,
} from "@/hooks/useAgentRegistry";
import { useCodexRateLimits, useHosts, type Host } from "@/hooks/useHosts";
import { useSession } from "@/hooks/useSession";
import { useStableProviderUsageLimits } from "@/hooks/useStableProviderUsageLimits";
import { useUsageContextPreferences } from "@/hooks/useUsageContextPreferences";
import { formatTokenCountShort } from "@/lib/formatCost";
import { harnessUnavailableReasonOnHost } from "@/lib/harnessSetup";
import {
  levelsBelow,
  missingCellText,
  type LayeredLevel,
  type LayeredStore,
} from "@/lib/layeredValues";
import {
  formatProviderUsageLimits,
  providerUsageLimitsFromCodex,
  type ProviderUsageLimitsSnapshot,
} from "@/lib/providerUsageLimits";
import { useLocation, useNavigate, useSearchParams } from "@/lib/routing";
import {
  fixedBufferFitsCapacity,
  inheritedUsageContext,
  resolveUsageContextLevel,
  setUsageContextLevel,
  usageContextHasValue,
  usageContextLevelKey,
  usageContextLevelLabel,
  usageContextRecordFromKey,
  usageContextSourceFromKey,
  usageContextSourceKey,
  writeUsageContextPreferences,
  type ResolvedUsageContext,
  type UsageContextLevel,
  type UsageContextOverride,
  type UsageContextPreferences,
  type UsageContextSource,
} from "@/lib/usageContextPreferences";
import { useChatStore } from "@/store/chatStore";

/** One record holds a level's whole value, so the layered store carries one field. */
type StoredLevels = LayeredStore<"value">;

interface Draft {
  context: string;
  buffer: string;
  percent: string;
  mode: "tokens" | "percent";
}

interface UndoState {
  overrides: Record<string, UsageContextOverride>;
  keys: string[];
}

interface AgentIdentity {
  agentId: string | null;
  name: string;
  harness: string;
}

interface AgentRow extends AgentIdentity {
  id: string;
}

interface HostColumn {
  id: string;
  hostId: string;
  label: string;
  host: Host | null;
}

const NO_HOST_COLUMN = "no-host";
const CORNER_SOURCE: UsageContextSource = { hostId: "", agentName: "", harness: "", model: "" };
const CORNER_KEY = usageContextLevelKey(CORNER_SOURCE, "all");
const NOTHING_REPORTED = { contextWindow: null, autoCompactTokenLimit: null };

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
  return Number(compact.slice(0, -1)) * divisor === value ? compact : String(value);
}

function stateSummary(resolved: ResolvedUsageContext, mark: boolean, models = 0): string {
  const context = resolved.contextWindow.value;
  const buffer = resolved.buffer.value;
  return [
    `context ${context === null ? "Auto" : `${context} tokens`}`,
    `compact buffer ${buffer === null ? "Auto" : resolved.buffer.mode === "percent" ? `${buffer}%` : `${buffer} tokens`}`,
    mark ? "set here" : "",
    models > 0 ? `${models} saved model ${models === 1 ? "value" : "values"}` : "",
  ]
    .filter(Boolean)
    .join(", ");
}

function agentRank(agent: AgentIdentity): number {
  if (agent.harness === "pi" || agent.harness === "pi-rpc") return 2;
  const section = harnessSection(null, agent.harness);
  return section === "native" ? 0 : section === "sdk" ? 1 : section === "acp" ? 2 : 3;
}

/** Every agent that can hold a Context value: a listed agent, a saved one, or this session's. */
function agentIdentities(
  items: readonly RegistryItem[],
  preferences: UsageContextPreferences,
  live: AgentIdentity | null,
): AgentIdentity[] {
  const found = new Map<string, AgentIdentity>();
  const itemFor = (name: string, harness: string) =>
    items.find((item) => item.name === name && item.harness === harness);
  const add = (identity: AgentIdentity) => {
    if (!identity.name) return;
    const key = JSON.stringify([identity.name, identity.harness]);
    if (!found.has(key)) found.set(key, identity);
  };
  for (const item of items) {
    if (!item.name || !item.harness) continue;
    add({ agentId: item.key, name: item.name, harness: item.harness });
  }
  for (const key of Object.keys(preferences.overrides)) {
    const record = usageContextRecordFromKey(key);
    if (!record || !record.source.agentName) continue;
    add({
      agentId: itemFor(record.source.agentName, record.source.harness)?.key ?? null,
      name: record.source.agentName,
      harness: record.source.harness,
    });
  }
  if (live) {
    add({
      name: live.name,
      harness: live.harness,
      agentId: itemFor(live.name, live.harness)?.key ?? null,
    });
  }
  return [...found.values()].sort(
    (left, right) => agentRank(left) - agentRank(right) || left.name.localeCompare(right.name),
  );
}

function hostColumns(
  hosts: readonly Host[],
  preferences: UsageContextPreferences,
  liveHostId: string | null,
): HostColumn[] {
  const byHostId = new Map<string, HostColumn>();
  for (const host of hosts) {
    byHostId.set(host.host_id, {
      id: host.host_id,
      hostId: host.host_id,
      label: host.name || host.host_id,
      host,
    });
  }
  const addHost = (hostId: string) => {
    if (!byHostId.has(hostId))
      byHostId.set(hostId, { id: hostId, hostId, label: hostId, host: null });
  };
  let noHost = liveHostId === "";
  for (const key of Object.keys(preferences.overrides)) {
    const record = usageContextRecordFromKey(key);
    if (!record) continue;
    if (record.source.hostId) addHost(record.source.hostId);
    else noHost = true;
  }
  if (liveHostId) addHost(liveHostId);
  const columns = [...byHostId.values()].sort((left, right) =>
    left.label.localeCompare(right.label),
  );
  if (noHost) columns.push({ id: NO_HOST_COLUMN, hostId: "", label: "No host", host: null });
  return columns;
}

function unavailableCell(agent: AgentIdentity, column: HostColumn): string | undefined {
  if (!column.host) return undefined;
  const reason = harnessUnavailableReasonOnHost(agent.harness, column.host);
  if (reason === "platform-unsupported")
    return missingCellText(agent.harness, column.host.platform);
  if (reason === "binary-missing" || reason === "unconfigured")
    return missingCellText(agent.harness, null);
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

/** Fixed total/buffer slots; a blank mark slot keeps inherited values aligned. */
function LevelState({
  resolved,
  mark,
  models = 0,
}: {
  resolved: ResolvedUsageContext;
  mark: boolean;
  models?: number;
}) {
  const context = resolved.contextWindow.value;
  const buffer = resolved.buffer.value;
  const values = (
    <span className="inline-flex items-center gap-1">
      <span data-context-slot="total">
        <LayeredChip tone="first" title={`Context total: ${context ?? "Auto"}`}>
          {context === null ? "–" : contextTokenText(context)}
        </LayeredChip>
      </span>
      <span data-context-slot="buffer">
        <LayeredChip
          tone="second"
          title={`Compact buffer: ${buffer ?? "Auto"}${resolved.buffer.mode === "percent" && buffer !== null ? "%" : ""}`}
        >
          {buffer === null
            ? "–"
            : resolved.buffer.mode === "percent"
              ? `${buffer}%`
              : contextTokenText(buffer)}
        </LayeredChip>
      </span>
      {models > 0 && (
        <span data-context-slot="models">
          <LayeredChip title={`${models} saved model ${models === 1 ? "value" : "values"}`}>
            {models}M
          </LayeredChip>
        </span>
      )}
    </span>
  );
  if (mark) return <LayeredOwn>{values}</LayeredOwn>;
  return (
    <span className="inline-flex items-center gap-1.5">
      <span aria-hidden="true" className="size-1.5 shrink-0" />
      {values}
    </span>
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
  contextInputRef,
  source,
  level,
  label,
  levelText,
  draft,
  onChange,
  onSubmit,
  error,
}: {
  preferences: UsageContextPreferences;
  contextInputRef?: Ref<HTMLInputElement>;
  source: UsageContextSource;
  level: UsageContextLevel;
  label: string;
  levelText: (level: UsageContextLevel) => string;
  draft: Draft;
  onChange: (next: Draft) => void;
  onSubmit: () => void;
  error: string;
}) {
  const stored = draftOf(preferences.overrides[usageContextLevelKey(source, level)]);
  const inherited = inheritedUsageContext(preferences, source, level);
  const inheritedBuffer = inherited.buffer.value;
  const fromText = (winner: UsageContextLevel | "reported") =>
    winner === "reported" ? "The agent reports its own" : `From ${levelText(winner)}`;
  const valueNote = (typed: string, saved: string, winner: UsageContextLevel | "reported") => {
    if (typed.trim() === "") return fromText(winner);
    if (typed !== saved) return "Not saved yet";
    return (
      <span className="inline-flex items-center gap-1.5">
        <LayeredMark />
        Saved here
      </span>
    );
  };
  return (
    <DraftFields
      draft={draft}
      contextInputRef={contextInputRef}
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
        field === "context"
          ? valueNote(draft.context, stored.context, inherited.contextWindow.level)
          : draft.mode === "percent"
            ? valueNote(draft.percent, stored.percent, inherited.buffer.level)
            : valueNote(draft.buffer, stored.buffer, inherited.buffer.level)
      }
    />
  );
}

/** Validate a draft and turn it into the record its level stores. */
function recordForLevel(
  preferences: UsageContextPreferences,
  source: UsageContextSource,
  level: UsageContextLevel,
  draft: Draft,
): { record: UsageContextOverride | null } | { error: string } {
  const inheritedContext = inheritedUsageContext(preferences, source, level).contextWindow.value;
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

/** The editor of a corner, host column or cell: one level, written whole on Save. */
function ValueEditor({
  trigger,
  title,
  help,
  open,
  onOpenChange,
  preferences,
  source,
  level,
  label,
  levelText,
  clearBelowCount,
  onClearBelow,
  undo,
  onUndo,
}: {
  trigger: ReactElement;
  title: string;
  help?: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  preferences: UsageContextPreferences;
  source: UsageContextSource;
  level: UsageContextLevel;
  label: string;
  levelText: (level: UsageContextLevel) => string;
  clearBelowCount: number;
  onClearBelow: () => void;
  undo: UndoState | null;
  onUndo: () => void;
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
    const outcome = recordForLevel(preferences, source, level, draft);
    if ("error" in outcome) {
      setError(outcome.error);
      return;
    }
    setUsageContextLevel(preferences, key, outcome.record);
    onOpenChange(false);
  };
  return (
    <LayeredEditor
      trigger={trigger}
      title={title}
      help={help}
      open={open}
      onOpenChange={onOpenChange}
      onClear={
        usageContextHasValue(own) ? () => setUsageContextLevel(preferences, key, null) : undefined
      }
      clearBelowCount={clearBelowCount}
      onClearBelow={clearBelowCount > 0 ? onClearBelow : undefined}
      actions={undo ? <LayeredEditorAction onClick={onUndo}>Undo</LayeredEditorAction> : null}
      footer={<EditorButtons onCancel={() => onOpenChange(false)} onSave={save} />}
    >
      <LevelFields
        preferences={preferences}
        source={source}
        level={level}
        label={label}
        levelText={levelText}
        draft={draft}
        onChange={setDraft}
        onSubmit={save}
        error={error}
      />
    </LayeredEditor>
  );
}

export function ContextUsageSettings() {
  const preferences = useUsageContextPreferences();
  const registry = useAgentRegistry();
  const { data: hosts = [] } = useHosts();
  const conversationId = useChatStore((state) => state.conversationId);
  const liveAgentName = useChatStore((state) => state.boundAgentName);
  const liveHarness = useChatStore((state) => state.sessionHarness);
  const liveModel = useChatStore((state) => state.llmModel);
  const reportedContextWindow = useChatStore((state) => state.contextWindow);
  const reportedCompactLimit = useChatStore((state) => state.autoCompactTokenLimit);
  const sessionProviderUsageLimits = useChatStore((state) => state.providerUsageLimits);
  const { session } = useSession(conversationId);
  const liveHostId: string | null = conversationId ? (session?.hostId ?? "") : null;
  const [openEditor, setOpenEditor] = useState<LayeredLevel | null>(null);
  const [focusModel, setFocusModel] = useState<string | null>(null);
  const [missingSource, setMissingSource] = useState(false);
  const [undo, setUndo] = useState<UndoState | null>(null);
  const searchParams = useSearchParams()[0];
  const navigate = useNavigate();
  const location = useLocation();
  const consumedRef = useRef<string | null>(null);
  const focusEditorRef = useRef(false);

  const table = useMemo(() => {
    const live: AgentIdentity | null =
      liveAgentName && liveHarness
        ? { agentId: null, name: liveAgentName, harness: liveHarness }
        : null;
    const identities = agentIdentities(registry.items, preferences, live);
    const usedIds = new Set<string>();
    const rows: AgentRow[] = identities.map((agent) => {
      const base = `${slug(agent.name)}-${slug(agent.harness)}`;
      let id = base;
      let suffix = 2;
      while (usedIds.has(id)) id = `${base}-${suffix++}`;
      usedIds.add(id);
      return { ...agent, id };
    });
    const columns = hostColumns(hosts, preferences, liveHostId);
    const rowKeys = new Map<string, string>();
    const colKeys = new Map<string, string>();
    const cellKeys = new Map<string, string>();
    for (const row of rows) {
      rowKeys.set(
        row.id,
        usageContextLevelKey(
          { hostId: "", agentName: row.name, harness: row.harness, model: "" },
          "column",
        ),
      );
    }
    for (const column of columns) {
      colKeys.set(
        column.id,
        usageContextLevelKey(
          { hostId: column.hostId, agentName: "", harness: "", model: "" },
          "host",
        ),
      );
    }
    const cells: StoredLevels["cells"] = {};
    for (const column of columns) {
      cells[column.id] = {};
      for (const row of rows) {
        const key = usageContextLevelKey(
          { hostId: column.hostId, agentName: row.name, harness: row.harness, model: "" },
          "cell",
        );
        cellKeys.set(`${row.id}\u0000${column.id}`, key);
        const override = preferences.overrides[key];
        if (usageContextHasValue(override)) cells[column.id][row.id] = { value: override };
      }
    }
    const store: StoredLevels = { corner: {}, rows: {}, cols: {}, cells };
    const corner = preferences.overrides[CORNER_KEY];
    if (usageContextHasValue(corner)) store.corner = { value: corner };
    for (const row of rows) {
      const override = preferences.overrides[rowKeys.get(row.id)!];
      if (usageContextHasValue(override)) store.rows[row.id] = { value: override };
    }
    for (const column of columns) {
      const override = preferences.overrides[colKeys.get(column.id)!];
      if (usageContextHasValue(override)) store.cols[column.id] = { value: override };
    }
    return {
      rows,
      columns,
      rowById: new Map(rows.map((row) => [row.id, row])),
      columnById: new Map(columns.map((column) => [column.id, column])),
      rowKeys,
      colKeys,
      cellKeys,
      store,
    };
  }, [registry.items, preferences, hosts, liveAgentName, liveHarness, liveHostId]);

  const { rows, columns, rowById, columnById, rowKeys, colKeys, cellKeys, store } = table;
  const hostLabelOf = (hostId: string) =>
    hostId ? (columns.find((column) => column.hostId === hostId)?.label ?? hostId) : "No host";
  const agentLabelOf = (source: UsageContextSource) =>
    registrySourceLabels(registry.items, source.agentName, source.harness).agentLabel;
  const levelLabel = (level: UsageContextLevel, source: UsageContextSource) =>
    usageContextLevelLabel(level, {
      hostLabel: hostLabelOf(source.hostId),
      agentLabel: agentLabelOf(source),
    });
  const sourceOf = (row: AgentRow, column: HostColumn): UsageContextSource => ({
    hostId: column.hostId,
    agentName: row.name,
    harness: row.harness,
    model: "",
  });
  const reportedFor = (source: UsageContextSource) =>
    !!source.agentName &&
    source.hostId === (liveHostId ?? "") &&
    source.agentName === (liveAgentName ?? "") &&
    source.harness === (liveHarness ?? "")
      ? { contextWindow: reportedContextWindow, autoCompactTokenLimit: reportedCompactLimit }
      : NOTHING_REPORTED;
  const resolveAt = (at: LayeredLevel) => {
    if (at.level === "corner") return resolveUsageContextLevel(preferences, CORNER_SOURCE, "all");
    if (at.level === "row") {
      const row = rowById.get(at.row)!;
      const source = { hostId: "", agentName: row.name, harness: row.harness, model: "" };
      return resolveUsageContextLevel(preferences, source, "column");
    }
    if (at.level === "col") {
      const column = columnById.get(at.col)!;
      const source = { hostId: column.hostId, agentName: "", harness: "", model: "" };
      return resolveUsageContextLevel(preferences, source, "host");
    }
    const source = sourceOf(rowById.get(at.row)!, columnById.get(at.col)!);
    return resolveUsageContextLevel(preferences, source, "cell", reportedFor(source));
  };
  const levelAt = (at: LayeredLevel): UsageContextLevel =>
    at.level === "corner"
      ? "all"
      : at.level === "row"
        ? "column"
        : at.level === "col"
          ? "host"
          : "cell";
  const sourceAt = (at: LayeredLevel): UsageContextSource => {
    if (at.level === "corner") return CORNER_SOURCE;
    if (at.level === "row") {
      const row = rowById.get(at.row)!;
      return { hostId: "", agentName: row.name, harness: row.harness, model: "" };
    }
    if (at.level === "col") {
      const column = columnById.get(at.col)!;
      return { hostId: column.hostId, agentName: "", harness: "", model: "" };
    }
    return sourceOf(rowById.get(at.row)!, columnById.get(at.col)!);
  };
  const titleAt = (at: LayeredLevel): string => {
    if (at.level === "corner") return "All agents, all hosts";
    if (at.level === "row") return `${agentLabelOf(sourceAt(at))}, all hosts`;
    if (at.level === "col") return `${hostLabelOf(sourceAt(at).hostId)}, all agents`;
    return `${agentLabelOf(sourceAt(at))} on ${hostLabelOf(sourceAt(at).hostId)}`;
  };
  const keyAt = (at: LayeredLevel): string | null => {
    if (at.level === "corner") return CORNER_KEY;
    if (at.level === "row") return rowKeys.get(at.row) ?? null;
    if (at.level === "col") return colKeys.get(at.col) ?? null;
    return cellKeys.get(`${at.row}\u0000${at.col}`) ?? null;
  };

  const markAt = (at: LayeredLevel) => {
    const key = keyAt(at);
    return key ? usageContextHasValue(preferences.overrides[key]) : false;
  };
  const keysBelow = (at: LayeredLevel): string[] => {
    const keys = new Set(
      levelsBelow(store, at).flatMap((level) => {
        const key = keyAt(level);
        return key ? [key] : [];
      }),
    );
    for (const [key, override] of Object.entries(preferences.overrides)) {
      const record = usageContextRecordFromKey(key);
      if (
        !record ||
        (record.level !== "model" && record.level !== "agent-model") ||
        !usageContextHasValue(override)
      )
        continue;
      const sameAgent =
        record.source.agentName === sourceAt(at).agentName &&
        record.source.harness === sourceAt(at).harness;
      const below =
        at.level === "corner" ||
        (at.level === "row" && sameAgent) ||
        (at.level === "col" &&
          record.level === "model" &&
          record.source.hostId === sourceAt(at).hostId) ||
        (at.level === "cell" &&
          record.level === "model" &&
          sameAgent &&
          record.source.hostId === sourceAt(at).hostId);
      if (below) keys.add(key);
    }
    return [...keys];
  };

  const writeOverrides = (overrides: Record<string, UsageContextOverride>) => {
    const keys = [
      ...new Set([...Object.keys(preferences.overrides), ...Object.keys(overrides)]),
    ].filter((key) => preferences.overrides[key] !== overrides[key]);
    setUndo({ overrides: preferences.overrides, keys });
    writeUsageContextPreferences({ ...preferences, overrides });
  };
  const restoreUndo = () => {
    if (!undo) return;
    const overrides = { ...preferences.overrides };
    for (const key of undo.keys) {
      if (Object.hasOwn(undo.overrides, key)) overrides[key] = undo.overrides[key]!;
      else Reflect.deleteProperty(overrides, key);
    }
    writeUsageContextPreferences({ ...preferences, overrides });
    setUndo(null);
  };
  const clearBelow = (at: LayeredLevel) => {
    const overrides = { ...preferences.overrides };
    for (const key of keysBelow(at)) Reflect.deleteProperty(overrides, key);
    writeOverrides(overrides);
  };

  const openAt = (at: LayeredLevel, model: string | null) => {
    focusEditorRef.current = true;
    setFocusModel(model);
    setOpenEditor(at);
  };
  const sameLevel = (left: LayeredLevel | null, right: LayeredLevel) =>
    !!left && JSON.stringify(left) === JSON.stringify(right);
  const editorProps = (at: LayeredLevel) => ({
    open: sameLevel(openEditor, at),
    onOpenChange: (next: boolean) => {
      focusEditorRef.current = false;
      if (next) setOpenEditor(at);
      else if (sameLevel(openEditor, at)) {
        setOpenEditor(null);
        setFocusModel(null);
      }
    },
  });

  const sessionKey = usageContextSourceKey({
    hostId: liveHostId ?? "",
    agentName: liveAgentName ?? "",
    harness: liveHarness ?? "",
    model: liveModel ?? "",
  });

  // The ring's entry: open this session's agent row on its own model, then drop
  // `ctx` from the URL so a reload does not reopen it.
  useEffect(() => {
    const param = searchParams.get("ctx");
    if (!param || consumedRef.current === param) return;
    consumedRef.current = param;
    const source = usageContextSourceFromKey(param);
    const row = source
      ? rows.find((item) => item.name === source.agentName && item.harness === source.harness)
      : null;
    if (row && source) {
      openAt({ level: "row", row: row.id }, source.model);
      setMissingSource(false);
    } else {
      setMissingSource(true);
    }
    const next = new URLSearchParams(searchParams);
    next.delete("ctx");
    navigate({ search: next.toString(), hash: location.hash }, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams, rows]);

  // Bring the row, header or cell the open editor belongs to on screen: a float
  // anchored past the viewport edge cannot be reached.
  useEffect(() => {
    if (!openEditor) return;
    const focusFirstField = focusEditorRef.current;
    focusEditorRef.current = false;
    const selector =
      openEditor.level === "corner"
        ? '[data-testid="layered-corner"]'
        : openEditor.level === "row"
          ? `[data-testid="layered-row-${openEditor.row}"]`
          : openEditor.level === "col"
            ? `[data-testid="layered-col-${openEditor.col}"]`
            : `[data-testid="layered-cell-${openEditor.row}-${openEditor.col}"]`;
    const timer = window.setTimeout(() => {
      document.querySelector(selector)?.scrollIntoView({ block: "center", inline: "center" });
      if (focusFirstField)
        document.querySelector<HTMLInputElement>('[data-pop][data-state="open"] input')?.focus();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [openEditor]);

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
  const liveHostColumn =
    liveHostId && liveHostId !== "" ? (columnById.get(liveHostId) ?? null) : null;

  // Stable identity: the table calls this as a render prop, not as a component type.
  const renderSelectionActions = useMemo(
    () => (selected: readonly LayeredCellLevel[], clear: () => void) => (
      <SelectionActions
        preferences={preferences}
        selected={selected}
        cellKeys={cellKeys}
        undo={undo}
        onWrite={writeOverrides}
        onUndo={restoreUndo}
        clear={clear}
      />
    ),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [preferences, cellKeys, undo],
  );

  const renderValueEditor = (at: LayeredLevel, trigger: ReactElement) => {
    if (!keyAt(at)) return trigger;
    const source = sourceAt(at);
    const level = levelAt(at);
    const belowCount = keysBelow(at).length;
    const props = editorProps(at);
    return (
      <ValueEditor
        trigger={trigger}
        title={titleAt(at)}
        help={
          at.level === "cell"
            ? `This cell, then ${levelLabel("column", source)}, then ${levelLabel("host", source)}, then ${levelLabel("all", source)}.`
            : undefined
        }
        open={props.open}
        onOpenChange={props.onOpenChange}
        preferences={preferences}
        source={source}
        level={level}
        label={levelLabel(level, source)}
        levelText={(winner) => levelLabel(winner, source)}
        clearBelowCount={belowCount}
        onClearBelow={() => clearBelow(at)}
        undo={undo}
        onUndo={restoreUndo}
      />
    );
  };

  const savedSourcesTools = (
    <SavedContextSources
      preferences={preferences}
      hosts={hosts}
      registryItems={registry.items}
      currentSourceKey={sessionKey}
      initialSource={
        liveAgentName && liveHarness
          ? {
              hostId: liveHostId ?? "",
              agentName: liveAgentName,
              harness: liveHarness,
              model: "",
            }
          : rows[0]
            ? { hostId: "", agentName: rows[0].name, harness: rows[0].harness, model: "" }
            : null
      }
    />
  );

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

      <LayeredTable
        label="Saved context values"
        testId="saved-context-sources"
        className="border-t border-border pt-4"
        cornerLabel="All agents, all hosts"
        cornerContent="All"
        cornerSummary={stateSummary(resolveAt({ level: "corner" }), markAt({ level: "corner" }))}
        cornerDetail={
          <LevelState
            resolved={resolveAt({ level: "corner" })}
            mark={markAt({ level: "corner" })}
          />
        }
        note="Rules for every model, new ones included. A session's ring sets one model."
        tools={savedSourcesTools}
        legend={
          <>
            <span className="inline-flex items-center gap-1">
              <LayeredChip tone="first">200k</LayeredChip>
              context
            </span>
            <span className="inline-flex items-center gap-1">
              <LayeredChip tone="second">10%</LayeredChip>
              buffer
            </span>
            <span className="inline-flex items-center gap-1">
              <LayeredChip>3M</LayeredChip>
              saved models
            </span>
            <span>– Auto</span>
          </>
        }
        rows={rows.map((row) => {
          const at: LayeredLevel = { level: "row", row: row.id };
          return {
            id: row.id,
            label: row.name,
            content: (
              <AgentNameTag
                agentId={row.agentId}
                name={registrySourceLabels(registry.items, row.name, row.harness).agentLabel}
                harness={row.harness}
              />
            ),
            detail: <LevelState resolved={resolveAt(at)} mark={markAt(at)} />,
            summary: stateSummary(resolveAt(at), markAt(at)),
          };
        })}
        columns={columns.map((column) => {
          const at: LayeredLevel = { level: "col", col: column.id };
          return {
            id: column.id,
            label: column.label,
            content: column.label,
            detail: <LevelState resolved={resolveAt(at)} mark={markAt(at)} />,
            summary: stateSummary(resolveAt(at), markAt(at)),
          };
        })}
        renderCell={(rowId, colId) => {
          const row = rowById.get(rowId);
          const column = columnById.get(colId);
          if (!row || !column) return { content: null };
          const unavailable = unavailableCell(row, column);
          if (unavailable) return { content: null, unavailable };
          const at: LayeredLevel = { level: "cell", row: rowId, col: colId };
          const source = sourceOf(row, column);
          const models = Object.keys(preferences.overrides).filter((key) => {
            const record = usageContextRecordFromKey(key);
            return (
              record?.level === "model" &&
              record.source.hostId === column.hostId &&
              record.source.agentName === row.name &&
              record.source.harness === row.harness
            );
          }).length;
          return {
            content: <LevelState resolved={resolveAt(at)} mark={markAt(at)} models={models} />,
            summary: stateSummary(resolveAt(at), markAt(at), models),
            clash: cellClash(source, column, preferences, reportedFor(source), agentLabelOf),
          };
        }}
        renderEditor={(at, trigger) => {
          if (at.level !== "row") return renderValueEditor(at, trigger);
          const row = rowById.get(at.row);
          if (!row) return trigger;
          const props = editorProps(at);
          return (
            <AgentRowEditor
              preferences={preferences}
              registryItems={registry.items}
              row={row}
              trigger={trigger}
              open={props.open}
              onOpenChange={props.onOpenChange}
              focusModel={focusModel}
              hostColumn={liveHostColumn}
              hostLabel={hostLabelOf}
              clearBelowCount={keysBelow(at).length}
              onClearBelow={() => clearBelow(at)}
              undo={undo}
              onUndo={restoreUndo}
            />
          );
        }}
        selectionActions={renderSelectionActions}
      />
    </div>
  );
}

/** A cell whose two headers disagree carries a `?` naming the field and the winner. */
function cellClash(
  source: UsageContextSource,
  column: HostColumn,
  preferences: UsageContextPreferences,
  reported: { contextWindow: number | null; autoCompactTokenLimit: number | null },
  agentLabelOf: (source: UsageContextSource) => string,
): string | undefined {
  const cell = preferences.overrides[usageContextLevelKey(source, "cell")];
  const fromRow = preferences.overrides[usageContextLevelKey(source, "column")];
  const fromHost = preferences.overrides[usageContextLevelKey(source, "host")];
  const agentLabel = agentLabelOf(source);
  const resolved = resolveUsageContextLevel(preferences, source, "cell", reported);
  const lines: string[] = [];
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
  return lines.length ? lines.join("\n") : undefined;
}

/** Bulk edits on a selection of cells, with Undo. */
function SelectionActions({
  preferences,
  selected,
  cellKeys,
  undo,
  onWrite,
  onUndo,
  clear,
}: {
  preferences: UsageContextPreferences;
  selected: readonly LayeredCellLevel[];
  cellKeys: Map<string, string>;
  undo: UndoState | null;
  onWrite: (overrides: Record<string, UsageContextOverride>) => void;
  onUndo: () => void;
  clear: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<Draft>({
    context: "",
    buffer: "",
    percent: "",
    mode: "tokens",
  });
  const [error, setError] = useState("");
  const keys = selected.flatMap(({ row, col }) => {
    const key = cellKeys.get(`${row}\u0000${col}`);
    return key ? [key] : [];
  });
  const apply = () => {
    if (keys.length === 0) return;
    const outcome = recordFromDraft(draft, null);
    if ("error" in outcome) {
      setError(outcome.error);
      return;
    }
    setError("");
    const overrides = { ...preferences.overrides };
    for (const key of keys) {
      const record = usageContextRecordFromKey(key);
      const deduped =
        outcome.record && record?.level === "cell"
          ? dropRepeatedCellFields(preferences, record.source, outcome.record)
          : outcome.record;
      if (deduped) overrides[key] = deduped;
      else Reflect.deleteProperty(overrides, key);
    }
    onWrite(overrides);
    clear();
  };
  return (
    <>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className="min-h-11 md:min-h-7"
        disabled={!undo}
        onClick={onUndo}
      >
        Undo
      </Button>
      <LayeredEditor
        trigger={
          <Button type="button" variant="outline" size="sm" className="min-h-11 md:min-h-7">
            Set values
          </Button>
        }
        title={`${selected.length} selected ${selected.length === 1 ? "cell" : "cells"}`}
        note="Empty fields inherit."
        open={open}
        onOpenChange={setOpen}
        footer={
          <>
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="min-h-11 md:min-h-7"
              onClick={() => setOpen(false)}
            >
              Cancel
            </Button>
            <Button
              type="button"
              size="sm"
              className="min-h-11 md:min-h-7"
              disabled={keys.length === 0}
              onClick={apply}
            >
              Apply to {keys.length} selected
            </Button>
          </>
        }
      >
        <DraftFields
          draft={draft}
          onChange={setDraft}
          onSubmit={apply}
          error={error}
          placeholder={() => "Inherit"}
          labelFor={(field) =>
            field === "unit"
              ? "Buffer unit for the selection"
              : `${field === "context" ? "Context total" : "Compact buffer"} for the selection`
          }
        />
      </LayeredEditor>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className="min-h-11 md:min-h-7"
        disabled={keys.length === 0}
        onClick={() => {
          const overrides = { ...preferences.overrides };
          for (const key of keys) Reflect.deleteProperty(overrides, key);
          onWrite(overrides);
          clear();
        }}
      >
        Use inherited
      </Button>
    </>
  );
}

/** One stored level the agent row's editor can show. */
interface RowEditorTarget {
  key: string;
  level: UsageContextLevel;
  source: UsageContextSource;
  /** Names the fields for assistive tech. */
  label: string;
}

/** The agent row: one level in view at a time, the others a step away. */
function AgentRowEditor({
  preferences,
  registryItems,
  row,
  trigger,
  open,
  onOpenChange,
  focusModel,
  hostColumn,
  hostLabel,
  clearBelowCount,
  onClearBelow,
  undo,
  onUndo,
}: {
  preferences: UsageContextPreferences;
  registryItems: readonly RegistryItem[];
  row: AgentRow;
  trigger: ReactElement;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  focusModel: string | null;
  hostColumn: HostColumn | null;
  hostLabel: (hostId: string) => string;
  clearBelowCount: number;
  onClearBelow: () => void;
  undo: UndoState | null;
  onUndo: () => void;
}) {
  const labels = registrySourceLabels(registryItems, row.name, row.harness);
  const agentSource: UsageContextSource = {
    hostId: "",
    agentName: row.name,
    harness: row.harness,
    model: "",
  };
  const allModels: RowEditorTarget = {
    key: usageContextLevelKey(agentSource, "column"),
    level: "column",
    source: agentSource,
    label: `all models of ${labels.agentLabel}`,
  };
  const modelSource: UsageContextSource = { ...agentSource, model: focusModel ?? "" };
  const oneModel: RowEditorTarget | null = focusModel
    ? {
        key: usageContextLevelKey(modelSource, "agent-model"),
        level: "agent-model",
        source: modelSource,
        label: focusModel,
      }
    : null;
  const [modelTab, setModelTab] = useState<"model" | "all">("model");
  const [stepped, setStepped] = useState<(RowEditorTarget & { title: string }) | null>(null);
  const [typed, setTyped] = useState<Record<string, Draft>>({});
  const [error, setError] = useState("");
  const contextInputRef = useRef<HTMLInputElement>(null);
  const cellEntryRef = useRef<HTMLButtonElement>(null);
  const hostEntryRef = useRef<HTMLButtonElement>(null);
  const returnEntryRef = useRef<"cell" | "host" | null>(null);
  useEffect(() => {
    setModelTab("model");
    setStepped(null);
    setTyped({});
    setError("");
  }, [open]);
  useEffect(() => {
    if (stepped) contextInputRef.current?.focus();
    else if (returnEntryRef.current) {
      const entry = returnEntryRef.current === "host" ? hostEntryRef : cellEntryRef;
      entry.current?.focus();
      returnEntryRef.current = null;
    }
  }, [stepped]);

  const target = stepped ?? (oneModel && modelTab === "model" ? oneModel : allModels);
  const onFocusedModel = !stepped && target === oneModel;
  const draftFor = (key: string) => typed[key] ?? draftOf(preferences.overrides[key]);
  const draft = draftFor(target.key);
  const stepTo = (next: RowEditorTarget & { title: string }) => {
    const changed =
      JSON.stringify(draft) !== JSON.stringify(draftOf(preferences.overrides[target.key]));
    if (changed) setTyped({ ...typed, [next.key]: draft });
    setError("");
    returnEntryRef.current = next.level === "host" ? "host" : "cell";
    setStepped(next);
  };
  const save = () => {
    const outcome = recordForLevel(preferences, target.source, target.level, draft);
    if ("error" in outcome) {
      setError(outcome.error);
      return;
    }
    setUsageContextLevel(preferences, target.key, outcome.record);
    onOpenChange(false);
  };
  const clear = () => {
    const rest = { ...typed };
    Reflect.deleteProperty(rest, target.key);
    setTyped(rest);
    setUsageContextLevel(preferences, target.key, null);
  };

  const savedModels = Object.keys(preferences.overrides).flatMap((key) => {
    const record = usageContextRecordFromKey(key);
    if (!record || record.source.agentName !== row.name || record.source.harness !== row.harness)
      return [];
    if (record.level !== "model" && record.level !== "agent-model") return [];
    const override = preferences.overrides[key];
    if (!override || !usageContextHasValue(override)) return [];
    return [
      {
        key,
        model: record.source.model,
        override,
        scope:
          record.level === "agent-model" || !record.source.hostId
            ? "Every computer"
            : hostLabel(record.source.hostId),
      },
    ];
  });
  const hostText = hostColumn?.label ?? "";

  return (
    <LayeredEditor
      trigger={trigger}
      title={stepped ? stepped.title : `${labels.agentLabel}, all hosts`}
      note={
        stepped
          ? stepped.level === "model"
            ? focusModel
            : undefined
          : oneModel
            ? modelTab === "model"
              ? "This session's model"
              : "Any model with no value of its own"
            : "All models"
      }
      help={
        stepped?.level === "cell" || stepped?.level === "model"
          ? `This cell, then ${labels.agentLabel}, all hosts, then all agents on ${hostText}, then all agents, all hosts.`
          : undefined
      }
      back={
        stepped
          ? {
              label: `${labels.agentLabel}, all hosts`,
              onBack: () => {
                setError("");
                setStepped(null);
              },
            }
          : undefined
      }
      open={open}
      onOpenChange={onOpenChange}
      onClear={usageContextHasValue(preferences.overrides[target.key]) ? clear : undefined}
      clearBelowCount={stepped ? 0 : clearBelowCount}
      onClearBelow={clearBelowCount > 0 ? onClearBelow : undefined}
      actions={undo ? <LayeredEditorAction onClick={onUndo}>Undo</LayeredEditorAction> : null}
      footer={<EditorButtons onCancel={() => onOpenChange(false)} onSave={save} />}
    >
      {oneModel && !stepped ? (
        <Segmented
          quiet
          stretch
          label="Which models"
          value={modelTab}
          options={[
            { value: "model", label: oneModel.label },
            { value: "all", label: "All models" },
          ]}
          onChange={(next) => {
            setError("");
            setModelTab(next);
          }}
        />
      ) : null}
      <LevelFields
        preferences={preferences}
        contextInputRef={contextInputRef}
        source={target.source}
        level={target.level}
        label={target.label}
        levelText={(winner) =>
          usageContextLevelLabel(winner, {
            hostLabel: hostText || "No host",
            agentLabel: labels.agentLabel,
          })
        }
        draft={draft}
        onChange={(next) => setTyped({ ...typed, [target.key]: next })}
        onSubmit={save}
        error={error}
      />
      {!stepped && hostColumn ? (
        <nav aria-label="Other levels" className="grid border-t border-border pt-1.5">
          <StepButton
            buttonRef={cellEntryRef}
            onClick={() => {
              const source: UsageContextSource = {
                ...agentSource,
                hostId: hostColumn.hostId,
                model: onFocusedModel ? (focusModel ?? "") : "",
              };
              const level = onFocusedModel ? "model" : "cell";
              stepTo({
                key: usageContextLevelKey(source, level),
                level,
                source,
                label: `${labels.agentLabel} on ${hostColumn.label}`,
                title: `${labels.agentLabel} on ${hostColumn.label}`,
              });
            }}
          >
            Only on {hostColumn.label}
          </StepButton>
          <StepButton
            buttonRef={hostEntryRef}
            onClick={() => {
              const source: UsageContextSource = {
                hostId: hostColumn.hostId,
                agentName: "",
                harness: "",
                model: "",
              };
              stepTo({
                key: usageContextLevelKey(source, "host"),
                level: "host",
                source,
                label: `all agents on ${hostColumn.label}`,
                title: `All agents on ${hostColumn.label}`,
              });
            }}
          >
            All agents on {hostColumn.label}
          </StepButton>
        </nav>
      ) : null}
      {!stepped && savedModels.length > 0 ? (
        <section className="grid gap-1 border-t border-border pt-3">
          <p className="text-xs font-medium text-muted-foreground">Saved for one model</p>
          {savedModels.map((item) => (
            <div key={item.key} className="flex items-center gap-2 text-sm">
              <span className="min-w-0 flex-1">
                <span className="block break-all">{item.model}</span>
                <span className="block text-xs text-muted-foreground">
                  {item.scope} · {savedValueText(item.override)}
                </span>
              </span>
              <LayeredEditorAction
                aria-label={`Remove the value for ${item.model} on ${item.scope}`}
                onClick={() => setUsageContextLevel(preferences, item.key, null)}
              >
                Remove
              </LayeredEditorAction>
            </div>
          ))}
        </section>
      ) : null}
    </LayeredEditor>
  );
}

function StepButton({
  children,
  onClick,
  buttonRef,
}: {
  children: ReactNode;
  onClick: () => void;
  buttonRef?: Ref<HTMLButtonElement>;
}) {
  return (
    <button
      ref={buttonRef}
      type="button"
      className="-mx-1 flex min-h-11 items-center justify-between gap-2 rounded-md px-1 text-left text-sm text-muted-foreground hover:bg-foreground/5 hover:text-foreground md:min-h-8"
      onClick={onClick}
    >
      <span className="min-w-0 wrap-anywhere">{children}</span>
      <ChevronRightIcon className="size-4 shrink-0" />
    </button>
  );
}

function savedValueText(override: UsageContextOverride): string {
  const context = override.contextWindowTokens;
  const percent = override.compactBufferMode === "percent";
  const buffer = percent ? override.autoCompactBufferPercent : override.autoCompactBufferTokens;
  const contextText = context == null ? "Auto" : contextTokenText(context);
  if (buffer == null) return contextText;
  return `${contextText} / ${percent ? `${buffer}%` : contextTokenText(buffer)}`;
}
