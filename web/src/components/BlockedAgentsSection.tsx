import { PlusIcon, XIcon } from "lucide-react";
import { useMemo, useState } from "react";
import { AgentNameTag, nonNativeAgentKind } from "@/components/AgentNameTag";
import { HelpTip } from "@/components/HelpTip";
import { Button } from "@/components/ui/button";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { COMBOBOX_OPTION_THRESHOLD } from "@/components/ui/combobox";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import { isSdkAgent } from "@/lib/agentGrouping";
import { isNativeCodingAgent } from "@/lib/nativeCodingAgents";

export type BlockedAddDisabledReason = "host-unreported" | "host-offline" | "no-online-hosts";

export interface BlockedAgentsSectionProps {
  /** Host id or `"*"` for All hosts. */
  hostKey: string;
  /** Blocked agent ids stored directly on this key. */
  ownBlocked: readonly string[];
  /** Blocked agent ids inherited from `"*"` (non-removable here). */
  inheritedBlocked?: readonly string[];
  /** Full discovered catalog, used to resolve stored ids to name/badge/kind. */
  allAgents: readonly AvailableAgent[];
  /** Agents selectable to add right now (already filtered for usability/default/blocked). */
  addCandidates: readonly AvailableAgent[];
  /** Check whether a stored agent is currently installed/ready on this host. */
  isAgentInstalled?: (agent: AvailableAgent) => boolean;
  /** Non-null when adding is disabled (e.g. host offline). */
  addDisabledReason?: BlockedAddDisabledReason | null;
  disabled?: boolean;
  onAdd: (agentId: string) => void;
  onRemove: (agentId: string) => void;
  onDropdownOpenChange?: (open: boolean) => void;
}

const EMPTY_INHERITED_BLOCKED: readonly string[] = [];

