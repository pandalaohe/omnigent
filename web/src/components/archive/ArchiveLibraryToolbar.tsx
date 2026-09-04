import {
  CalendarRangeIcon,
  CheckIcon,
  ChevronDownIcon,
  ChevronsUpDownIcon,
  RotateCcwIcon,
  SearchIcon,
} from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Command,
  CommandEmpty,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { Input } from "@/components/ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type {
  ArchivedConversationFilters,
  ArchivedSearchScope,
  ArchivedSortField,
} from "@/hooks/useConversations";
import { cn } from "@/lib/utils";

export interface ArchiveLibraryViewState {
  searchQuery: string;
  searchScope: ArchivedSearchScope;
  project?: string;
  hostId?: string;
  agentName?: string;
  createdRange: string;
  archivedRange: string;
  sortField: ArchivedSortField;
  order: "asc" | "desc";
}

export interface ArchiveFilterOption {
  value: string;
  label: string;
  keywords?: string;
}

interface ArchiveLibraryToolbarProps {
  value: ArchiveLibraryViewState;
  projectOptions: ArchiveFilterOption[];
  hostOptions: ArchiveFilterOption[];
  agentOptions: ArchiveFilterOption[];
  onChange: (patch: Partial<ArchiveLibraryViewState>) => void;
  className?: string;
}

const DATE_RANGE_PATTERN = /^\d{6}-\d{6}$/;

export function parseArchiveDateRange(value: string): { after: number; before: number } | null {
  if (!DATE_RANGE_PATTERN.test(value)) return null;
  const parse = (part: string) => {
    const year = 2000 + Number.parseInt(part.slice(0, 2), 10);
    const month = Number.parseInt(part.slice(2, 4), 10) - 1;
    const day = Number.parseInt(part.slice(4, 6), 10);
    const date = new Date(year, month, day);
    if (date.getFullYear() !== year || date.getMonth() !== month || date.getDate() !== day) {
      return null;
    }
    return date;
  };
  const start = parse(value.slice(0, 6));
  const end = parse(value.slice(7));
  if (!start || !end || start > end) return null;
  const endExclusive = new Date(end);
  endExclusive.setDate(endExclusive.getDate() + 1);
  return {
    after: Math.floor(start.getTime() / 1000),
    before: Math.floor(endExclusive.getTime() / 1000),
  };
}

export function buildArchiveConversationFilters(
  value: ArchiveLibraryViewState,
  searchQuery: string,
): ArchivedConversationFilters {
  const created = parseArchiveDateRange(value.createdRange);
  const archived = parseArchiveDateRange(value.archivedRange);
  return {
    searchQuery,
    searchScope: value.searchScope,
    project: value.project,
    hostId: value.hostId,
    agentName: value.agentName,
    dateField: "archived_at",
    sortField: value.sortField,
    agePreset: "any",
    order: value.order,
    createdAfter: created?.after,
    createdBefore: created?.before,
    archivedAfter: archived?.after,
    archivedBefore: archived?.before,
  };
}

