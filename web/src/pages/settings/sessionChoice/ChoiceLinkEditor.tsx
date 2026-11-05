import { useId, useState } from "react";
import { XIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useHostModelOptions, type Host } from "@/hooks/useHosts";
import type { UsageAccountView } from "@/pages/usageBoard/usageBoardData";
import type { ChoiceLink } from "./sessionChoiceData";
import { agentReady, guessMeter, type ChoiceAgent } from "./sessionChoiceEdits";

export const CHOICE_SELECT =
  "h-8 min-w-0 rounded-md border border-input bg-background px-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";

export function ChoiceLinkEditor({
  link,
  agents,
  host,
  meters,
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
  onChange: (link: ChoiceLink) => void;
  onRemove: () => void;
  removable: boolean;
  meterPicked: boolean;
  onPickMeter: () => void;
}) {
  const listId = useId();
  const agent = agents.find((a) => a.id === link.agent);
  const catalog = useHostModelOptions(
    host?.host_id ?? null,
    agent?.harness ?? "",
    Boolean(agent?.harness),
    { poll: false },
  );
  const options = catalog.data ?? [];
  const model = options.find((m) => m.id === link.model);
  const efforts =
    model?.supportedReasoningEfforts?.map((e) => e.reasoningEffort) ?? model?.efforts ?? [];
  function changeIdentity(patch: Partial<ChoiceLink>) {
    const next = { ...link, ...patch };
    if (!meterPicked) next.meter = guessMeter(next, agents, meters);
    onChange(next);
  }
  return (
    <div className="space-y-2 rounded-md border border-border/70 p-2">
      <div className="flex items-end gap-2">
        <label className="flex min-w-0 flex-1 flex-col gap-1 text-xs">
          Agent
          <select
            aria-label="Link agent"
            className={CHOICE_SELECT}
            value={link.agent}
            onChange={(e) => changeIdentity({ agent: e.target.value, model: null, effort: null })}
          >
            <option value="" disabled>
              Choose an agent
            </option>
            {link.agent && !agent && <option value={link.agent}>{link.agent} (unavailable)</option>}
            {agents.map((a) => (
              <option key={a.id} value={a.id}>
                {a.display_name}
                {agentReady(a, host) ? "" : " (unavailable on this host)"}
              </option>
            ))}
          </select>
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
            value={link.model ?? ""}
            placeholder="Agent default"
            list={`${listId}-models`}
            onChange={(e) => changeIdentity({ model: e.target.value || null, effort: null })}
          />
          <datalist id={`${listId}-models`}>
            {options.map((m) => (
              <option key={m.id} value={m.id}>
                {m.displayName ?? m.id}
              </option>
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
      {catalog.isError && (
        <p className="text-xs text-muted-foreground">
          Model list unavailable. You can type a model.
        </p>
      )}
      <div className="flex items-end gap-2">
        <label className="flex min-w-0 flex-1 flex-col gap-1 text-xs">
          Meter
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
    </div>
  );
}

export function ChoiceChainEditor({
  links,
  onChange,
  ...props
}: Omit<
  Parameters<typeof ChoiceLinkEditor>[0],
  "link" | "onChange" | "onRemove" | "removable" | "meterPicked" | "onPickMeter"
> & {
  links: ChoiceLink[];
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
          {index > 0 && <p className="mb-1 text-xs text-muted-foreground">Fallback {index}</p>}
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
