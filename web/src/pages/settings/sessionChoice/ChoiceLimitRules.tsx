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
import { SESSION_CHOICE_ENTRY } from "../search/catalog/general";
import { settingsControlProps } from "../search/settingsControl";
import {
  SettingsOwner,
  settingsOverlayProps,
  settingsRuntimeChoiceProps,
} from "../search/settingsOwnership";
import { CHOICE_SELECT } from "./ChoiceLinkEditor";
import { ChoiceCandidateCheck } from "./ChoiceCandidateCheck";
import { limitRuleErrors, moveLimitRule, replaceLimitRules } from "./choiceLimitEdits";
import type { ChoiceLimitRule, ChoiceLimitScope, SessionChoiceDocument } from "./sessionChoiceData";

function isGroupRule(rule: ChoiceLimitRule): boolean {
  return Array.isArray(rule.members);
}

function ScopeFields({
  scope,
  catalogs,
  labelPrefix = "Rule",
  onChange,
}: {
  scope: ChoiceLimitScope;
  catalogs: CallingDefaultsCatalogRow[];
  labelPrefix?: string;
  onChange: (scope: ChoiceLimitScope) => void;
}) {
  const listId = useId();
  const agents = useAvailableAgents();
  const hosts = useHosts();
  const agent = agents.data?.find((item) => item.id === scope.agent);
  const agentOptions = agents.data ?? [];
  const hostOptions = hosts.data ?? [];
  const models = [
    ...new Set(
      catalogs
        .filter(
          (row) =>
            (!scope.host || row.host_id === scope.host) &&
            agent?.harness &&
            canonicalNativeHarness(row.harness.replaceAll("_", "-")) ===
              canonicalNativeHarness(agent.harness.replaceAll("_", "-")),
        )
        .flatMap((row) => row.models.map((model) => model.id)),
    ),
  ];
  return (
    <div className="grid gap-3 sm:grid-cols-3">
      <label className="grid min-w-0 gap-1 text-sm">
        Agent
        {agentOptions.length > COMBOBOX_OPTION_THRESHOLD ? (
          <SettingsOwner entry={SESSION_CHOICE_ENTRY} part="scopeAgent">
            <Combobox
              mode="select"
              options={[
                ...(scope.agent && !agent
                  ? [{ key: scope.agent, label: `${scope.agent} (unavailable)` }]
                  : []),
                ...agentOptions.map((item) => ({
                  key: item.id,
                  label: item.display_name,
                  description: item.name,
                })),
              ]}
              value={scope.agent}
              onValueChange={(value) => onChange({ ...scope, agent: value })}
              placeholder="Choose an agent"
              aria-label={`${labelPrefix} agent`}
              settingsRuntimeName={labelPrefix}
              className="h-auto w-full rounded-none! bg-transparent p-0"
            />
          </SettingsOwner>
        ) : (
          <select
            {...settingsControlProps(SESSION_CHOICE_ENTRY, "scopeAgent", {
              runtime: labelPrefix,
            })}
            className={CHOICE_SELECT}
            value={scope.agent}
            onChange={(e) => onChange({ ...scope, agent: e.target.value })}
          >
            <option value="" disabled>
              Choose an agent
            </option>
            {scope.agent && !agent && (
              <option value={scope.agent}>{scope.agent} (unavailable)</option>
            )}
            {agentOptions.map((item) => (
              <option key={item.id} value={item.id}>
                {item.display_name}
              </option>
            ))}
          </select>
        )}
        <span className="break-words text-muted-foreground">
          {agent?.display_name ?? scope.agent}
        </span>
      </label>
      <label className="grid min-w-0 gap-1 text-sm">
        Host
        {hostOptions.length > COMBOBOX_OPTION_THRESHOLD ? (
          <SettingsOwner entry={SESSION_CHOICE_ENTRY} part="scopeHost">
            <Combobox
              mode="select"
              options={[
                { key: "__all__", label: "All hosts" },
                ...(scope.host && !hostOptions.some((item) => item.host_id === scope.host)
                  ? [{ key: scope.host, label: `${scope.host} (unavailable)` }]
                  : []),
                ...hostOptions.map((item) => ({
                  key: item.host_id,
                  label: item.name,
                  description: item.status === "online" ? undefined : "Offline",
                })),
              ]}
              value={scope.host ?? "__all__"}
              onValueChange={(value) => {
                const next = { ...scope };
                if (value !== "__all__") next.host = value;
                else Reflect.deleteProperty(next, "host");
                onChange(next);
              }}
              aria-label={`${labelPrefix} host`}
              settingsRuntimeName={labelPrefix}
              className="h-auto w-full rounded-none! bg-transparent p-0"
            />
          </SettingsOwner>
        ) : (
          <select
            {...settingsControlProps(SESSION_CHOICE_ENTRY, "scopeHost", {
              runtime: labelPrefix,
            })}
            className={CHOICE_SELECT}
            value={scope.host ?? ""}
            onChange={(e) => {
              const next = { ...scope };
              if (e.target.value) next.host = e.target.value;
              else Reflect.deleteProperty(next, "host");
              onChange(next);
            }}
          >
            <option value="">All hosts</option>
            {scope.host && !hostOptions.some((item) => item.host_id === scope.host) && (
              <option value={scope.host}>{scope.host} (unavailable)</option>
            )}
            {hostOptions.map((item) => (
              <option key={item.host_id} value={item.host_id}>
                {item.name}
              </option>
            ))}
          </select>
        )}
        <span className="break-words text-muted-foreground">
          {hosts.data?.find((item) => item.host_id === scope.host)?.name ??
            scope.host ??
            "All hosts"}
        </span>
      </label>
      <label className="grid min-w-0 gap-1 text-sm">
        Model
        <Input
          {...settingsControlProps(SESSION_CHOICE_ENTRY, "scopeModel", {
            runtime: labelPrefix,
          })}
          list={listId}
          placeholder="All models"
          value={scope.model ?? ""}
          onChange={(e) => {
            const next = { ...scope };
            if (e.target.value) next.model = e.target.value;
            else Reflect.deleteProperty(next, "model");
            onChange(next);
          }}
        />
        <datalist id={listId} {...settingsOverlayProps(SESSION_CHOICE_ENTRY, "scopeModel")}>
          {models.map((model) => (
            <option
              key={model}
              value={model}
              {...settingsRuntimeChoiceProps(SESSION_CHOICE_ENTRY, "scopeModel")}
            />
          ))}
        </datalist>
        <span className="break-words text-muted-foreground">{scope.model ?? "All models"}</span>
      </label>
    </div>
  );
}

