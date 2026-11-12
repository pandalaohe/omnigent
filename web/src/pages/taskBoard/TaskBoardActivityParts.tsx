import type { ReactNode } from "react";
import { ChevronDown } from "lucide-react";
import { t, formatDate, formatNumber } from "./taskBoardText";
import type { BoardTask } from "./taskBoardData";
import { BoardPager } from "./TaskBoardRoutes";

export const activityStamp = (at: number, timeOnly = false) => {
  const now = Date.now(),
    date = new Date(at),
    day = (value: number) => {
      const d = new Date(value);
      return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    },
    key = day(at);
  const time = formatDate(date, { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  if (timeOnly || key === day(now)) return time;
  const prefix =
    now - at > 300 * 86400000
      ? key
      : key === day(now - 86400000)
        ? t("routes.yesterday")
        : at > now && at - now < 2 * 86400000
          ? t("routes.tomorrow")
          : key.slice(5);
  return prefix + " " + time;
};
export const activityDuration = (value: number) => {
  const seconds = Math.round(value);
  if (seconds < 3600) return activityNumber(Math.max(1, Math.round(seconds / 60))) + "m";
  const hours = Math.floor(seconds / 3600),
    minutes = Math.round((seconds % 3600) / 60);
  return hours >= 24
    ? activityNumber(Math.floor(hours / 24)) + "d " + (hours % 24) + "h"
    : hours + "h" + (minutes ? " " + minutes + "m" : "");
};
export const activityNumber = (amount: number | undefined) =>
  amount === undefined
    ? "—"
    : formatNumber(amount, {
        notation: "compact",
        maximumSignificantDigits: 3,
      });
export const activityMoney = (amount: number) =>
  "$" +
  formatNumber(amount, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
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
  small = true,
}: {
  small?: boolean;
  value: string;
  items: [string, string, ReactNode?][];
  onChange: (value: string) => void;
}) {
  return (
    <div className={small ? "seg seg-sm" : "seg"}>
      {items.map(([id, label, icon]) => (
        <button
          type="button"
          key={id}
          className={value === id ? "on" : ""}
          aria-pressed={value === id}
          onClick={() => onChange(id)}
        >
          {icon}
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
      total={total}
      formatValue={activityNumber}
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
