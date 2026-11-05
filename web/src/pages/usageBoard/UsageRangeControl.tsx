import { useState } from "react";
import { CalendarIcon, CheckIcon, ChevronDownIcon } from "lucide-react";
import { useUsageContextPreferences } from "@/hooks/useUsageContextPreferences";
import {
  patchUsageBoardPreferences,
  type UsageBoardPreferences,
} from "@/lib/usageContextPreferences";
import { Input } from "@/components/ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { toDateInput } from "./usageBoardSelect";

export const USAGE_RANGES = {
  today: "Today",
  h24: "24 hours",
  d7: "7 days",
  period: "This reset period",
  d30: "30 days",
  d90: "90 days",
  custom: "Custom",
};
export type UsageRange = NonNullable<UsageBoardPreferences["range"]>;

function rangeDates(key: UsageRange["key"], from: string, to: string): string {
  if (key === "period") return "Each account's own";
  if (key === "custom") return `${from} – ${to}`;
  const end = new Date();
  const start = new Date(end);
  if (key === "h24") start.setHours(start.getHours() - 24);
  else start.setDate(start.getDate() - { today: 0, d7: 6, d30: 29, d90: 89 }[key]);
  const format = (date: Date) =>
    date.toLocaleDateString("en-US", { month: "short", day: "numeric" });
  return `${format(start)} – ${format(end)}`;
}

export function usageRangeQuery(range: UsageRange): URLSearchParams {
  const params = new URLSearchParams({
    range: range.key,
    tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
  });
  if (range.key === "custom") {
    params.set("from", range.from ?? toDateInput(Date.now()));
    params.set("to", range.to ?? toDateInput(Date.now()));
  }
  return params;
}

export function UsageRangeControl() {
  const [open, setOpen] = useState(false);
  const board = useUsageContextPreferences().board;
  const range = board.range ?? { key: "d7" };
  const today = toDateInput(Date.now());
  const from = range.from ?? today;
  const to = range.to ?? today;
  const valid = (date: string) => /^\d{4}-\d{2}-\d{2}$/.test(date) && date <= today;
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label="Usage range"
          className="inline-flex min-h-11 items-center gap-1.5 rounded-md px-2 text-xs hover:bg-accent focus-visible:outline-2 focus-visible:outline-ring md:min-h-[30px]"
        >
          <CalendarIcon className="size-3.5" aria-hidden="true" />
          {USAGE_RANGES[range.key]}
          <ChevronDownIcon className="size-3" aria-hidden="true" />
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-[min(340px,calc(100vw-32px))] gap-1 p-1.5">
        {Object.entries(USAGE_RANGES).map(([id, name]) => {
          const key = id as UsageRange["key"];
          return (
            <button
              type="button"
              key={key}
              aria-pressed={range.key === key}
              className="flex min-h-11 items-center gap-2 rounded px-2 text-left text-xs hover:bg-accent focus-visible:outline-2 focus-visible:outline-ring md:min-h-[30px]"
              onClick={() => {
                patchUsageBoardPreferences({
                  range: key === "custom" ? { key, from, to } : { key },
                });
                if (key !== "custom") setOpen(false);
              }}
            >
              <span className="flex-1">{name}</span>
              <span className="text-[11px] text-muted-foreground">{rangeDates(key, from, to)}</span>
              {range.key === key ? (
                <CheckIcon className="size-3" aria-hidden="true" />
              ) : (
                <span className="w-3" />
              )}
            </button>
          );
        })}
        {range.key === "custom" ? (
          <div className="flex flex-wrap items-center gap-2 border-t border-border p-2">
            <label className="flex flex-col gap-1 text-xs">
              From
              <Input
                type="date"
                aria-label="Usage from date"
                className="h-9 w-36"
                required
                value={from}
                max={today}
                onChange={(event) => {
                  const next = event.target.value;
                  if (valid(next))
                    patchUsageBoardPreferences({
                      range: { key: "custom", from: next, to: next > to ? next : to },
                    });
                }}
              />
            </label>
            <label className="flex flex-col gap-1 text-xs">
              To
              <Input
                type="date"
                aria-label="Usage to date"
                className="h-9 w-36"
                required
                value={to}
                min={from || undefined}
                max={today}
                onChange={(event) => {
                  const next = event.target.value;
                  if (valid(next) && next >= from)
                    patchUsageBoardPreferences({
                      range: { key: "custom", from, to: next },
                    });
                }}
              />
            </label>
          </div>
        ) : null}
      </PopoverContent>
    </Popover>
  );
}
