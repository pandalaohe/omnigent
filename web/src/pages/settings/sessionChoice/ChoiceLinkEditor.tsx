import { useId, useState } from "react";
import { XIcon } from "lucide-react";
import { RadioGroup } from "radix-ui";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Combobox, COMBOBOX_OPTION_THRESHOLD } from "@/components/ui/combobox";
import { Input } from "@/components/ui/input";
import { useHostModelOptions, type Host } from "@/hooks/useHosts";
import type { UsageAccountView } from "@/pages/usageBoard/usageBoardData";
import type {
  ChoiceFallbackMode,
  ChoiceLink,
  ChoiceLimit,
  ChoicePermissionTier,
} from "./sessionChoiceData";
import type { ChoiceDrainTag } from "./choiceDrainLocks";
import { guessMeter, type ChoiceAgent } from "./sessionChoiceEdits";
import { canonicalNativeHarness } from "@/lib/nativeHarnessModes";
import { splitModelId } from "@/lib/modelReference";

export const CHOICE_SELECT =
  "h-8 min-w-0 rounded-md border border-input bg-background px-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";

export function ChoiceLinkEditor({
  link,
  agents,
  host,
  meters,
  limits,
  onChange,
  onRemove,
  removable,
  meterPicked,
  onPickMeter,
}: {
  link: ChoiceLink;
  agents: ChoiceAgent[];
  host: Host | undefined;
  meters: UsageAccountView[];
  limits?: Record<string, ChoiceLimit>;
  onChange: (link: ChoiceLink) => void;
  onRemove: () => void;
  removable: boolean;
  meterPicked: boolean;
  onPickMeter: () => void;
}) {
  const listId = useId();
  const agent = agents.find((a) => a.id === link.agent);
  const usableAgents = agents.filter(
    (item) =>
      item.harness &&
      host?.configured_harnesses?.[canonicalNativeHarness(item.harness.replaceAll("_", "-"))] ===
        true,
  );
  const agentLabel = (item: ChoiceAgent) =>
    agents.filter((other) => other.display_name === item.display_name).length > 1
      ? `${item.display_name} · ${item.harness ?? item.name} · ${item.name === item.display_name || agents.some((other) => other.id !== item.id && other.name === item.name && other.harness === item.harness) ? item.id : item.name}`
      : item.display_name;
  const catalog = useHostModelOptions(
    host?.host_id ?? null,
    agent?.harness ?? "",
    Boolean(agent?.harness),
    { poll: false },
  );
  const options = catalog.data ?? [];
  const identity = splitModelId(link.model ?? "");
  const variants = [
    ...new Map(
      options
        .filter((item) => splitModelId(item.id).model === identity.model)
        .map((item) => [item.id, item]),
    ).values(),
  ];
  const model =
    options.find((m) => m.id === link.model) ?? (variants.length === 1 ? variants[0] : undefined);
  const offered = options.some((item) => item.id === link.model);
  const displayedModel = offered ? identity.model : (link.model ?? "");
  const efforts =
    model?.supportedReasoningEfforts?.map((e) => e.reasoningEffort) ?? model?.efforts ?? [];
  function changeIdentity(patch: Partial<ChoiceLink>) {
    const next = { ...link, ...patch };
    if (!meterPicked) next.meter = guessMeter(next, agents, meters);
    if (next.meter) delete next.maxRunning;
    onChange(next);
  }
  return (
    <div className="space-y-2 rounded-md border border-border/70 p-2">
      <div className="flex items-end gap-2">
        <label className="flex min-w-0 flex-1 flex-col gap-1 text-xs">
          Agent
          {usableAgents.length > COMBOBOX_OPTION_THRESHOLD ? (
            <Combobox
              mode="select"
              options={[
                ...(link.agent && !usableAgents.some((item) => item.id === link.agent)
                  ? [
                      {
                        key: link.agent,
                        label: `${agent ? agentLabel(agent) : link.agent} (saved; not ready)`,
                        disabled: true,
                      },
                    ]
                  : []),
                ...usableAgents.map((item) => ({ key: item.id, label: agentLabel(item) })),
              ]}
              value={link.agent}
              onValueChange={(value) => changeIdentity({ agent: value, model: null, effort: null })}
              placeholder="Choose an agent"
              aria-label="Link agent"
              className="h-auto w-full rounded-none! bg-transparent p-0"
            />
          ) : (
            <select
              aria-label="Link agent"
              className={CHOICE_SELECT}
              value={link.agent}
              onChange={(e) => changeIdentity({ agent: e.target.value, model: null, effort: null })}
            >
              <option value="" disabled>
                Choose an agent
              </option>
              {link.agent && !usableAgents.some((item) => item.id === link.agent) && (
                <option value={link.agent} disabled hidden>
                  {agent ? agentLabel(agent) : link.agent} (saved; not ready)
                </option>
              )}
              {usableAgents.map((a) => (
                <option key={a.id} value={a.id}>
                  {agentLabel(a)}
                </option>
              ))}
            </select>
          )}
        </label>
        {removable && (
          <Button
            aria-label="Remove link"
            size="icon"
            variant="ghost"
            className="size-8 shrink-0"
            onClick={onRemove}
          >
            <XIcon className="size-4" />
          </Button>
        )}
      </div>
      <div className="grid grid-cols-[minmax(0,1fr)_minmax(80px,0.55fr)] gap-2">
        <label className="flex min-w-0 flex-col gap-1 text-xs">
          Model
          <Input
            aria-label="Link model"
            className="h-8"
            value={displayedModel}
            placeholder="Agent default"
            list={`${listId}-models`}
            onChange={(e) => {
              const value = e.target.value;
              const exact = options.find((item) => item.id === value);
              const matches = options.filter((item) => splitModelId(item.id).model === value);
              const chosen =
                exact ??
                matches.find((item) => splitModelId(item.id).provider === identity.provider) ??
                matches[0];
              changeIdentity({ model: chosen?.id ?? (value || null), effort: null });
            }}
          />
          <datalist id={`${listId}-models`}>
            {[...new Set(options.map((item) => splitModelId(item.id).model))].map((name) => (
              <option key={name} value={name} />
            ))}
          </datalist>
        </label>
        <label className="flex min-w-0 flex-col gap-1 text-xs">
          Effort
          <Input
            aria-label="Link effort"
            className="h-8"
            placeholder="Default"
            value={link.effort ?? ""}
            list={`${listId}-efforts`}
            onChange={(e) => onChange({ ...link, effort: e.target.value || null })}
          />
          <datalist id={`${listId}-efforts`}>
            {efforts.map((e) => (
              <option key={e} value={e} />
            ))}
          </datalist>
        </label>
      </div>
      <label className="flex min-w-0 flex-col gap-1 text-xs">
        Permission tier
        <select
          aria-label="Link permission tier"
          className={CHOICE_SELECT}
          value={link.permissionTier ?? ""}
          onChange={(e) => {
            const next = { ...link };
            if (e.target.value) next.permissionTier = e.target.value as ChoicePermissionTier;
            else delete next.permissionTier;
            onChange(next);
          }}
        >
          <option value="">Host default</option>
          <option value="ask">Ask</option>
          <option value="plan">Plan</option>
          <option value="accept-edits">Accept edits</option>
          <option value="auto">Auto</option>
        </select>
      </label>
      {variants.length > 1 && (
        <label className="flex min-w-0 flex-col gap-1 text-xs">
          Provider
          <select
            aria-label="Link provider"
            className={CHOICE_SELECT}
            value={link.model ?? ""}
            onChange={(event) => changeIdentity({ model: event.target.value, effort: null })}
          >
            {!offered && link.model && (
              <option value={link.model}>
                {identity.provider ?? "Saved provider"} (not offered here)
              </option>
            )}
            {variants.map((item) => (
              <option key={item.id} value={item.id}>
                {splitModelId(item.id).provider ?? "Default provider"}
              </option>
            ))}
          </select>
        </label>
      )}
      {link.model && !offered && variants.length === 1 && identity.provider && (
        <p className="text-xs text-muted-foreground">
          Saved provider {identity.provider} is not offered here. Select the model again to use the
          available provider.
        </p>
      )}
      {catalog.isError && (
        <p className="text-xs text-muted-foreground">
          Model list unavailable. You can type a model.
        </p>
      )}
      <div className="flex items-end gap-2">
        <label className="flex min-w-0 flex-1 flex-col gap-1 text-xs">
          Meter · quota counter
          {meters.length > COMBOBOX_OPTION_THRESHOLD ? (
            <Combobox
              mode="select"
              options={[
                { key: "__none__", label: "No meter" },
                ...(link.meter && !meters.some((meter) => meter.id === link.meter)
                  ? [{ key: link.meter, label: `${link.meter} (tile gone)` }]
                  : []),
                ...meters.map((meter) => ({ key: meter.id, label: meter.display_name })),
              ]}
              value={link.meter ?? "__none__"}
              onValueChange={(value) => {
                onPickMeter();
                const next = { ...link, meter: value === "__none__" ? null : value };
                if (next.meter) delete next.maxRunning;
                onChange(next);
              }}
              aria-label="Link meter"
              className="h-auto w-full rounded-none! bg-transparent p-0"
            />
          ) : (
            <select
              aria-label="Link meter"
              className={CHOICE_SELECT}
              value={link.meter ?? ""}
              onChange={(e) => {
                onPickMeter();
                const next = { ...link, meter: e.target.value || null };
                if (next.meter) delete next.maxRunning;
                onChange(next);
              }}
            >
              <option value="">No meter</option>
              {link.meter && !meters.some((m) => m.id === link.meter) && (
                <option value={link.meter}>{link.meter} (tile gone)</option>
              )}
              {meters.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.display_name}
                </option>
              ))}
            </select>
          )}
        </label>
        {!link.meter && (
          <label className="flex w-28 flex-col gap-1 text-xs">
            Max running
            <Input
              aria-label="Link max running"
              className="h-8"
              type="number"
              min={1}
              placeholder="No limit"
              value={link.maxRunning ?? ""}
              onChange={(e) =>
                onChange({
                  ...link,
                  maxRunning: e.target.value ? Math.max(1, Number(e.target.value)) : null,
                })
              }
            />
          </label>
        )}
      </div>
      <p className="text-xs text-muted-foreground">
        The quota counter this link spends from, used by Limits, warning/stop lines and quota
        display; it is separate from provider, model and effort.
      </p>
      {link.meter && (
        <p className="text-xs text-muted-foreground" role="note">
          Meter max running:{" "}
          {limits ? (limits[link.meter]?.maxRunning ?? "No pool limit") : "See Limits"}. Agent rules
          may also apply.{" "}
          <a className="underline" href="#session-choice-limits">
            Edit Limits
          </a>
        </p>
      )}
    </div>
  );
}

