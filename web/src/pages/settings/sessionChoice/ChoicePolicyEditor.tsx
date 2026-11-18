import { useRef, useState } from "react";
import { SortableContext, verticalListSortingStrategy } from "@dnd-kit/sortable";
import { XIcon } from "lucide-react";
import { HelpTip } from "@/components/HelpTip";
import { applyPolicyDrop } from "./choicePolicyDrop";
import { Button } from "@/components/ui/button";
import { Combobox, COMBOBOX_OPTION_THRESHOLD } from "@/components/ui/combobox";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import type { Host } from "@/hooks/useHosts";
import type { CallingDefaultsCatalogRow } from "@/lib/callingDefaultsApi";
import type { UsageAccountView } from "@/pages/usageBoard/usageBoardData";
import {
  choiceRequest,
  type ChoiceEnvelope,
  type ChoiceRow,
  type SessionChoiceDocument,
} from "./sessionChoiceData";
import { projectChoiceDrainTags } from "./choiceDrainLocks";
import {
  addToOtherTypes,
  applyPreset,
  choiceId,
  choiceTokenEstimate,
  copyStrategy,
  removePreset,
  removeWorkType,
  renameWorkType,
  starter,
  weightOf,
  type ChoiceAgent,
} from "./sessionChoiceEdits";
import { ChoiceDragContext, ChoiceDraggable, ChoiceDropZone, type DropIntent } from "./ChoiceDrag";
import { CHOICE_SELECT, ChoiceChainEditor } from "./ChoiceLinkEditor";

const EMPTY_CATALOGS: CallingDefaultsCatalogRow[] = [];