function FilterCombobox({
  label,
  allLabel,
  value,
  options,
  onChange,
}: {
  label: string;
  allLabel: string;
  value?: string;
  options: ArchiveFilterOption[];
  onChange: (value: string | undefined) => void;
}) {
  const [open, setOpen] = useState(false);
  const current = options.find((option) => option.value === value);
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <Tooltip>
        <TooltipTrigger asChild>
          <span className="min-w-0 flex-1">
            <PopoverTrigger asChild>
              <Button
                type="button"
                variant="outline"
                role="combobox"
                aria-label={`Filter archived sessions by ${label.toLowerCase()}`}
                aria-expanded={open}
                className={cn(
                  "h-8 w-full min-w-0 justify-between gap-1 px-2 text-xs font-normal",
                  value && "border-primary/40 bg-primary/5",
                )}
              >
                <span className="truncate">{current?.label ?? value ?? allLabel}</span>
                <ChevronsUpDownIcon className="size-3 shrink-0 text-muted-foreground" />
              </Button>
            </PopoverTrigger>
          </span>
        </TooltipTrigger>
        <TooltipContent>
          {label}: {current?.label ?? allLabel}. Open the list or type to fuzzy search; use ↑/↓ and
          Enter to confirm.
        </TooltipContent>
      </Tooltip>
      <PopoverContent align="start" className="w-(--radix-popover-trigger-width) min-w-48 p-0">
        <Command>
          <CommandInput placeholder={`Search ${label.toLowerCase()}…`} />
          <CommandList>
            <CommandEmpty>No matching option.</CommandEmpty>
            <CommandItem
              value={allLabel}
              onSelect={() => {
                onChange(undefined);
                setOpen(false);
              }}
            >
              <CheckIcon className={cn("size-3.5", value === undefined ? "opacity-100" : "opacity-0")} />
              {allLabel}
            </CommandItem>
            {options.map((option) => (
              <CommandItem
                key={option.value}
                value={`${option.label} ${option.keywords ?? ""}`}
                onSelect={() => {
                  onChange(option.value);
                  setOpen(false);
                }}
              >
                <CheckIcon
                  className={cn("size-3.5", option.value === value ? "opacity-100" : "opacity-0")}
                />
                <span className="truncate">{option.label}</span>
              </CommandItem>
            ))}
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}

function sortLabel(value: ArchiveLibraryViewState): string {
  if (value.sortField === "title") return value.order === "asc" ? "Name A–Z" : "Name Z–A";
  return `${value.sortField === "created_at" ? "Created" : "Archived"} ${value.order === "desc" ? "↓" : "↑"}`;
}

