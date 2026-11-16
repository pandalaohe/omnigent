import { useId, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { SortableContext, verticalListSortingStrategy } from "@dnd-kit/sortable";
import { ChoiceDragContext, ChoiceDraggable } from "./ChoiceDrag";
import { insertAtAnchor } from "./choiceDrop";
import { Button } from "@/components/ui/button";
import { Combobox, COMBOBOX_OPTION_THRESHOLD } from "@/components/ui/combobox";
import { Input } from "@/components/ui/input";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import { useHosts } from "@/hooks/useHosts";
import {
  listCallingDefaultCatalogs,
  type CallingDefaultsCatalogRow,
} from "@/lib/callingDefaultsApi";
import { canonicalNativeHarness } from "@/lib/nativeHarnessModes";
import { CHOICE_SELECT } from "./ChoiceLinkEditor";
import { ChoiceCandidateCheck } from "./ChoiceCandidateCheck";
import { limitRuleErrors, moveLimitRule, replaceLimitRules } from "./choiceLimitEdits";
import type { ChoiceLimitRule, SessionChoiceDocument } from "./sessionChoiceData";

function NumberPatch({
  label,
  value,
  max,
  onChange,
}: {
  label: string;
  value: number | null | undefined;
  max?: number;
  onChange: (value: number | null | undefined) => void;
}) {
  const state = value === undefined ? "inherit" : value === null ? "unlimited" : "value";
  return (
    <div className="grid gap-1">
      <label className="flex flex-wrap items-center gap-2 text-sm">
        {label}
        <select
          aria-label={`${label} setting`}
          className={CHOICE_SELECT}
          value={state}
          onChange={(event) =>
            onChange(
              event.target.value === "inherit"
                ? undefined
                : event.target.value === "unlimited"
                  ? null
                  : max
                    ? 0
                    : 1,
            )
          }
        >
          <option value="inherit">Inherit</option>
          <option value="value">Set value</option>
          <option value="unlimited">{max ? "No threshold" : "Unlimited"}</option>
        </select>
      </label>
      {state === "value" && (
        <label className="flex items-center gap-2 text-sm">
          {label} value
          <Input
            aria-label={`${label} value`}
            type="number"
            className="h-9 w-28"
            min={max ? 0 : 1}
            max={max}
            step={max ? "any" : 1}
            value={Number.isNaN(value) ? "" : (value ?? "")}
            onChange={(event) =>
              onChange(event.target.value === "" ? undefined : Number(event.target.value))
            }
          />
          {max ? "%" : "sessions"}
        </label>
      )}
    </div>
  );
}

function RuleFields({
  rule,
  document,
  catalogs,
  onChange,
}: {
  rule: ChoiceLimitRule;
  document: SessionChoiceDocument;
  catalogs: CallingDefaultsCatalogRow[];
  onChange: (rule: ChoiceLimitRule) => void;
}) {
  const listId = useId();
  const agents = useAvailableAgents();
  const hosts = useHosts();
  const agent = agents.data?.find((item) => item.id === rule.agent);
  const agentOptions = agents.data ?? [];
  const hostOptions = hosts.data ?? [];
  const models = [
    ...new Set(
      catalogs
        .filter(
          (row) =>
            (!rule.host || row.host_id === rule.host) &&
            agent?.harness &&
            canonicalNativeHarness(row.harness.replaceAll("_", "-")) ===
              canonicalNativeHarness(agent.harness.replaceAll("_", "-")),
        )
        .flatMap((row) => row.models.map((model) => model.id)),
    ),
  ];
  function field(
    name: "maxRunning" | "reserveExempt",
    value: number | string[] | null | undefined,
  ) {
    const next = { ...rule };
    if (value === undefined) Reflect.deleteProperty(next, name);
    else Object.assign(next, { [name]: value });
    onChange(next);
  }
  function reserve(name: string, value: string | number | null | undefined) {
    const patch = rule.reserve === null ? { pct: null, stopPct: null } : { ...rule.reserve };
    if (value === undefined) Reflect.deleteProperty(patch, name);
    else Object.assign(patch, { [name]: value });
    const next = { ...rule, reserve: patch };
    if (!Object.keys(patch).length) Reflect.deleteProperty(next, "reserve");
    onChange(next);
  }
  return (
    <div className="space-y-4 pt-3">
      <div className="grid gap-3 sm:grid-cols-3">
        <label className="grid min-w-0 gap-1 text-sm">
          Agent
          {agentOptions.length > COMBOBOX_OPTION_THRESHOLD ? (
            <Combobox
              mode="select"
              options={[
                ...(rule.agent && !agent
                  ? [{ key: rule.agent, label: `${rule.agent} (unavailable)` }]
                  : []),
                ...agentOptions.map((item) => ({
                  key: item.id,
                  label: item.display_name,
                  description: item.name,
                })),
              ]}
              value={rule.agent}
              onValueChange={(value) => onChange({ ...rule, agent: value })}
              placeholder="Choose an agent"
              aria-label="Rule agent"
              className="h-auto w-full rounded-none! bg-transparent p-0"
            />
          ) : (
            <select
              className={CHOICE_SELECT}
              value={rule.agent}
              onChange={(e) => onChange({ ...rule, agent: e.target.value })}
            >
              <option value="" disabled>
                Choose an agent
              </option>
              {rule.agent && !agent && (
                <option value={rule.agent}>{rule.agent} (unavailable)</option>
              )}
              {agentOptions.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.display_name}
                </option>
              ))}
            </select>
          )}
          <span className="break-words text-muted-foreground">
            {agent?.display_name ?? rule.agent}
          </span>
        </label>
        <label className="grid min-w-0 gap-1 text-sm">
          Host
          {hostOptions.length > COMBOBOX_OPTION_THRESHOLD ? (
            <Combobox
              mode="select"
              options={[
                { key: "__all__", label: "All hosts" },
                ...(rule.host && !hostOptions.some((item) => item.host_id === rule.host)
                  ? [{ key: rule.host, label: `${rule.host} (unavailable)` }]
                  : []),
                ...hostOptions.map((item) => ({
                  key: item.host_id,
                  label: item.name,
                  description: item.status === "online" ? undefined : "Offline",
                })),
              ]}
              value={rule.host ?? "__all__"}
              onValueChange={(value) => {
                const next = { ...rule };
                if (value !== "__all__") next.host = value;
                else Reflect.deleteProperty(next, "host");
                onChange(next);
              }}
              aria-label="Rule host"
              className="h-auto w-full rounded-none! bg-transparent p-0"
            />
          ) : (
            <select
              className={CHOICE_SELECT}
              value={rule.host ?? ""}
              onChange={(e) => {
                const next = { ...rule };
                if (e.target.value) next.host = e.target.value;
                else Reflect.deleteProperty(next, "host");
                onChange(next);
              }}
            >
              <option value="">All hosts</option>
              {rule.host && !hostOptions.some((item) => item.host_id === rule.host) && (
                <option value={rule.host}>{rule.host} (unavailable)</option>
              )}
              {hostOptions.map((item) => (
                <option key={item.host_id} value={item.host_id}>
                  {item.name}
                </option>
              ))}
            </select>
          )}
          <span className="break-words text-muted-foreground">
            {hosts.data?.find((item) => item.host_id === rule.host)?.name ??
              rule.host ??
              "All hosts"}
          </span>
        </label>
        <label className="grid min-w-0 gap-1 text-sm">
          Model
          <Input
            list={listId}
            placeholder="All models"
            value={rule.model ?? ""}
            onChange={(e) => {
              const next = { ...rule };
              if (e.target.value) next.model = e.target.value;
              else Reflect.deleteProperty(next, "model");
              onChange(next);
            }}
          />
          <datalist id={listId}>
            {models.map((model) => (
              <option key={model} value={model} />
            ))}
          </datalist>
          <span className="break-words text-muted-foreground">{rule.model ?? "All models"}</span>
        </label>
      </div>
      <div className="grid gap-4 md:grid-cols-3">
        <NumberPatch
          label="Max running"
          value={rule.maxRunning}
          onChange={(value) => field("maxRunning", value)}
        />
        <NumberPatch
          label="Warning"
          value={rule.reserve === null ? null : rule.reserve?.pct}
          max={100}
          onChange={(value) => reserve("pct", value)}
        />
        <NumberPatch
          label="Stop"
          value={rule.reserve === null ? null : rule.reserve?.stopPct}
          max={100}
          onChange={(value) => reserve("stopPct", value)}
        />
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="flex flex-wrap items-center gap-2 text-sm">
          Usage mode
          <select
            className={CHOICE_SELECT}
            value={rule.reserve?.mode ?? ""}
            onChange={(e) => reserve("mode", e.target.value || undefined)}
          >
            <option value="">Inherit</option>
            <option value="total">Total</option>
            <option value="daily">Daily</option>
          </select>
        </label>
        <label className="grid min-w-0 gap-1 text-sm">
          Usage window
          <Input
            placeholder="Inherit"
            value={rule.reserve?.window ?? ""}
            onChange={(e) => reserve("window", e.target.value || undefined)}
          />
        </label>
      </div>
      <div className="space-y-2">
        <label className="flex flex-wrap items-center gap-2 text-sm">
          Warning exemptions
          <select
            className={CHOICE_SELECT}
            value={rule.reserveExempt === undefined ? "inherit" : "set"}
            onChange={(e) => field("reserveExempt", e.target.value === "inherit" ? undefined : [])}
          >
            <option value="inherit">Inherit</option>
            <option value="set">Set work types (empty clears exemptions)</option>
          </select>
        </label>
        {rule.reserveExempt !== undefined && (
          <div className="flex flex-wrap gap-3">
            {document.workTypes.map((work) => (
              <label key={work.id} className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={rule.reserveExempt?.includes(work.id) ?? false}
                  onChange={(e) =>
                    field(
                      "reserveExempt",
                      e.target.checked
                        ? [...(rule.reserveExempt ?? []), work.id]
                        : rule.reserveExempt?.filter((id) => id !== work.id),
                    )
                  }
                />
                {work.label}
              </label>
            ))}
          </div>
        )}
        <p className="text-xs text-muted-foreground">
          Exemptions apply to warnings only. Stop thresholds and session caps still apply.
        </p>
      </div>
    </div>
  );
}