const FALLBACK_OPTIONS = [
  { mode: "drain", label: "Drain", hint: "Use this node up before going back" },
  { mode: "return", label: "Return", hint: "Go back as soon as an earlier node recovers" },
] as const;
const drainTime = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit" });

function FallbackModeControl({
  index,
  mode,
  since,
  onChange,
}: {
  index: number;
  mode: ChoiceFallbackMode;
  since?: number;
  onChange: (mode: ChoiceFallbackMode) => void;
}) {
  const id = useId();
  return (
    <div className="mb-1 space-y-1 text-xs">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span id={`${id}-label`} className="text-muted-foreground">
          Fallback {index}
        </span>
        <RadioGroup.Root
          aria-labelledby={`${id}-label`}
          aria-describedby={`${id}-hint`}
          orientation="horizontal"
          className="inline-flex gap-0.5 rounded-md bg-muted p-0.5"
          value={mode}
          onValueChange={(value: ChoiceFallbackMode) => onChange(value)}
        >
          {FALLBACK_OPTIONS.map(({ mode: option, label }) => (
            <RadioGroup.Item
              key={option}
              value={option}
              className="rounded px-2 py-1 text-muted-foreground outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring data-[state=checked]:bg-background data-[state=checked]:font-medium data-[state=checked]:text-foreground data-[state=checked]:shadow-xs"
            >
              {label}
            </RadioGroup.Item>
          ))}
        </RadioGroup.Root>
        {since !== undefined && (
          <Badge variant="secondary" className="text-xs tabular-nums">
            Draining since {drainTime.format(since * 1000)}
          </Badge>
        )}
      </div>
      <p id={`${id}-hint`} className="text-muted-foreground">
        {FALLBACK_OPTIONS.find((option) => option.mode === mode)?.hint}
      </p>
    </div>
  );
}