function GroupRuleFields({
  rule,
  catalogs,
  onChange,
}: {
  rule: ChoiceLimitRule;
  catalogs: CallingDefaultsCatalogRow[];
  onChange: (rule: ChoiceLimitRule) => void;
}) {
  const agents = useAvailableAgents();
  const members = rule.members ?? [];
  const hasReserveFields = rule.reserve !== undefined || rule.reserveExempt !== undefined;
  function updateMembers(nextMembers: ChoiceLimitScope[]) {
    onChange({ ...rule, members: nextMembers });
  }
  function clearReserveFields() {
    const next = { ...rule };
    Reflect.deleteProperty(next, "reserve");
    Reflect.deleteProperty(next, "reserveExempt");
    onChange(next);
  }
  return (
    <div className="space-y-4 pt-3">
      {hasReserveFields && (
        <div className="flex flex-wrap items-center justify-between gap-2 rounded border border-border p-2 text-xs text-muted-foreground">
          <span>Clear reserve fields before adding another member.</span>
          <Button
            {...settingsControlProps(SESSION_CHOICE_ENTRY, "clearReserve")}
            size="sm"
            variant="outline"
            onClick={clearReserveFields}
          >
            Clear reserve fields
          </Button>
        </div>
      )}
      <div className="space-y-3">
        {members.map((member, memberIndex) => (
          // eslint-disable-next-line react/no-array-index-key -- Member rows are ordered slots in a small controlled list.
          <div key={`member:${memberIndex}`} className="space-y-2 rounded border border-border p-2">
            <div className="flex items-center justify-between gap-2">
              <span className="text-xs font-medium text-muted-foreground">
                Member {memberIndex + 1}
              </span>
              {members.length > 1 && (
                <Button
                  {...settingsControlProps(SESSION_CHOICE_ENTRY, "removeMember", {
                    runtime: String(memberIndex + 1),
                  })}
                  size="sm"
                  variant="ghost"
                  onClick={() => updateMembers(members.filter((_, idx) => idx !== memberIndex))}
                >
                  Remove member {memberIndex + 1}
                </Button>
              )}
            </div>
            <ScopeFields
              scope={member}
              catalogs={catalogs}
              labelPrefix={`Member ${memberIndex + 1}`}
              onChange={(nextMember) =>
                updateMembers(members.map((item, idx) => (idx === memberIndex ? nextMember : item)))
              }
            />
          </div>
        ))}
        <Button
          {...settingsControlProps(SESSION_CHOICE_ENTRY, "addMember")}
          size="sm"
          variant="outline"
          disabled={hasReserveFields}
          onClick={() => updateMembers([...members, { agent: agents.data?.[0]?.id ?? "" }])}
        >
          + Add member
        </Button>
      </div>
      <label className="flex flex-wrap items-center gap-2 text-sm">
        Max running
        <Input
          {...settingsControlProps(SESSION_CHOICE_ENTRY, "groupMaximum")}
          type="number"
          className="h-9 w-28"
          min={1}
          step={1}
          value={Number.isNaN(rule.maxRunning) ? "" : (rule.maxRunning ?? "")}
          onChange={(event) => {
            const next = { ...rule };
            if (event.target.value === "") Reflect.deleteProperty(next, "maxRunning");
            else next.maxRunning = Number(event.target.value);
            onChange(next);
          }}
        />
        sessions
      </label>
    </div>
  );
}

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
  if (isGroupRule(rule)) {
    return <GroupRuleFields rule={rule} catalogs={catalogs} onChange={onChange} />;
  }
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
      <ScopeFields
        scope={{ agent: rule.agent ?? "", host: rule.host, model: rule.model }}
        catalogs={catalogs}
        onChange={(scope) => {
          const next = { ...rule, agent: scope.agent };
          if (scope.host !== undefined) next.host = scope.host;
          else Reflect.deleteProperty(next, "host");
          if (scope.model !== undefined) next.model = scope.model;
          else Reflect.deleteProperty(next, "model");
          onChange(next);
        }}
      />
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
          Single-scope rules: more specific wins per field. Group caps sum across their members;
          every matching cap applies and the tightest decides.
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
            <div className="flex flex-wrap gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={() => {
                  onChange(
                    replaceLimitRules(document, [
                      ...rules,
                      {
                        id: crypto.randomUUID().replaceAll("-", ""),
                        agent: agents.data?.[0]?.id ?? "",
                      },
                    ]),
                  );
                  setStatus("Added rule. Not saved yet.");
                }}
              >
                Add rule
              </Button>
              <Button
                {...settingsControlProps(SESSION_CHOICE_ENTRY, "addGroup")}
                variant="outline"
                size="sm"
                onClick={() => {
                  const id = crypto.randomUUID().replaceAll("-", "");
                  onChange(
                    replaceLimitRules(document, [
                      ...rules,
                      {
                        id,
                        members: [{ agent: agents.data?.[0]?.id ?? "" }],
                        maxRunning: 1,
                      },
                    ]),
                  );
                  setEditing(id);
                  setStatus("Added group cap. Not saved yet.");
                }}
              >
                Add group cap
              </Button>
            </div>
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
                        {isGroupRule(rule)
                          ? rule.members!.length === 1
                            ? `Rule ${index + 1} · Group cap (1 member)`
                            : `Rule ${index + 1} · Group cap (${rule.members!.length} members)`
                          : `Rule ${index + 1} · Specificity ${1 + Number(Boolean(rule.host)) + Number(Boolean(rule.model))}`}
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
                      {isGroupRule(rule)
                        ? rule
                            .members!.map((member) => {
                              const agentName =
                                agents.data?.find((item) => item.id === member.agent)
                                  ?.display_name ?? member.agent;
                              const hostName =
                                hosts.data?.find((item) => item.host_id === member.host)?.name ??
                                member.host ??
                                "All hosts";
                              return `${agentName} / ${hostName} / ${member.model ?? "All models"}`;
                            })
                            .join(" + ")
                        : `${agents.data?.find((item) => item.id === rule.agent)?.display_name ?? rule.agent} / ${hosts.data?.find((item) => item.host_id === rule.host)?.name ?? rule.host ?? "All hosts"} / ${rule.model ?? "All models"}`}
                    </p>
                    <p className="mt-1 text-sm text-muted-foreground">
                      {isGroupRule(rule) ? (
                        <>
                          Max running: {rule.maxRunning ?? "Unlimited"} · Every matching cap applies
                        </>
                      ) : (
                        <>
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
                        </>
                      )}
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