export function ChoicePolicyEditor({
  document,
  savedChoice,
  host,
  hosts,
  agents,
  meters,
  catalogs = EMPTY_CATALOGS,
  onChange,
}: {
  document: SessionChoiceDocument;
  savedChoice?: ChoiceEnvelope;
  host: string;
  hosts: Host[];
  agents: ChoiceAgent[];
  meters: UsageAccountView[];
  catalogs?: CallingDefaultsCatalogRow[];
  onChange: (value: SessionChoiceDocument) => void;
}) {
  const [selected, setSelected] = useState("");
  const work = document.workTypes.find((w) => w.id === selected) ?? document.workTypes[0];
  const [workLabel, setWorkLabel] = useState<{ id: string; value: string } | null>(null);
  const [presetId, setPresetId] = useState<string | null>(null);
  const presetEditor = useRef<HTMLDivElement>(null);
  const [abilityPreset, setAbilityPreset] = useState<string | null>(null);
  const [editRow, setEditRow] = useState<{ tier: number; row: number } | null>(null);
  const [copyHost, setCopyHost] = useState("");
  const [copyWork, setCopyWork] = useState("");
  const [undo, setUndo] = useState<{
    host: string;
    work: string | null;
    strategy: Record<string, ChoiceRow[][]> | undefined;
  } | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastAdded, setLastAdded] = useState<{ preset: string; tier: number } | null>(null);
  const [rowIds] = useState(() => new WeakMap<ChoiceRow, string>());
  const [containerIds] = useState(() => new WeakMap<object, string>());
  const [addAbilityLevel] = useState<string[]>([]);
  function containerId(container: object) {
    if (!containerIds.has(container))
      containerIds.set(container, `container:${crypto.randomUUID()}`);
    return containerIds.get(container)!;
  }
  function rowId(tier: number, row: number) {
    const item = tiers[tier][row];
    if (!rowIds.has(item)) rowIds.set(item, `row:${crypto.randomUUID()}`);
    return rowIds.get(item)!;
  }
  const [previewing, setPreviewing] = useState(false);
  const targetHost = hosts.find((h) => h.host_id === host);
  const tiers = work ? (document.hosts[host]?.[work.id] ?? []) : [];
  const drainTags = projectChoiceDrainTags(savedChoice, host, work?.id, tiers);
  const preset = document.presets.find((p) => p.id === presetId);
  function change(next: SessionChoiceDocument) {
    const nextTiers = work && next.hosts[host]?.[work.id];
    nextTiers?.forEach((rows, index) => {
      const previous = tiers[index];
      if (!previous) return;
      if (!containerIds.has(rows)) containerIds.set(rows, containerId(previous));
      if (rows.length === previous.length)
        rows.forEach((row, rowIndex) => {
          const previousId = rowIds.get(previous[rowIndex]);
          if (!rowIds.has(row) && previousId) rowIds.set(row, previousId);
        });
    });
    next.abilityOrder.forEach((level, index) => {
      const previous = document.abilityOrder[index];
      if (previous && !containerIds.has(level)) containerIds.set(level, containerId(previous));
    });
    setError(null);
    setMessage(null);
    onChange(next);
  }
  function updateTiers(nextTiers: ChoiceRow[][]) {
    if (!work) return;
    change({
      ...document,
      hosts: { ...document.hosts, [host]: { ...document.hosts[host], [work.id]: nextTiers } },
    });
  }
  function updateRow(tier: number, index: number, patch: Partial<ChoiceRow>) {
    updateTiers(
      tiers.map((rows, i) =>
        i === tier ? rows.map((r, j) => (j === index ? { ...r, ...patch } : r)) : rows,
      ),
    );
  }
  function guarded(action: () => SessionChoiceDocument) {
    try {
      change(action());
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Could not change this setting.");
    }
  }
  function dropped(intent: DropIntent) {
    setEditRow(null);
    const next = applyPolicyDrop(document, host, work?.id, intent);
    if (next === document || JSON.stringify(next) === JSON.stringify(document))
      return { focus: intent.source.id, message: "Position unchanged." };
    if (intent.source.data.kind === "work" && work) setSelected(work.id);
    change(next);
    let focus = intent.source.id;
    const from = intent.source.data;
    const to = intent.target.data;
    if (from.kind === "preset" && (to.kind === "tier" || to.kind === "row") && work) {
      const oldRows = new Set(tiers.flat());
      const added = next.hosts[host][work.id][to.tier].find((row) => !oldRows.has(row));
      if (added) {
        const id = `row:${crypto.randomUUID()}`;
        rowIds.set(added, id);
        focus = id;
      }
      setLastAdded({ preset: from.preset, tier: to.tier });
    } else if (from.kind === "preset" && (to.kind === "ability" || to.kind === "ability-level")) {
      focus = `ability:${from.preset}`;
    }
    return { focus, message: `${intent.message}. Not saved yet.` };
  }
  function renamePreset(name: string) {
    if (!preset) return;
    const next = structuredClone(document);
    for (const types of Object.values(next.hosts))
      for (const ts of Object.values(types))
        for (const tier of ts)
          for (const row of tier) if (row.preset === preset.id && !row.name) row.name = preset.name;
    next.presets = next.presets.map((p) => (p.id === preset.id ? { ...p, name } : p));
    change(next);
  }
  return (
    <div className="space-y-3">
      {!document.workTypes.length && (
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm text-muted-foreground">Start with</span>
          {([false, true] as const).map((simple) => (
            <Button
              key={String(simple)}
              size="sm"
              variant="outline"
              disabled={!targetHost || !agents.length}
              onClick={() => {
                if (targetHost) {
                  change(starter(targetHost, agents, meters, simple));
                  setSelected("");
                }
              }}
            >
              {simple ? "Simple" : "Recommended"}
            </Button>
          ))}
        </div>
      )}
      <ChoiceDragContext
        scope={`${host}:${work?.id}`}
        reset={document}
        abilityOrder={document.abilityOrder}
        onDrop={dropped}
      >
        <div className="grid min-w-0 gap-3 md:grid-cols-[172px_minmax(0,1fr)]">
          <div className="min-w-0 space-y-2">
            <p className="text-sm font-medium">Work types</p>
            <SortableContext
              items={document.workTypes.map((w) => `work:${w.id}`)}
              strategy={verticalListSortingStrategy}
            >
              {document.workTypes.map((w) => (
                <ChoiceDraggable
                  key={w.id}
                  id={`work:${w.id}`}
                  data={{ kind: "work", work: w.id }}
                  label={`${w.label} work type`}
                  className={w.id === work?.id ? "bg-accent" : "hover:bg-accent/50"}
                >
                  <button
                    type="button"
                    aria-current={w.id === work?.id ? "true" : undefined}
                    className="min-h-9 min-w-0 flex-1 truncate pr-2 text-left text-sm md:min-h-7"
                    onClick={() => {
                      setSelected(w.id);
                      setEditRow(null);
                    }}
                  >
                    {w.label}
                  </button>
                </ChoiceDraggable>
              ))}
            </SortableContext>
            <Button
              size="sm"
              variant="outline"
              disabled={document.workTypes.length >= 24}
              onClick={() => {
                let number = document.workTypes.length + 1;
                while (
                  document.workTypes.some(
                    (w) => w.id === `work-${number}` || w.aliases?.includes(`work-${number}`),
                  )
                )
                  number++;
                const id = `work-${number}`;
                change({
                  ...document,
                  workTypes: [...document.workTypes, { id, label: `Work ${number}` }],
                });
                setSelected(id);
              }}
            >
              + Work type
            </Button>
            {document.workTypes.length > 0 && (
              <label className="flex flex-col gap-1 text-xs">
                Default work type
                {document.workTypes.length > COMBOBOX_OPTION_THRESHOLD ? (
                  <Combobox
                    mode="select"
                    options={document.workTypes.map((item) => ({
                      key: item.id,
                      label: item.label,
                    }))}
                    value={document.defaultWorkType ?? document.workTypes[0].id}
                    onValueChange={(value) => change({ ...document, defaultWorkType: value })}
                    aria-label="Default work type"
                    className="h-auto w-full rounded-none! bg-transparent p-0"
                  />
                ) : (
                  <select
                    className={CHOICE_SELECT}
                    value={document.defaultWorkType ?? document.workTypes[0].id}
                    onChange={(e) => change({ ...document, defaultWorkType: e.target.value })}
                  >
                    {document.workTypes.map((w) => (
                      <option key={w.id} value={w.id}>
                        {w.label}
                      </option>
                    ))}
                  </select>
                )}
              </label>
            )}
          </div>
          <div className="min-w-0 space-y-3">
            {work && (
              <>
                <div className="flex flex-wrap items-end gap-2">
                  <label className="flex min-w-32 flex-1 flex-col gap-1 text-xs">
                    Name
                    <Input
                      className="h-8"
                      aria-label="Work type name"
                      value={workLabel?.id === work.id ? workLabel.value : work.label}
                      onChange={(e) => setWorkLabel({ id: work.id, value: e.target.value })}
                      onBlur={() => {
                        if (workLabel?.id !== work.id) return;
                        try {
                          const next = renameWorkType(document, work.id, workLabel.value);
                          change(next);
                          setSelected(choiceId(workLabel.value));
                          setWorkLabel(null);
                        } catch (failure) {
                          setError((failure as Error).message);
                        }
                      }}
                    />
                  </label>
                  <label className="flex flex-col gap-1 text-xs">
                    If full
                    <select
                      className={CHOICE_SELECT}
                      value={work.ifFull ?? "queue"}
                      onChange={(e) =>
                        change({
                          ...document,
                          workTypes: document.workTypes.map((w) =>
                            w.id === work.id
                              ? { ...w, ifFull: e.target.value as "queue" | "ask" }
                              : w,
                          ),
                        })
                      }
                    >
                      <option value="queue">Queue</option>
                      <option value="ask">Ask</option>
                    </select>
                  </label>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => guarded(() => removeWorkType(document, work.id))}
                  >
                    Delete work type
                  </Button>
                </div>
                <label className="flex flex-col gap-1 text-xs">
                  Definition
                  <Textarea
                    aria-label="Work type definition"
                    className="min-h-14 text-sm"
                    rows={2}
                    maxLength={160}
                    value={work.definition ?? ""}
                    placeholder="Optional, included at session start"
                    onChange={(e) =>
                      change({
                        ...document,
                        workTypes: document.workTypes.map((w) =>
                          w.id === work.id ? { ...w, definition: e.target.value } : w,
                        ),
                      })
                    }
                  />
                </label>
                <div className="flex flex-wrap items-center gap-1.5 text-xs">
                  <span>Copy</span>
                  {hosts.length > COMBOBOX_OPTION_THRESHOLD ? (
                    <Combobox
                      mode="select"
                      options={hosts.map((item) => ({
                        key: item.host_id,
                        label: item.name,
                        description: item.status === "online" ? undefined : "Offline",
                      }))}
                      value={copyHost || host}
                      onValueChange={setCopyHost}
                      aria-label="Copy from host"
                      className="h-auto w-44 rounded-none! bg-transparent p-0"
                    />
                  ) : (
                    <select
                      aria-label="Copy from host"
                      className={CHOICE_SELECT}
                      value={copyHost || host}
                      onChange={(e) => setCopyHost(e.target.value)}
                    >
                      {hosts.map((h) => (
                        <option key={h.host_id} value={h.host_id}>
                          {h.name}
                        </option>
                      ))}
                    </select>
                  )}
                  {document.workTypes.length > COMBOBOX_OPTION_THRESHOLD ? (
                    <Combobox
                      mode="select"
                      options={[
                        { key: "*", label: "All work types" },
                        ...document.workTypes.map((item) => ({
                          key: item.id,
                          label: item.label,
                        })),
                      ]}
                      value={copyWork || work.id}
                      onValueChange={setCopyWork}
                      aria-label="Copy from work type"
                      className="h-auto w-44 rounded-none! bg-transparent p-0"
                    />
                  ) : (
                    <select
                      aria-label="Copy from work type"
                      className={CHOICE_SELECT}
                      value={copyWork || work.id}
                      onChange={(e) => setCopyWork(e.target.value)}
                    >
                      <option value="*">All work types</option>
                      {document.workTypes.map((w) => (
                        <option key={w.id} value={w.id}>
                          {w.label}
                        </option>
                      ))}
                    </select>
                  )}
                  <span>to here</span>
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={!targetHost}
                    onClick={() => {
                      if (!targetHost) return;
                      const models = Object.fromEntries(
                        agents.map((a) => [
                          a.id,
                          catalogs
                            .find((c) => c.host_id === host && c.harness === a.harness)
                            ?.models.map((m) => m.id) ?? [],
                        ]),
                      );
                      const result = copyStrategy(
                        document,
                        copyHost || host,
                        copyWork || work.id,
                        targetHost,
                        work.id,
                        agents,
                        models,
                      );
                      setUndo({
                        host,
                        work: copyWork === "*" ? null : work.id,
                        strategy: structuredClone(document.hosts[host]),
                      });
                      change(result.document);
                      setEditRow(null);
                      setMessage(["Copied strategy.", ...result.changes].join(" "));
                    }}
                  >
                    Copy
                  </Button>
                  {undo && (
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => {
                        const next = structuredClone(document);
                        if (undo.work) {
                          const previous = undo.strategy?.[undo.work];
                          if (previous) (next.hosts[undo.host] ??= {})[undo.work] = previous;
                          else if (next.hosts[undo.host]) {
                            Reflect.deleteProperty(next.hosts[undo.host], undo.work);
                            if (!Object.keys(next.hosts[undo.host]).length)
                              Reflect.deleteProperty(next.hosts, undo.host);
                          }
                        } else if (undo.strategy) next.hosts[undo.host] = undo.strategy;
                        else Reflect.deleteProperty(next.hosts, undo.host);
                        change(next);
                        setUndo(null);
                        setMessage("Restored strategy.");
                      }}
                    >
                      Undo
                    </Button>
                  )}
                </div>
                <div
                  className="flex min-w-0 items-stretch gap-2 overflow-x-auto pb-2"
                  aria-label="Tiers"
                >
                  {tiers.map((rows, tierIndex) => {
                    const total = rows.reduce(
                      (sum, row, index) => sum + weightOf(row, index, rows.length),
                      0,
                    );
                    return (
                      <ChoiceDropZone
                        key={containerId(rows)}
                        id={containerId(rows)}
                        data={{ kind: "tier", tier: tierIndex }}
                        label={`Tier ${tierIndex + 1}`}
                        className="min-h-32 w-64 shrink-0 space-y-2 bg-muted/15"
                      >
                        <div className="flex items-center justify-between">
                          <h4 className="text-sm font-medium">Tier {tierIndex + 1}</h4>
                          <button
                            type="button"
                            aria-label={`Remove Tier ${tierIndex + 1}`}
                            className="rounded p-1 focus-visible:ring-2"
                            onClick={() => {
                              updateTiers(tiers.filter((_, i) => i !== tierIndex));
                              setEditRow(null);
                            }}
                          >
                            <XIcon className="size-3.5" />
                          </button>
                        </div>
                        <SortableContext
                          items={rows.map((_, i) => rowId(tierIndex, i))}
                          strategy={verticalListSortingStrategy}
                        >
                          {rows.map((row, index) => {
                            const name =
                              row.name ??
                              document.presets.find((p) => p.id === row.preset)?.name ??
                              "Adjusted preset";
                            const first = row.links[0];
                            return (
                              <div
                                key={rowId(tierIndex, index)}
                                className="rounded-md border border-border bg-background p-1.5"
                              >
                                <ChoiceDraggable
                                  id={rowId(tierIndex, index)}
                                  data={{ kind: "row", tier: tierIndex, row: index }}
                                  label={`${name} in Tier ${tierIndex + 1}`}
                                >
                                  <button
                                    type="button"
                                    className="min-w-0 flex-1 text-left text-sm"
                                    onClick={() =>
                                      setEditRow(
                                        editRow?.tier === tierIndex && editRow.row === index
                                          ? null
                                          : { tier: tierIndex, row: index },
                                      )
                                    }
                                  >
                                    <span className="block truncate font-medium">{name}</span>
                                    <span className="block truncate text-xs text-muted-foreground">
                                      {agents.find((a) => a.id === first?.agent)?.display_name ??
                                        first?.agent}
                                      {first?.model ? ` · ${first.model}` : " · default"}
                                    </span>
                                    {row.links.length > 1 && (
                                      <span className="text-xs text-muted-foreground">
                                        → {row.links.length - 1} fallback
                                        {row.links.length > 2 ? "s" : ""}
                                      </span>
                                    )}
                                    {row.adjusted && (
                                      <span className="ml-1 text-xs text-warning">Adjusted</span>
                                    )}
                                  </button>
                                  <button
                                    type="button"
                                    aria-label={`Remove ${name} from Tier ${tierIndex + 1}`}
                                    className="rounded p-1 focus-visible:ring-2"
                                    onClick={() => {
                                      updateTiers(
                                        tiers.map((rs, t) =>
                                          t === tierIndex ? rs.filter((_, i) => i !== index) : rs,
                                        ),
                                      );
                                      setEditRow(null);
                                    }}
                                  >
                                    <XIcon className="size-3.5" />
                                  </button>
                                </ChoiceDraggable>
                                <label className="mt-1 flex items-center justify-end gap-1.5 text-xs">
                                  Weight
                                  <Input
                                    className="h-7 w-14 px-1 text-center tabular-nums"
                                    aria-label={`${name} weight in Tier ${tierIndex + 1}`}
                                    type="number"
                                    min={1}
                                    max={9}
                                    step={1}
                                    value={weightOf(row, index, rows.length)}
                                    onChange={(e) =>
                                      updateRow(tierIndex, index, {
                                        weight: e.target.value
                                          ? Math.min(
                                              9,
                                              Math.max(1, Math.round(Number(e.target.value))),
                                            )
                                          : null,
                                      })
                                    }
                                  />
                                </label>
                                {editRow?.tier === tierIndex && editRow.row === index && (
                                  <div className="mt-2 space-y-2 border-t border-border pt-2">
                                    <ChoiceChainEditor
                                      links={row.links}
                                      drainTag={drainTags.get(row)}
                                      agents={agents}
                                      host={targetHost}
                                      meters={meters}
                                      limits={document.limits}
                                      onChange={(links) =>
                                        updateRow(tierIndex, index, { links, adjusted: true })
                                      }
                                    />
                                    {row.preset &&
                                      document.presets.some((p) => p.id === row.preset) && (
                                        <Button
                                          size="sm"
                                          variant="outline"
                                          onClick={() => {
                                            const p = document.presets.find(
                                              (candidate) => candidate.id === row.preset,
                                            )!;
                                            updateRow(tierIndex, index, {
                                              links: structuredClone(p.links),
                                              name: p.name,
                                              adjusted: false,
                                            });
                                          }}
                                        >
                                          Back to preset
                                        </Button>
                                      )}
                                  </div>
                                )}
                              </div>
                            );
                          })}
                        </SortableContext>
                        {!rows.length && (
                          <p className="py-4 text-center text-xs text-muted-foreground">
                            Drag a preset here
                          </p>
                        )}
                        {!!total && (
                          <>
                            <div
                              className="flex h-1.5 overflow-hidden rounded bg-muted"
                              aria-hidden="true"
                            >
                              {rows.map((row, index) => (
                                <span
                                  key={rowId(tierIndex, index)}
                                  className={index % 2 ? "bg-primary/40" : "bg-primary/75"}
                                  style={{
                                    width: `${(weightOf(row, index, rows.length) / total) * 100}%`,
                                  }}
                                />
                              ))}
                            </div>
                            <p
                              className="text-xs tabular-nums text-muted-foreground"
                              aria-label={`Tier ${tierIndex + 1} shares`}
                            >
                              {rows
                                .map(
                                  (row, i) =>
                                    `${row.name ?? document.presets.find((p) => p.id === row.preset)?.name ?? "Row"} ${Math.round((weightOf(row, i, rows.length) / total) * 100)}%`,
                                )
                                .join(" · ")}
                            </p>
                          </>
                        )}
                      </ChoiceDropZone>
                    );
                  })}
                  <Button
                    size="sm"
                    variant="outline"
                    className="shrink-0"
                    onClick={() => updateTiers([...tiers, []])}
                  >
                    + Tier
                  </Button>
                </div>
                {lastAdded && (
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => {
                      change(addToOtherTypes(document, host, lastAdded.preset, lastAdded.tier));
                      setMessage(
                        `Added to Tier ${lastAdded.tier + 1} of other work types; existing rows kept.`,
                      );
                      setLastAdded(null);
                    }}
                  >
                    Also add to Tier {lastAdded.tier + 1} of every work type
                  </Button>
                )}
              </>
            )}
          </div>
        </div>
        <div className="space-y-2 border-t border-border pt-3">
          <div className="flex items-center gap-2">
            <h4 className="text-sm font-medium">Presets</h4>
            <Button
              size="sm"
              variant="outline"
              onClick={() => {
                const id = `preset-${crypto.randomUUID().slice(0, 12)}`;
                change({
                  ...document,
                  presets: [
                    ...document.presets,
                    { id, name: "New preset", links: [{ agent: "" }] },
                  ],
                });
                setPresetId(id);
              }}
            >
              + New preset
            </Button>
          </div>
          <SortableContext items={document.presets.map((p) => `preset:${p.id}`)}>
            <div className="flex flex-wrap gap-2">
              {document.presets.map((p) => (
                <ChoiceDraggable
                  key={p.id}
                  id={`preset:${p.id}`}
                  data={{ kind: "preset", preset: p.id }}
                  label={p.name}
                  className="items-center border border-border bg-card py-1 pr-2"
                >
                  <button
                    type="button"
                    className="text-sm"
                    onClick={() => setPresetId(presetId === p.id ? null : p.id)}
                  >
                    {p.name}
                  </button>
                </ChoiceDraggable>
              ))}
            </div>
          </SortableContext>
          {preset && (
            <div
              ref={presetEditor}
              className="max-w-xl space-y-2 rounded-lg border border-border p-3"
            >
              {abilityPreset === preset.id && (
                <p className="text-xs text-muted-foreground" role="note">
                  This shared preset is used in{" "}
                  {Object.values(document.hosts)
                    .flatMap((types) => Object.values(types).flat(2))
                    .filter((row) => row.preset === preset.id).length +
                    document.abilityOrder.flat().filter((id) => id === preset.id).length -
                    1}{" "}
                  other places. Edits affect the shared preset. Use Apply below to update its
                  unadjusted tier rows; adjusted rows keep their own links.
                </p>
              )}
              <div className="flex items-end gap-2">
                <label className="flex min-w-0 flex-1 flex-col gap-1 text-xs">
                  Preset name
                  <Input
                    className="h-8"
                    aria-label="Preset name"
                    maxLength={128}
                    value={preset.name}
                    onChange={(e) => renamePreset(e.target.value)}
                  />
                </label>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => {
                    change(removePreset(document, preset.id));
                    setPresetId(null);
                  }}
                >
                  Delete preset
                </Button>
              </div>
              <ChoiceChainEditor
                key={preset.id}
                links={preset.links}
                agents={agents}
                host={targetHost}
                meters={meters}
                limits={document.limits}
                onChange={(links) =>
                  change({
                    ...document,
                    presets: document.presets.map((p) =>
                      p.id === preset.id ? { ...p, links } : p,
                    ),
                  })
                }
              />
              <Button
                variant="outline"
                size="sm"
                onClick={() => {
                  const result = applyPreset(document, preset.id);
                  change(result.document);
                  setMessage(
                    `${result.updated} updated, ${result.adjusted} adjusted left as they are.`,
                  );
                }}
              >
                Apply to {applyPreset(document, preset.id).updated} rows
              </Button>
            </div>
          )}
        </div>
        <details className="border-t border-border pt-3">
          <summary className="flex cursor-pointer items-center gap-2 text-sm font-medium">
            <span>Ability order · Delegation ceiling</span>
            <HelpTip label="About Ability order">
              <p>
                Lower numbers mean higher ability. A caller can delegate to the same or a lower
                ability level.
              </p>
              <p className="mt-2">
                For example, Codex at Level 2 cannot delegate to Claude at Level 1.
              </p>
              <p className="mt-2">
                If either side has no level mapping, this condition does not block it. Tier order,
                weights and availability checks still apply.
              </p>
              <p className="mt-2">
                Each entry names a preset. Its Agent and Model links share that preset's ability
                level. Entries in one level have equal ability; their order is not a preference.
              </p>
            </HelpTip>
          </summary>
          <div className="mt-1 text-xs text-muted-foreground">
            {document.abilityOrder.some((level) => level.length)
              ? "Higher ability has a lower level number"
              : "No delegation ceiling configured"}
          </div>
          <div className="mt-2 flex flex-wrap items-start gap-2">
            {[...document.abilityOrder, addAbilityLevel].map((level, index) => (
              <ChoiceDropZone
                key={containerId(level)}
                id={containerId(level)}
                data={{ kind: "ability-level", level: index }}
                label={`Ability level ${index + 1}`}
                className="min-h-20 w-52 space-y-1"
              >
                <p className="text-xs font-medium">
                  Level {index + 1}
                  {index === document.abilityOrder.length ? " · add" : ""}
                </p>
                <p className="text-xs text-muted-foreground">
                  {index === document.abilityOrder.length
                    ? "Drop a preset here to add a lower ability level."
                    : `Callers at Level ${index + 1} can delegate to this level or a higher-numbered level.`}
                </p>
                <SortableContext items={level.map((p) => `ability:${p}`)}>
                  {level.map((p) => (
                    <ChoiceDraggable
                      key={p}
                      id={`ability:${p}`}
                      data={{ kind: "ability", level: index, preset: p }}
                      label={`${p} ability`}
                    >
                      <button
                        type="button"
                        className="min-w-0 flex-1 break-words text-left text-sm"
                        aria-label={`Edit ${document.presets.find((item) => item.id === p)?.name ?? p} preset from ability order`}
                        onClick={() => {
                          setPresetId(p);
                          setAbilityPreset(p);
                          requestAnimationFrame(() => {
                            presetEditor.current?.scrollIntoView?.({ block: "nearest" });
                            presetEditor.current?.querySelector<HTMLInputElement>("input")?.focus();
                          });
                        }}
                      >
                        {document.presets.find((item) => item.id === p)?.name ?? p}
                      </button>
                      <button
                        type="button"
                        aria-label={`Remove ${p} from ability order`}
                        onClick={() =>
                          change({
                            ...document,
                            abilityOrder: document.abilityOrder.map((ids) =>
                              ids.filter((id) => id !== p),
                            ),
                          })
                        }
                      >
                        <XIcon className="size-3.5" />
                      </button>
                    </ChoiceDraggable>
                  ))}
                </SortableContext>
              </ChoiceDropZone>
            ))}
          </div>
        </details>
      </ChoiceDragContext>
      <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        <span>About {choiceTokenEstimate(document)} tokens at each session start</span>
        {work && (
          <Button
            variant="ghost"
            size="sm"
            disabled={previewing || !host}
            onClick={async () => {
              setPreviewing(true);
              try {
                const result = await choiceRequest<{
                  reason: string;
                  agent?: string;
                  pool?: string;
                }>(
                  `/preview?host_id=${encodeURIComponent(host)}&work_type=${encodeURIComponent(work.id)}`,
                );
                setMessage(
                  `${agents.find((a) => a.id === result.agent)?.display_name ?? "No pick"}: ${result.reason}`,
                );
              } catch (failure) {
                setError((failure as Error).message);
              } finally {
                setPreviewing(false);
              }
            }}
          >
            Preview saved choice
          </Button>
        )}
      </div>
      {message && (
        <p role="status" className="text-sm text-muted-foreground">
          {message}
        </p>
      )}
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
    </div>
  );
}
