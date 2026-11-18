import { CalendarIcon, ChevronDownIcon, XIcon } from "lucide-react";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import type { UsageAnalysisV2 } from "./usageAnalysisData";
import {
  analysisDimensions,
  analysisRanges,
  toggleAnalysisValue,
  type FilterDimension,
  type PageFilter,
} from "./usageAnalysisFilters";
import { toDateInput } from "./usageBoardSelect";
import { formatOverviewTokens } from "./usageBoardFormat";
import { UsageName } from "./UsageName";

export const analysisDimensionLabels = {
  account: "Account",
  model: "Model",
  host: "Host",
  agent: "Agent",
};
const rangeLabels = { today: "Today", d7: "Last 7 days", d30: "Last 30 days", d90: "Last 90 days" };
export const analysisSections = {
  totals: "Totals",
  cumulative: "Cumulative",
  compare: "Compare with last period",
  items: "Each item",
  hosts: "Hosts",
  shares: "Token and cost share",
  composition: "Composition",
  failures: "Failure rate",
  heat: "When usage is highest",
  payback: "Subscription payback",
  remaining: "Unused last period",
  savings: "Cache savings",
};

export function UsageAnalysisRail({
  filter,
  facets,
  accounts,
  label,
  onChange,
  showSections,
}: {
  filter: PageFilter;
  facets?: UsageAnalysisV2["facets"];
  accounts: string[];
  label: (dimension: FilterDimension, key: string) => string;
  onChange: (filter: PageFilter) => void;
  showSections: boolean;
}) {
  const withDay = (day: string | null): PageFilter => ({
    ...filter,
    day,
    range: !day && filter.range === "custom" && !filter.from ? "d30" : filter.range,
  });
  const clear = () =>
    onChange({
      ...withDay(null),
      account: [],
      model: [],
      host: [],
      agent: [],
      slot: null,
    });
  const hasFilters =
    analysisDimensions.some((key) => filter[key].length) || filter.day || filter.slot;
  const now = new Date();
  const earliest = new Date(now);
  earliest.setDate(earliest.getDate() - 399);
  return (
    <div className="ua-rail" aria-label="Analysis filters" role="region">
      <div className="ua-controls">
        <div className="ua-segment" role="group" aria-label="Time range">
          {analysisRanges.map((range) => (
            <button
              key={range}
              type="button"
              aria-pressed={filter.range === range && !filter.day}
              onClick={() => onChange({ ...filter, range, day: null, from: null, to: null })}
            >
              {rangeLabels[range]}
            </button>
          ))}
          {filter.range === "custom" && filter.from ? (
            <span className="px-2 text-xs">
              {filter.from} – {filter.to}
            </span>
          ) : null}
        </div>
        <div className="ua-segment" role="group" aria-label="Group by">
          {analysisDimensions.map((group) => (
            <button
              key={group}
              type="button"
              aria-pressed={filter.group === group}
              onClick={() => onChange({ ...filter, group })}
            >
              {analysisDimensionLabels[group]}
            </button>
          ))}
        </div>
      </div>
      <div className="ua-filter-controls">
        {(["host", "agent", "model", "account"] as const).map((dimension) => {
          const options = new Map(
            (facets?.[dimension] ?? []).flatMap((item) =>
              typeof item.key === "string" ? [[item.key, item.tokens ?? null] as const] : [],
            ),
          );
          if (dimension === "account" && !facets)
            for (const key of accounts) options.set(key, null);
          for (const key of filter[dimension])
            if (!options.has(key)) options.set(key, facets ? 0 : null);
          return (
            <Popover key={dimension}>
              <PopoverTrigger asChild>
                <button
                  type="button"
                  className="ua-filter-control"
                  aria-label={`Filter by ${analysisDimensionLabels[dimension]}`}
                >
                  {analysisDimensionLabels[dimension]}{" "}
                  <ChevronDownIcon size={14} aria-hidden="true" />
                </button>
              </PopoverTrigger>
              <PopoverContent
                align="start"
                className="ua-filter-options"
                aria-label={`Filter by ${analysisDimensionLabels[dimension]}`}
              >
                {options.size ? (
                  Array.from(options, ([key, tokens]) => (
                    <label key={key} className="ua-filter-option">
                      <input
                        type="checkbox"
                        checked={filter[dimension].includes(key)}
                        disabled={
                          filter[dimension].length === 64 && !filter[dimension].includes(key)
                        }
                        onChange={() => onChange(toggleAnalysisValue(filter, dimension, key))}
                      />
                      <UsageName>{label(dimension, key)}</UsageName>
                      <small>{tokens === null ? "—" : formatOverviewTokens(tokens)}</small>
                    </label>
                  ))
                ) : (
                  <p role="status">{facets ? "No values in this range" : "Reading filters…"}</p>
                )}
              </PopoverContent>
            </Popover>
          );
        })}
        <label className="ua-filter-control">
          <CalendarIcon size={14} aria-hidden="true" /> Date
          <input
            aria-label="Date"
            type="date"
            min={toDateInput(earliest.getTime())}
            max={toDateInput(now.getTime())}
            value={filter.day ?? ""}
            onChange={(event) => {
              const day = event.currentTarget.value || null;
              if (!event.currentTarget.validity.valid) return;
              onChange(withDay(day));
            }}
          />
        </label>
      </div>
      {hasFilters ? (
        <div className="ua-picks" aria-label="Active filters">
          {analysisDimensions.flatMap((dimension) =>
            filter[dimension].map((key) => (
              <FilterChip
                key={JSON.stringify([dimension, key])}
                text={`${analysisDimensionLabels[dimension]}: ${label(dimension, key)}`}
                absent={
                  !!facets && !facets[dimension].some((item) => item.key === key || item.remainder)
                }
                onClick={() => onChange(toggleAnalysisValue(filter, dimension, key))}
              />
            )),
          )}
          {filter.day ? (
            <FilterChip text={`Date: ${filter.day}`} onClick={() => onChange(withDay(null))} />
          ) : null}
          {filter.slot ? (
            <FilterChip
              text={`Time slot: ${filter.slot}`}
              onClick={() => onChange({ ...filter, slot: null })}
            />
          ) : null}
          <button type="button" onClick={clear}>
            Clear filters
          </button>
        </div>
      ) : null}
      {showSections ? (
        <nav className="ua-section-nav" aria-label="Analysis sections">
          {Object.entries(analysisSections).map(([key, name]) => (
            <button
              key={key}
              type="button"
              onClick={() => {
                const section = document.getElementById(`analysis-${key}`);
                if (section instanceof HTMLDetailsElement) section.open = true;
                if (section) {
                  const rail = document.querySelector(".ua-rail");
                  section.style.scrollMarginTop = `${(rail?.getBoundingClientRect().height ?? 0) + 8}px`;
                  section.scrollIntoView({ block: "start" });
                }
              }}
            >
              {name}
            </button>
          ))}
        </nav>
      ) : null}
    </div>
  );
}

function FilterChip({
  text,
  absent,
  onClick,
}: {
  text: string;
  absent?: boolean;
  onClick: () => void;
}) {
  return (
    <button type="button" className="ua-chip" aria-label={`Remove ${text}`} onClick={onClick}>
      <UsageName>{`${text}${absent ? " · 0" : ""}`}</UsageName>
      <XIcon size={13} aria-hidden="true" />
    </button>
  );
}
