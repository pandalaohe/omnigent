import {
  ArrowDownAZIcon,
  ArrowUpAZIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  SearchIcon,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ArchiveTranscriptViewer } from "@/components/archive/ArchiveTranscriptViewer";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  type ArchivedConversationFilters,
  type Conversation,
  useArchivedConversations,
  useArchivedSessionFacets,
  useProjects,
} from "@/hooks/useConversations";
import { useHosts } from "@/hooks/useHosts";
import { conversationDisplayLabel } from "@/shell/sidebarNav";
import { cn } from "@/lib/utils";

const SPLIT_STORAGE_KEY = "omnigent:archive-library-rail-split-v1";

function readSplit(): number {
  const parsed = Number.parseFloat(localStorage.getItem(SPLIT_STORAGE_KEY) ?? "");
  return Number.isFinite(parsed) ? Math.min(0.76, Math.max(0.24, parsed)) : 0.42;
}

function useVerticalSplit() {
  const containerRef = useRef<HTMLDivElement>(null);
  const dragging = useRef(false);
  const [ratio, setRatio] = useState(readSplit);

  const updateFromPointer = useCallback((clientY: number) => {
    const bounds = containerRef.current?.getBoundingClientRect();
    if (!bounds || bounds.height <= 0) return;
    setRatio(Math.min(0.76, Math.max(0.24, (clientY - bounds.top) / bounds.height)));
  }, []);

  useEffect(() => {
    const onPointerMove = (event: PointerEvent) => {
      if (dragging.current) updateFromPointer(event.clientY);
    };
    const onPointerUp = () => {
      if (!dragging.current) return;
      dragging.current = false;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", onPointerUp);
    return () => {
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", onPointerUp);
      onPointerUp();
    };
  }, [updateFromPointer]);

  useEffect(() => {
    localStorage.setItem(SPLIT_STORAGE_KEY, String(ratio));
  }, [ratio]);

  return {
    containerRef,
    ratio,
    handleProps: {
      role: "separator" as const,
      tabIndex: 0,
      "aria-label": "Resize archive list and conversation",
      "aria-orientation": "horizontal" as const,
      "aria-valuemin": 24,
      "aria-valuemax": 76,
      "aria-valuenow": Math.round(ratio * 100),
      onPointerDown: (event: React.PointerEvent<HTMLDivElement>) => {
        event.preventDefault();
        dragging.current = true;
        document.body.style.cursor = "row-resize";
        document.body.style.userSelect = "none";
        updateFromPointer(event.clientY);
      },
      onKeyDown: (event: React.KeyboardEvent<HTMLDivElement>) => {
        if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
        event.preventDefault();
        setRatio((current) =>
          Math.min(0.76, Math.max(0.24, current + (event.key === "ArrowDown" ? 0.04 : -0.04))),
        );
      },
    },
  };
}

const DEFAULT_FILTERS: ArchivedConversationFilters = {
  searchQuery: "",
  dateField: "archived_at",
  sortField: "archived_at",
  agePreset: "any",
  order: "desc",
};

export function ArchiveLibraryRail() {
  const { containerRef, ratio, handleProps } = useVerticalSplit();
  const [query, setQuery] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const [project, setProject] = useState("");
  const [hostId, setHostId] = useState("");
  const [agentName, setAgentName] = useState("");
  const [order, setOrder] = useState<"asc" | "desc">("desc");
  const [selected, setSelected] = useState<Conversation | null>(null);
  const [pageAfter, setPageAfter] = useState<string | undefined>();
  const [pageHistory, setPageHistory] = useState<(string | undefined)[]>([]);
  const facets = useArchivedSessionFacets();
  const projects = useProjects();
  const hosts = useHosts({ includeSandbox: true });

  useEffect(() => {
    const timeout = window.setTimeout(() => setDebouncedQuery(query.trim()), 250);
    return () => window.clearTimeout(timeout);
  }, [query]);

  const filters = useMemo<ArchivedConversationFilters>(
    () => ({
      ...DEFAULT_FILTERS,
      searchQuery: debouncedQuery,
      project: project || undefined,
      hostId: hostId || undefined,
      agentName: agentName || undefined,
      order,
    }),
    [agentName, debouncedQuery, hostId, order, project],
  );
  useEffect(() => {
    setPageAfter(undefined);
    setPageHistory([]);
  }, [filters]);

  const archivedQuery = useArchivedConversations(filters, pageAfter);
  const archived = useMemo(() => archivedQuery.data?.data ?? [], [archivedQuery.data]);
  const hostNames = useMemo(
    () => new Map((hosts.data ?? []).map((host) => [host.host_id, host.name])),
    [hosts.data],
  );
  const projectNames = useMemo(
    () =>
      [
        ...new Set([
          ...(facets.data?.projects ?? []),
          ...(projects.data ?? []).map((candidate) => candidate.name),
        ]),
      ].sort((a, b) => a.localeCompare(b)),
    [facets.data, projects.data],
  );

  useEffect(() => {
    if (archivedQuery.isLoading) return;
    const current = archived.find((conversation) => conversation.id === selected?.id);
    setSelected(current ?? archived[0] ?? null);
  }, [archived, archivedQuery.isLoading, selected?.id]);

  return (
    <div
      ref={containerRef}
      className="flex min-h-0 flex-1 flex-col"
      data-testid="archive-library-rail"
    >
      <section
        className="min-h-0 shrink-0 overflow-y-auto p-3"
        style={{ height: `${ratio * 100}%` }}
      >
        <div className="mb-2 flex items-center gap-2">
          <h2 className="text-sm font-semibold">Archive Library</h2>
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            className="ml-auto"
            aria-label={order === "desc" ? "Sort oldest first" : "Sort newest first"}
            onClick={() => setOrder((current) => (current === "desc" ? "asc" : "desc"))}
          >
            {order === "desc" ? <ArrowDownAZIcon /> : <ArrowUpAZIcon />}
          </Button>
        </div>
        <div className="relative">
          <SearchIcon className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            aria-label="Search archived sessions and messages"
            placeholder="Search sessions and messages…"
            className="h-8 pl-8 text-sm"
          />
        </div>
        <div className="mt-2 grid grid-cols-3 gap-1">
          <select
            aria-label="Filter archive by project"
            value={project}
            onChange={(event) => setProject(event.target.value)}
            className="h-7 min-w-0 rounded-md border bg-background px-1 text-xs"
          >
            <option value="">All projects</option>
            {projectNames.map((value) => (
              <option key={value}>{value}</option>
            ))}
          </select>
          <select
            aria-label="Filter archive by host"
            value={hostId}
            onChange={(event) => setHostId(event.target.value)}
            className="h-7 min-w-0 rounded-md border bg-background px-1 text-xs"
          >
            <option value="">All hosts</option>
            {(facets.data?.hostIds ?? []).map((value) => (
              <option key={value} value={value}>
                {hostNames.get(value) ?? value}
              </option>
            ))}
          </select>
          <select
            aria-label="Filter archive by agent"
            value={agentName}
            onChange={(event) => setAgentName(event.target.value)}
            className="h-7 min-w-0 rounded-md border bg-background px-1 text-xs"
          >
            <option value="">All agents</option>
            {(facets.data?.agentNames ?? []).map((value) => (
              <option key={value}>{value}</option>
            ))}
          </select>
        </div>

        <div className="mt-2 flex flex-col gap-1" role="listbox" aria-label="Archived sessions">
          {archivedQuery.isLoading ? (
            <p className="p-2 text-xs text-muted-foreground">Loading…</p>
          ) : archived.length === 0 ? (
            <p className="p-2 text-xs text-muted-foreground">No archived sessions match.</p>
          ) : (
            archived.map((conversation) => (
              <button
                key={conversation.id}
                type="button"
                role="option"
                aria-selected={selected?.id === conversation.id}
                className={cn(
                  "rounded-md border border-transparent px-2 py-1.5 text-left hover:bg-muted",
                  selected?.id === conversation.id && "border-primary/30 bg-primary/5",
                )}
                onClick={() => setSelected(conversation)}
              >
                <span className="block truncate text-xs font-medium">
                  {conversationDisplayLabel(conversation)}
                </span>
                <span className="block truncate text-[11px] text-muted-foreground">
                  {hostNames.get(conversation.host_id ?? "") ??
                    conversation.host_id ??
                    "Unknown host"}
                  {conversation.agent_name ? ` · ${conversation.agent_name}` : ""}
                </span>
                {conversation.search_snippet && (
                  <span className="mt-0.5 line-clamp-2 block text-[11px] leading-4 text-foreground/70">
                    {conversation.search_snippet}
                  </span>
                )}
              </button>
            ))
          )}
        </div>
        {(pageHistory.length > 0 || archivedQuery.data?.has_more) && (
          <div className="mt-2 flex items-center justify-between gap-2 border-t pt-2">
            <Button
              type="button"
              variant="ghost"
              size="xs"
              disabled={pageHistory.length === 0 || archivedQuery.isFetching}
              onClick={() => {
                const previous = pageHistory.at(-1);
                setPageHistory((current) => current.slice(0, -1));
                setPageAfter(previous);
              }}
            >
              <ChevronLeftIcon /> Previous
            </Button>
            <span className="text-[11px] text-muted-foreground">Page {pageHistory.length + 1}</span>
            <Button
              type="button"
              variant="ghost"
              size="xs"
              disabled={!archivedQuery.data?.has_more || archivedQuery.isFetching}
              onClick={() => {
                const next = archivedQuery.data?.last_id;
                if (!next) return;
                setPageHistory((current) => [...current, pageAfter]);
                setPageAfter(next);
              }}
            >
              Next <ChevronRightIcon />
            </Button>
          </div>
        )}
      </section>

      <div
        {...handleProps}
        className="group relative h-1 shrink-0 cursor-row-resize bg-border/80 outline-none focus-visible:bg-primary/50"
      >
        <span className="absolute top-1/2 left-1/2 h-1 w-10 -translate-x-1/2 -translate-y-1/2 rounded-full bg-border-strong group-hover:bg-primary/50" />
      </div>
      <ArchiveTranscriptViewer conversation={selected} />
    </div>
  );
}