export function BlockedAgentsSection({
  hostKey,
  ownBlocked,
  inheritedBlocked = EMPTY_INHERITED_BLOCKED,
  allAgents,
  addCandidates,
  isAgentInstalled,
  addDisabledReason = null,
  disabled = false,
  onAdd,
  onRemove,
  onDropdownOpenChange,
}: BlockedAgentsSectionProps) {
  const [pickerOpen, setPickerOpen] = useState(false);
  const [search, setSearch] = useState("");
  const agentsById = useMemo(
    () => new Map(allAgents.map((agent) => [agent.id, agent] as const)),
    [allAgents],
  );
  const inheritedSet = useMemo(() => new Set(inheritedBlocked), [inheritedBlocked]);
  const ownSet = useMemo(() => new Set(ownBlocked), [ownBlocked]);
  const inheritedOnly = useMemo(
    () => inheritedBlocked.filter((id) => !ownSet.has(id)),
    [inheritedBlocked, ownSet],
  );

  const searchable = addCandidates.length > COMBOBOX_OPTION_THRESHOLD;
  const query = search.trim().toLocaleLowerCase();
  const filteredCandidates = useMemo(() => {
    if (!query) return addCandidates;
    return addCandidates.filter((agent) => {
      const kind = nonNativeAgentKind(agent.harness) ?? "";
      return `${agent.display_name} ${agent.name} ${kind}`.toLocaleLowerCase().includes(query);
    });
  }, [addCandidates, query]);

  const grouped = useMemo(() => {
    const harnesses = filteredCandidates.filter(isNativeCodingAgent);
    const sdks = filteredCandidates.filter(
      (agent) => !isNativeCodingAgent(agent) && isSdkAgent(agent),
    );
    const others = filteredCandidates.filter(
      (agent) => !isNativeCodingAgent(agent) && !isSdkAgent(agent),
    );
    return { harnesses, sdks, others };
  }, [filteredCandidates]);

  const setOpen = (next: boolean) => {
    setPickerOpen(next);
    if (!next) setSearch("");
    onDropdownOpenChange?.(next);
  };

  const renderChipContent = (agentId: string) => {
    const agent = agentsById.get(agentId);
    if (!agent) {
      return <span className="truncate font-mono text-xs">{agentId}</span>;
    }
    const notInstalled = isAgentInstalled ? !isAgentInstalled(agent) : false;
    return (
      <>
        <AgentNameTag
          agentId={agent.id}
          name={agent.display_name}
          harness={agent.harness}
          kindTag={nonNativeAgentKind(agent.harness)}
        />
        {notInstalled && (
          <span
            className="shrink-0 text-xs text-muted-foreground"
            data-testid={`project-settings-blocked-uninstalled-${hostKey}-${agentId}`}
          >
            Not installed
          </span>
        )}
      </>
    );
  };

  const renderCandidateOption = (agent: AvailableAgent) => (
    <CommandItem
      key={agent.id}
      value={agent.id}
      data-testid={`project-settings-blocked-option-${hostKey}-${agent.id}`}
      onSelect={() => {
        onAdd(agent.id);
        setOpen(false);
      }}
    >
      <AgentNameTag
        agentId={agent.id}
        name={agent.display_name}
        harness={agent.harness}
        kindTag={nonNativeAgentKind(agent.harness)}
      />
    </CommandItem>
  );

  const canAdd = !disabled && !addDisabledReason && addCandidates.length > 0;

  return (
    <div
      className="flex min-w-0 flex-col gap-2 border-t pt-3"
      data-testid={`project-settings-blocked-section-${hostKey}`}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="flex items-center gap-1.5 text-ui font-medium">
          <span>Blocked agents</span>
          <HelpTip label="About blocked agents">
            Blocked agents cannot be opened for this project on this host. Existing sessions are
            unaffected.
          </HelpTip>
        </span>
        <div className="flex flex-wrap items-center gap-2">
          {addDisabledReason && (
            <span
              className="text-xs text-muted-foreground"
              data-testid={`project-settings-blocked-disabled-reason-${hostKey}`}
            >
              {addDisabledReason === "host-unreported"
                ? "Host not reported"
                : addDisabledReason === "host-offline"
                  ? "Host offline"
                  : "No online hosts"}
            </span>
          )}
          <Popover open={pickerOpen} onOpenChange={setOpen}>
            <PopoverTrigger asChild>
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={!canAdd}
                data-testid={`project-settings-blocked-add-${hostKey}`}
              >
                <PlusIcon className="size-3.5" aria-hidden="true" />
                Add
              </Button>
            </PopoverTrigger>
            <PopoverContent align="end" className="w-72 max-w-[calc(100vw-2rem)] p-0">
              <Command shouldFilter={false}>
                {searchable && (
                  <CommandInput
                    value={search}
                    onValueChange={setSearch}
                    placeholder="Search agents…"
                    aria-label="Search agents"
                  />
                )}
                <CommandList className="max-h-64">
                  {filteredCandidates.length === 0 ? (
                    <CommandEmpty>No matching agents</CommandEmpty>
                  ) : (
                    <>
                      {grouped.harnesses.length > 0 && (
                        <CommandGroup heading="Harnesses">
                          {grouped.harnesses.map(renderCandidateOption)}
                        </CommandGroup>
                      )}
                      {grouped.sdks.length > 0 && (
                        <CommandGroup heading="SDK">
                          {grouped.sdks.map(renderCandidateOption)}
                        </CommandGroup>
                      )}
                      {grouped.others.length > 0 && (
                        <CommandGroup heading="Agents">
                          {grouped.others.map(renderCandidateOption)}
                        </CommandGroup>
                      )}
                    </>
                  )}
                </CommandList>
              </Command>
            </PopoverContent>
          </Popover>
        </div>
      </div>
      {inheritedOnly.length === 0 && ownBlocked.length === 0 ? (
        <p className="text-xs text-muted-foreground">No agents blocked</p>
      ) : (
        <div
          className="flex min-w-0 flex-wrap gap-1.5"
          data-testid={`project-settings-blocked-chips-${hostKey}`}
        >
          {inheritedOnly.map((agentId) => (
            <span
              key={`inherited-${agentId}`}
              className="inline-flex max-w-full items-center gap-1.5 rounded-md border bg-muted/60 px-2 py-1 text-xs"
              data-testid={`project-settings-blocked-inherited-${hostKey}-${agentId}`}
            >
              {renderChipContent(agentId)}
              <span className="shrink-0 text-muted-foreground">(All hosts)</span>
            </span>
          ))}
          {ownBlocked.map((agentId) => (
            <span
              key={agentId}
              className="inline-flex max-w-full items-center gap-1.5 rounded-md border bg-background px-2 py-1 text-xs"
              data-testid={`project-settings-blocked-chip-${hostKey}-${agentId}`}
            >
              {renderChipContent(agentId)}
              {inheritedSet.has(agentId) && (
                <span className="shrink-0 text-muted-foreground">(All hosts)</span>
              )}
              <button
                type="button"
                className="inline-flex size-4 shrink-0 items-center justify-center rounded text-muted-foreground hover:text-foreground disabled:opacity-50"
                aria-label="Remove blocked agent"
                disabled={disabled}
                onClick={() => onRemove(agentId)}
                data-testid={`project-settings-blocked-remove-${hostKey}-${agentId}`}
              >
                <XIcon className="size-3" aria-hidden="true" />
              </button>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