export function ArchiveLibraryToolbar({
  value,
  projectOptions,
  hostOptions,
  agentOptions,
  onChange,
  className,
}: ArchiveLibraryToolbarProps) {
  const hasFilters = Boolean(
    value.searchQuery ||
      value.project ||
      value.hostId ||
      value.agentName ||
      value.createdRange ||
      value.archivedRange,
  );
  const createdValid = !value.createdRange || parseArchiveDateRange(value.createdRange) !== null;
  const archivedValid = !value.archivedRange || parseArchiveDateRange(value.archivedRange) !== null;

  return (
    <div className={cn("space-y-1.5 border-b p-2", className)}>
      <div className="flex min-w-0 items-center gap-1.5">
        <div className="relative min-w-0 flex-1">
          <SearchIcon className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input
            type="search"
            value={value.searchQuery}
            onChange={(event) => onChange({ searchQuery: event.target.value })}
            aria-label={
              value.searchScope === "title"
                ? "Search archived session titles"
                : "Search archived conversation content"
            }
            placeholder={value.searchScope === "title" ? "Search session titles…" : "Search messages…"}
            className="h-8 pl-8 text-xs"
          />
        </div>
        <div className="flex shrink-0 rounded-md border bg-muted/20 p-0.5">
          {(["title", "content"] as const).map((scope) => (
            <Tooltip key={scope}>
              <TooltipTrigger asChild>
                <button
                  type="button"
                  aria-pressed={value.searchScope === scope}
                  className={cn(
                    "h-6 rounded px-1.5 text-[10px] text-muted-foreground",
                    value.searchScope === scope && "bg-background text-foreground shadow-sm",
                  )}
                  onClick={() => onChange({ searchScope: scope, searchQuery: "" })}
                >
                  {scope === "title" ? "Title" : "Content"}
                </button>
              </TooltipTrigger>
              <TooltipContent>
                {scope === "title"
                  ? "Search only session titles."
                  : "Search message and response content, then open the exact matching window."}
              </TooltipContent>
            </Tooltip>
          ))}
        </div>
        <Popover>
          <Tooltip>
            <TooltipTrigger asChild>
              <span className="shrink-0">
                <PopoverTrigger asChild>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    className="h-8 gap-1 px-2 text-[10px] font-normal"
                    aria-label={`Archive sort: ${sortLabel(value)}`}
                  >
                    {sortLabel(value)} <ChevronDownIcon className="size-3" />
                  </Button>
                </PopoverTrigger>
              </span>
            </TooltipTrigger>
            <TooltipContent>Sort by archive date, creation date, or session name.</TooltipContent>
          </Tooltip>
          <PopoverContent align="end" className="w-52 p-1">
            {(
              [
                ["archived_at", "Archive date"],
                ["created_at", "Create date"],
                ["title", "Name"],
              ] as const
            ).map(([field, label]) => (
              <Button
                key={field}
                type="button"
                variant="ghost"
                size="sm"
                className="w-full justify-start"
                onClick={() =>
                  onChange({ sortField: field, order: field === "title" ? "asc" : "desc" })
                }
              >
                <CheckIcon
                  className={cn("size-3.5", value.sortField === field ? "opacity-100" : "opacity-0")}
                />
                {label}
              </Button>
            ))}
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="w-full justify-start"
              onClick={() => onChange({ order: value.order === "asc" ? "desc" : "asc" })}
            >
              {value.sortField === "title"
                ? value.order === "asc"
                  ? "Switch to Z–A"
                  : "Switch to A–Z"
                : value.order === "desc"
                  ? "Switch to oldest first"
                  : "Switch to newest first"}
            </Button>
          </PopoverContent>
        </Popover>
      </div>

      <div className="flex min-w-0 items-center gap-1.5">
        <FilterCombobox
          label="Project"
          allLabel="All projects"
          value={value.project}
          options={projectOptions}
          onChange={(project) => onChange({ project })}
        />
        <FilterCombobox
          label="Host"
          allLabel="All hosts"
          value={value.hostId}
          options={hostOptions}
          onChange={(hostId) => onChange({ hostId })}
        />
        <FilterCombobox
          label="Agent"
          allLabel="All agents"
          value={value.agentName}
          options={agentOptions}
          onChange={(agentName) => onChange({ agentName })}
        />
        <Popover>
          <Tooltip>
            <TooltipTrigger asChild>
              <span className="shrink-0">
                <PopoverTrigger asChild>
                  <Button
                    type="button"
                    variant="outline"
                    size="icon-sm"
                    className={cn(
                      "size-8",
                      (value.createdRange || value.archivedRange) &&
                        "border-primary/40 bg-primary/5",
                    )}
                    aria-label="Filter archive by created and archived date ranges"
                  >
                    <CalendarRangeIcon className="size-3.5" />
                  </Button>
                </PopoverTrigger>
              </span>
            </TooltipTrigger>
            <TooltipContent>
              Limit by Created and Archived ranges using YYMMDD-YYMMDD; the end date is inclusive.
            </TooltipContent>
          </Tooltip>
          <PopoverContent align="end" className="w-72 space-y-2 p-3">
            <p className="text-xs font-medium">Date ranges</p>
            {(
              [
                ["createdRange", "Created"],
                ["archivedRange", "Archived"],
              ] as const
            ).map(([key, label]) => {
              const valid = key === "createdRange" ? createdValid : archivedValid;
              return (
                <label key={key} className="grid grid-cols-[4.5rem_1fr] items-center gap-2 text-xs">
                  <span className="text-muted-foreground">{label}</span>
                  <Input
                    value={value[key]}
                    onChange={(event) => onChange({ [key]: event.target.value })}
                    placeholder="YYMMDD-YYMMDD"
                    maxLength={13}
                    aria-invalid={!valid}
                    className="h-8 font-mono text-xs"
                  />
                </label>
              );
            })}
            <p className="text-[11px] text-muted-foreground">
              Example: 260901-260904. Leave a field empty for no limit.
            </p>
          </PopoverContent>
        </Popover>
        {hasFilters && (
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                className="size-8 shrink-0 text-destructive"
                aria-label="Clear all archive filters"
                onClick={() =>
                  onChange({
                    searchQuery: "",
                    project: undefined,
                    hostId: undefined,
                    agentName: undefined,
                    createdRange: "",
                    archivedRange: "",
                  })
                }
              >
                <RotateCcwIcon className="size-3.5" />
              </Button>
            </TooltipTrigger>
            <TooltipContent>Clear search, Project, Host, Agent, and both date ranges.</TooltipContent>
          </Tooltip>
        )}
      </div>
    </div>
  );
}
