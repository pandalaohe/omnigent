import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, Info, Unplug, CircleAlert } from "lucide-react";
import { Button } from "@/components/ui/button";
import { interfaceText } from "@/lib/interfaceText";
import { usageJson } from "./usagePriceSourceData";
import { USAGE_BOARD_POLL_MS, type UsageAccountView } from "./usageBoardData";
import type { UsageActivity } from "./usageActivityData";

interface UsageEvent {
  id: string;
  source: string;
  account: string | null;
  type: string;
  time: number;
  severity: "error" | "warn" | "info" | "recover";
  data: Record<string, unknown> | null;
  episode: { recovered_at: number; recover_event_id: string } | null;
}
interface EventPage {
  events: UsageEvent[];
  next_before: string | null;
}
const kinds = [
  ["", "allEvents"],
  ["error", "eventError"],
  ["warn", "eventWarning"],
  ["disconnect", "eventDisconnect"],
  ["update", "eventUpdate"],
] as const;
const clock = (at: number | null) =>
  at === null
    ? "—"
    : new Date(at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

export function UsageEvents({
  accounts,
  activity,
  account,
  enabled = true,
}: {
  accounts: UsageAccountView[];
  activity?: UsageActivity;
  account?: string;
  enabled?: boolean;
}) {
  const [kind, setKind] = useState("");
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const params = new URLSearchParams({ limit: "6" });
  if (account) params.set("account", account);
  if (kind) params.set("kind", kind);
  const before = cursors.at(-1);
  if (before) params.set("before", before);
  const events = useQuery({
    queryKey: ["usageBoard", "events", params.toString()],
    enabled,
    queryFn: () => usageJson<EventPage>(`events?${params}`),
    refetchInterval: () => (document.visibilityState === "visible" ? USAGE_BOARD_POLL_MS : false),
  });
  const sources = useQuery({
    queryKey: ["usageBoard", "eventSources", account],
    enabled,
    queryFn: () =>
      usageJson<{ sources: { fresh_until: number | null }[] }>(
        `sources${account ? `?account=${encodeURIComponent(account)}` : ""}`,
      ),
    refetchInterval: USAGE_BOARD_POLL_MS,
  });
  const selected = accounts.filter((item) => !account || item.id === account);
  const quotaAt = Math.max(0, ...selected.map((item) => item.read_at ?? 0)) || null;
  const costAt =
    Math.max(
      0,
      ...(activity?.accounts
        .filter((item) => !account || item.account === account)
        .map((item) => item.totals.last_at ?? 0) ?? []),
    ) || null;
  const stale = sources.data?.sources.some(
    (source) => source.fresh_until !== null && source.fresh_until <= Date.now() / 1000,
  );
  return (
    <section
      className="rounded-[11px] border border-border bg-card p-4"
      aria-label={interfaceText("eventHistory")}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-semibold">{interfaceText("eventHistory")}</h2>
        <div className="flex flex-wrap gap-1" role="group" aria-label={interfaceText("eventType")}>
          {kinds.map(([value, label]) => (
            <Button
              key={value}
              size="sm"
              variant={kind === value ? "secondary" : "ghost"}
              aria-pressed={kind === value}
              onClick={() => {
                setKind(value);
                setCursors([null]);
              }}
            >
              {interfaceText(label)}
            </Button>
          ))}
        </div>
      </div>
      <div className="my-3 flex gap-4 text-xs text-muted-foreground">
        <span className={selected.some((item) => item.freshness === "old") ? "text-amber-600" : ""}>
          {interfaceText("quotaRefreshed", { time: clock(quotaAt) })}
        </span>
        <span className={stale ? "text-amber-600" : ""}>
          {interfaceText("costFetched", { time: clock(costAt) })}
        </span>
      </div>
      {events.isError ? (
        <p role="alert" className="text-sm text-destructive">
          {interfaceText("eventsFailed")}{" "}
          <Button variant="ghost" size="sm" onClick={() => void events.refetch()}>
            {interfaceText("retryEvents")}
          </Button>
        </p>
      ) : events.isPending ? (
        <p className="text-xs text-muted-foreground">{interfaceText("eventsLoading")}</p>
      ) : (
        <>
          <ol className="divide-y divide-border">
            {events.data.events.map((event) => (
              <EventRow key={`${event.source}:${event.id}`} event={event} />
            ))}
          </ol>
          {!events.data.events.length && (
            <p className="py-3 text-xs text-muted-foreground">{interfaceText("noEventsOnPage")}</p>
          )}
          <div className="mt-3 flex items-center justify-end gap-2 text-xs">
            <Button
              size="sm"
              variant="ghost"
              disabled={cursors.length === 1}
              onClick={() => setCursors(cursors.slice(0, -1))}
            >
              {interfaceText("previousEventsPage")}
            </Button>
            <span>{interfaceText("eventsPageNumber", { page: cursors.length })}</span>
            <Button
              size="sm"
              variant="ghost"
              disabled={!events.data.next_before}
              onClick={() => setCursors([...cursors, events.data.next_before])}
            >
              {interfaceText("nextEventsPage")}
            </Button>
          </div>
        </>
      )}
    </section>
  );
}

function EventRow({ event }: { event: UsageEvent }) {
  const disconnected = event.type === "connection" && event.severity === "error";
  const Icon = disconnected
    ? Unplug
    : event.severity === "error"
      ? CircleAlert
      : event.severity === "warn"
        ? AlertTriangle
        : Info;
  const label = interfaceText(
    disconnected
      ? "eventDisconnected"
      : event.severity === "error"
        ? "eventError"
        : event.severity === "warn"
          ? "eventWarning"
          : "eventUpdate",
  );
  const duration = event.episode ? Math.max(0, event.episode.recovered_at - event.time) : null;
  return (
    <li className="grid grid-cols-[auto_auto_1fr] items-start gap-3 py-3 text-xs">
      <time
        className="tabular-nums text-muted-foreground"
        dateTime={new Date(event.time * 1000).toISOString()}
      >
        {new Date(event.time * 1000).toLocaleDateString([], { month: "2-digit", day: "2-digit" })}{" "}
        {clock(event.time)}
      </time>
      <Icon
        aria-label={label}
        className={`size-4 ${event.severity === "error" ? "text-destructive" : event.severity === "warn" ? "text-amber-600" : "text-muted-foreground"}`}
      />
      <div className="min-w-0">
        <p>
          {String(event.data?.host ?? event.account ?? event.source)} · {label}
          {duration !== null ? (
            <span className="ml-2 text-muted-foreground">
              {interfaceText("eventRecovered", {
                minutes: Math.floor(duration / 60),
                seconds: duration % 60,
              })}
            </span>
          ) : null}
        </p>
        {event.data && (
          <details className="mt-1 text-muted-foreground">
            <summary className="cursor-pointer">{interfaceText("eventRawDetails")}</summary>
            <pre className="mt-2 overflow-x-auto whitespace-pre-wrap break-words">
              {JSON.stringify(event.data, null, 2)}
            </pre>
          </details>
        )}
      </div>
    </li>
  );
}