export function ChoiceChainEditor({
  links,
  drainTag,
  onChange,
  ...props
}: Omit<
  Parameters<typeof ChoiceLinkEditor>[0],
  "link" | "onChange" | "onRemove" | "removable" | "meterPicked" | "onPickMeter"
> & {
  links: ChoiceLink[];
  drainTag?: ChoiceDrainTag;
  onChange: (links: ChoiceLink[]) => void;
}) {
  const [manualMeters, setManualMeters] = useState(() =>
    links.map((link) => link.meter !== undefined),
  );
  return (
    <div className="space-y-2">
      {links.map((link, index) => (
        // eslint-disable-next-line react/no-array-index-key -- Chain slots are fully controlled and ordered by position.
        <div key={`link:${index}`}>
          {index > 0 && (
            <FallbackModeControl
              index={index}
              mode={link.fallbackMode ?? "return"}
              since={drainTag?.linkIndex === index ? drainTag.since : undefined}
              onChange={(mode) => {
                const next = { ...link };
                if (mode === "drain") next.fallbackMode = mode;
                else delete next.fallbackMode;
                onChange(links.map((item, i) => (i === index ? next : item)));
              }}
            />
          )}
          <ChoiceLinkEditor
            {...props}
            link={link}
            removable={links.length > 1}
            meterPicked={manualMeters[index] ?? false}
            onPickMeter={() =>
              setManualMeters((values) => links.map((_, i) => i === index || values[i]))
            }
            onRemove={() => {
              setManualMeters((values) => values.filter((_, i) => i !== index));
              onChange(links.filter((_, i) => i !== index));
            }}
            onChange={(next) => onChange(links.map((item, i) => (i === index ? next : item)))}
          />
        </div>
      ))}
      <Button
        variant="outline"
        size="sm"
        onClick={() => {
          setManualMeters((values) => [...values, false]);
          onChange([...links, { agent: "" }]);
        }}
      >
        + Fallback
      </Button>
    </div>
  );
}
