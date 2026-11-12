import type { ReactNode } from "react";
import { ChevronDown } from "lucide-react";
import { t, formatDate } from "./taskBoardText";
import type { BoardTask } from "./taskBoardData";
import { BoardPager } from "./TaskBoardRoutes";

export const activityStamp = (at: number, timeOnly = false) =>
  formatDate(
    new Date(at),
    timeOnly
      ? { hour: "2-digit", minute: "2-digit", hour12: false }
      : { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false },
  );
export const activityDuration = (value: number) => {
  const seconds = Math.round(value);
  if (seconds < 3600) return Math.max(1, Math.round(seconds / 60)) + "m";
  const hours = Math.floor(seconds / 3600),
    minutes = Math.round((seconds % 3600) / 60);
  return hours >= 24
    ? Math.floor(hours / 24) + "d " + (hours % 24) + "h"
    : hours + "h" + (minutes ? " " + minutes + "m" : "");
};
export const activityMoney = (amount: number) =>
  amount >= 1000
    ? "$" + (amount / 1000).toFixed(1) + "k"
    : amount >= 100
      ? "$" + amount.toFixed(0)
      : amount >= 10
        ? "$" + amount.toFixed(1)
        : "$" + amount.toFixed(2);
export function ActivityMoney({
  amount,
  actual = false,
}: {
  amount: number | null;
  actual?: boolean;
}) {
  return (
    <span
      className={`usd ${amount === null ? "none" : actual ? "k-real" : "k-est"}`}
      title={t(actual ? "sessions.cost.actual.hint" : "sessions.cost.estimate.hint")}
    >
      {amount === null ? (
        "—"
      ) : (
        <>
          {activityMoney(amount)}
          <i>{t(actual ? "sessions.cost.actual" : "sessions.cost.estimate")}</i>
        </>
      )}
    </span>
  );
}
export function ActivitySegments({
  value,
  items,
  onChange,
}: {
  value: string;
  items: [string, string][];
  onChange: (value: string) => void;
}) {
  return (
    <div className="seg seg-sm">
      {items.map(([id, label]) => (
        <button
          type="button"
          key={id}
          className={value === id ? "on" : ""}
          aria-pressed={value === id}
          onClick={() => onChange(id)}
        >
          {label}
        </button>
      ))}
    </div>
  );
}
export function ActivityPages({
  page,
  total,
  size,
  onChange,
}: {
  page: number;
  total: number;
  size: number;
  onChange: (page: number) => void;
}) {
  return total > size ? (
    <BoardPager
      page={Math.min(page, Math.ceil(total / size) - 1)}
      pages={Math.ceil(total / size)}
      onChange={onChange}
    />
  ) : null;
}
export function ActivityTile({
  label,
  value,
  note,
}: {
  label: ReactNode;
  value: ReactNode;
  note?: ReactNode;
}) {
  return (
    <div className="h-stat">
      <b>{value}</b>
      <span>{label}</span>
      {note && <small>{note}</small>}
    </div>
  );
}
export const ActivityChevron = () => <ChevronDown size={16} className="h-chev" />;

export const activityTaskName = (task: BoardTask) =>
  task["omnigent.ai:sourceDisplay"]?.identity?.label ?? task.title ?? task.uid;