export function ChoiceLimitRules({
  document,
  previewDocument,
  previewEpoch,
  host,
  revision,
  canEdit,
  onChange,
}: {
  document: SessionChoiceDocument;
  previewDocument: SessionChoiceDocument;
  previewEpoch: Readonly<{ current: number }>;
  host: string;
  revision: string;
  canEdit: boolean;
  onChange: (document: SessionChoiceDocument) => void;
}) {
  const agents = useAvailableAgents();
  const hosts = useHosts();
  const catalogs = useQuery({
    queryKey: ["calling-defaults-catalogs"],
    queryFn: listCallingDefaultCatalogs,
    staleTime: 60_000,
    retry: false,
  });
  const [status, setStatus] = useState("");
  const [editing, setEditing] = useState<string | null>(null);
  const rules = document.limitRules ?? [];
  const errors = limitRuleErrors(document);
  return (
    <details id="session-choice-limits" className="border-t border-border pt-3" open>
      <summary className="cursor-pointer text-sm font-medium">Limits</summary>
      <div className="mt-3 space-y-3">
        <p className="text-sm text-muted-foreground">
          More specific rules win per field. Ties use list order. Unset fields inherit.
        </p>
        {!canEdit && (
          <p className="text-sm text-muted-foreground">
            This server cannot edit scoped rules. Existing settings are preserved.
          </p>
        )}
        <ChoiceDragContext
          reset={document}
          onDrop={(intent) => {
            const from = intent.source.data;
            const to = intent.target.data;
            const source = from.kind === "rule" && rules.find((rule) => rule.id === from.rule);
            const target = to.kind === "rule" && rules.find((rule) => rule.id === to.rule);
            if (!canEdit || !source || !target)
              return { focus: intent.source.id, message: "Move cancelled." };
            const next = insertAtAnchor(rules, source, target, intent.edge);
            if (next.every((rule, index) => rule === rules[index]))
              return { focus: intent.source.id, message: "Position unchanged." };
            onChange(replaceLimitRules(document, next));
            return {
              focus: intent.source.id,
              message: `Moved rule to position ${next.indexOf(source) + 1}. Not saved yet.`,
            };
          }}
        >
          <fieldset disabled={!canEdit} className="min-w-0 space-y-3">
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                onChange(
                  replaceLimitRules(document, [
                    ...rules,
                    { id: crypto.randomUUID(), agent: agents.data?.[0]?.id ?? "" },
                  ]),
                );
                setStatus("Added rule. Not saved yet.");
              }}
            >
              Add rule
            </Button>
            {!rules.length && (
              <p className="text-sm text-muted-foreground">
                No scoped rules. No additional restriction from this policy; remaining legacy limits
                still apply.
              </p>
            )}
            <SortableContext
              items={rules.map((rule) => `rule:${rule.id}`)}
              strategy={verticalListSortingStrategy}
            >
              {rules.map((rule, index) => (
                <ChoiceDraggable
                  key={rule.id}
                  id={`rule:${rule.id}`}
                  data={{ kind: "rule", rule: rule.id }}
                  label={`rule ${index + 1}`}
                  disabled={!canEdit}
                  className="border border-border p-3"
                >
                  <article aria-label={`Rule ${index + 1}`} className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <button
                        type="button"
                        className="text-left text-sm font-medium"
                        aria-expanded={editing === rule.id}
                        onClick={() => setEditing(rule.id)}
                      >
                        Rule {index + 1} · Specificity{" "}
                        {1 + Number(Boolean(rule.host)) + Number(Boolean(rule.model))}
                      </button>
                      {([-1, 1] as const).map((offset) => (
                        <Button
                          key={offset}
                          size="sm"
                          variant="ghost"
                          aria-label={`Move rule ${index + 1} ${offset < 0 ? "up" : "down"}`}
                          disabled={offset < 0 ? index === 0 : index === rules.length - 1}
                          onClick={() => {
                            onChange(moveLimitRule(document, rule.id, offset));
                            setStatus(
                              `Moved rule ${index + 1} to position ${index + offset + 1}. Not saved yet.`,
                            );
                          }}
                        >
                          {offset < 0 ? "Move up" : "Move down"}
                        </Button>
                      ))}
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => {
                          onChange(
                            replaceLimitRules(
                              document,
                              rules.filter((item) => item.id !== rule.id),
                            ),
                          );
                          setStatus(`Removed rule ${index + 1}. Not saved yet.`);
                        }}
                      >
                        Remove rule {index + 1}
                      </Button>
                    </div>
                    <p className="mt-1 break-words text-sm text-muted-foreground">
                      {agents.data?.find((item) => item.id === rule.agent)?.display_name ??
                        rule.agent}{" "}
                      / {rule.host ?? "All hosts"} / {rule.model ?? "All models"}
                    </p>
                    <p className="mt-1 text-sm text-muted-foreground">
                      Max running:{" "}
                      {rule.maxRunning === undefined
                        ? "Inherit"
                        : rule.maxRunning === null
                          ? "Unlimited"
                          : rule.maxRunning}{" "}
                      · Warning:{" "}
                      {rule.reserve === null || rule.reserve?.pct === null
                        ? "No threshold"
                        : rule.reserve?.pct === undefined
                          ? "Inherit"
                          : `${rule.reserve.pct}%`}{" "}
                      · Stop:{" "}
                      {rule.reserve === null || rule.reserve?.stopPct === null
                        ? "No threshold"
                        : rule.reserve?.stopPct === undefined
                          ? "Inherit"
                          : `${rule.reserve.stopPct}%`}
                    </p>
                    <details open={editing === rule.id}>
                      <summary
                        className="mt-2 cursor-pointer text-sm"
                        onClick={(event) => {
                          event.preventDefault();
                          setEditing(editing === rule.id ? null : rule.id);
                        }}
                      >
                        Edit rule {index + 1}
                      </summary>
                      <RuleFields
                        rule={rule}
                        document={document}
                        catalogs={catalogs.data ?? []}
                        onChange={(next) =>
                          onChange(
                            replaceLimitRules(
                              document,
                              rules.map((item) => (item.id === rule.id ? next : item)),
                            ),
                          )
                        }
                      />
                    </details>
                  </article>
                </ChoiceDraggable>
              ))}
            </SortableContext>
          </fieldset>
        </ChoiceDragContext>
        {status && (
          <p role="status" className="text-sm text-muted-foreground">
            {status}
          </p>
        )}
        {errors.length > 0 && (
          <p role="alert" className="text-sm text-destructive">
            {errors.join(" ")}
          </p>
        )}
        {canEdit && (
          <ChoiceCandidateCheck
            document={previewDocument}
            previewEpoch={previewEpoch}
            hosts={hosts.data ?? []}
            agents={agents.data ?? []}
            host={host}
            revision={revision}
          />
        )}
      </div>
    </details>
  );
}
