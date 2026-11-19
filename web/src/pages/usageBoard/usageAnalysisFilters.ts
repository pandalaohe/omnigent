import { usageBoardTheme } from "@/lib/boardFrame";
import { toDateInput } from "./usageBoardSelect";
import type { AnalysisFilter } from "./usageAnalysisData";

export const analysisPath = "/usage/board/analysis";
export const analysisDimensions = ["account", "model", "host", "agent"] as const;
export const analysisRanges = ["today", "d7", "d30", "d90"] as const;
export type FilterDimension = (typeof analysisDimensions)[number];
export type AnalysisRange = (typeof analysisRanges)[number];
export type PageFilter = Omit<AnalysisFilter, "tz" | "group" | "range"> & {
  group: FilterDimension;
  range: AnalysisRange | "custom";
};

const limits = { account: 64, model: 128, host: 64, agent: 32 };
const slotPattern = /^[1-7]-(?:[01][0-9]|2[0-3])$/;

function compareUnicode(left: string, right: string) {
  const a = Array.from(left);
  const b = Array.from(right);
  for (let index = 0; index < Math.min(a.length, b.length); index++) {
    const difference = a[index].codePointAt(0)! - b[index].codePointAt(0)!;
    if (difference) return difference;
  }
  return a.length - b.length;
}

function calendarDate(value: string | null) {
  if (!value || !/^[0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(value) || value.startsWith("0000"))
    return null;
  const timestamp = Date.parse(`${value}T00:00:00Z`);
  return Number.isFinite(timestamp) && new Date(timestamp).toISOString().startsWith(`${value}T`)
    ? value
    : null;
}

export function filterFromQuery(query: URLSearchParams, now = new Date()): PageFilter {
  const single = (key: string) => (query.getAll(key).length === 1 ? query.get(key) : null);
  const today = toDateInput(now.getTime());
  const earliest = new Date(now);
  earliest.setDate(earliest.getDate() - 399);
  const candidate = calendarDate(single("day"));
  const day =
    candidate && candidate >= toDateInput(earliest.getTime()) && candidate <= today
      ? candidate
      : null;
  const from = calendarDate(single("from"));
  const to = calendarDate(single("to"));
  const rangeParam = single("range");
  const hasCustomRange =
    rangeParam === "custom" &&
    from !== null &&
    to !== null &&
    from <= to &&
    to <= today &&
    Date.parse(to) - Date.parse(from) < 370 * 86_400_000;
  const custom = hasCustomRange || (rangeParam === "custom" && day !== null);
  const range = custom ? "custom" : (analysisRanges.find((key) => key === rangeParam) ?? "d30");
  const group = analysisDimensions.find((key) => key === single("group")) ?? "account";
  const slot = single("slot");
  function values(key: FilterDimension) {
    return [
      ...new Set(
        query
          .getAll(key)
          .filter(
            (value) =>
              Array.from(value).length <= limits[key] &&
              (key !== "agent" || value === "" || /^[a-z0-9][a-z0-9._-]{0,31}$/.test(value)),
          ),
      ),
    ]
      .sort(compareUnicode)
      .slice(0, 64);
  }
  return {
    range,
    group,
    account: values("account"),
    model: values("model"),
    host: values("host"),
    agent: values("agent"),
    day,
    slot: slot && slotPattern.test(slot) ? slot : null,
    from: hasCustomRange ? from : null,
    to: hasCustomRange ? to : null,
  };
}

export function toQuery(filter: PageFilter, shell?: URLSearchParams, api = false) {
  const query = new URLSearchParams();
  if (api || filter.range !== "d30") query.set("range", filter.range);
  if (api || filter.group !== "account") query.set("group", filter.group);
  for (const key of analysisDimensions)
    for (const value of [...filter[key]].sort(compareUnicode)) query.append(key, value);
  // Keep the return range in the link while the API reads just the selected day.
  const dateKeys =
    api && filter.day ? (["day", "slot"] as const) : (["day", "slot", "from", "to"] as const);
  for (const key of dateKeys) if (filter[key] !== null) query.set(key, filter[key]);
  const theme = shell && usageBoardTheme(shell);
  if (theme) query.set("theme", theme);
  return query;
}

export function analysisLink(search: URLSearchParams, values: Partial<PageFilter> = {}) {
  const filter = { ...filterFromQuery(new URLSearchParams()), ...values };
  const query = toQuery(filter, search).toString();
  return `${analysisPath}${query ? `?${query}` : ""}`;
}

export function toggleAnalysisValue(filter: PageFilter, dimension: FilterDimension, value: string) {
  const selected = filter[dimension];
  if (selected.includes(value))
    return { ...filter, [dimension]: selected.filter((key) => key !== value) };
  if (selected.length >= 64) return filter;
  return { ...filter, [dimension]: [...selected, value].sort(compareUnicode) };
}
