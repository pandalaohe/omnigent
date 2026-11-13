import { useEffect, useRef, useState } from "react";
import { timeDay, timeMonday, timeMonth, timeYear } from "d3-time";
import type { HistoryCard } from "./taskBoardHistoryData";
import { DAY, activityColors } from "./taskBoardHistoryData";
import type { ClosedDay } from "./taskBoardActivityData";
import { localDay } from "./taskBoardActivityData";
import { activityMoney, activityNumber, activitySourceName } from "./TaskBoardActivityParts";
import { mapAxisTicks } from "./taskBoardRouteModel";
import { t } from "./taskBoardText";

export function TaskBoardHistoryChart({
  cards,
  counts,
  accounts,
  from,
  to,
  now,
  historyStart,
  capped,
  sourceOrder,
  sourceLabels,
  onRange,
}: {
  cards: HistoryCard[];
  counts: ClosedDay[];
  accounts: Map<string, number | null>;
  from: number;
  to: number;
  now: number;
  historyStart: number;
  capped: boolean;
  sourceOrder: string[];
  sourceLabels?: Record<string, string>;
  onRange: (from: number, to: number) => void;
}) {
  const [preview, setPreview] = useState<[number, number] | null>(null);
  const [hover, setHover] = useState<{ x: number; y: number; text: string } | null>(null);
  const ref = useRef<HTMLDivElement>(null),
    drag = useRef<{ x: number; t: number; moved: boolean } | null>(null),
    [width, setWidth] = useState(1000);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const observer = new ResizeObserver(() =>
      setWidth(el.clientWidth - (el.clientWidth > 620 ? 32 : 24)),
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  const start = Math.min(
      +timeDay.floor(new Date(historyStart)),
      ...cards.map((c) => c.first),
      ...counts.map((c) => Date.parse(c.day + "T00:00:00")),
    ),
    span = Math.max(DAY, +timeDay.offset(timeDay.floor(new Date(now)), 1) - start),
    W = Math.max(100, width),
    H = W > 620 ? 150 : 124,
    pad = 22,
    ih = H - pad - 16;
  const days = span / DAY,
    interval =
      (
        [
          [timeDay, 1],
          [timeDay.every(2)!, 2],
          [timeMonday, 7],
          [timeMonth, 30.4],
          [timeMonth.every(3)!, 91],
          [timeMonth.every(6)!, 182],
          [timeYear, 365],
        ] as const
      ).find(([, step]) => W / (days / step) >= (W > 620 ? 7 : 5))?.[0] ?? timeYear;
  const edges = interval
    .range(interval.floor(new Date(start)), interval.offset(new Date(now), 1))
    .map(Number);
  if (edges.length < 2) edges.push(now + DAY);
  const cells = edges
    .slice(0, -1)
    .map((a, i) => ({ a, b: edges[i + 1], values: new Map<string, number>() }));
  const add = (source: string, date: number, n: number) => {
    const cell = cells.find((c) => date >= c.a && date < c.b);
    if (cell) cell.values.set(source, (cell.values.get(source) ?? 0) + n);
  };
  if (capped) counts.forEach((c) => add(c.source, Date.parse(c.day + "T12:00:00"), c.closed));
  else cards.forEach((c) => add(c.source, c.end, 1));
  const totals = cells.map((c) => [...c.values.values()].reduce((a, b) => a + b, 0)),
    ranked = [...totals].sort((a, b) => b - a),
    max = Math.max(1, ranked[0] > ranked[1] * 2.5 ? ranked[1] * 1.3 : (ranked[0] ?? 1));
  const sources = [...new Set(cells.flatMap((c) => [...c.values.keys()]))].sort((a, b) =>
      cells.reduce((sum, c) => sum + (c.values.get(b) ?? 0) - (c.values.get(a) ?? 0), 0),
    ),
    top = sources.slice(0, 6),
    stacks = [...top, ...(sources.length > 6 ? ["other"] : [])],
    palette = stacks.map((source) =>
      source === "other"
        ? "var(--fg3)"
        : activityColors[Math.max(0, sourceOrder.indexOf(source)) % activityColors.length],
    );
  const x = (time: number) => 2 + ((time - start) / span) * (W - 4),
    clamp = (time: number) => Math.max(2, Math.min(W - 2, x(time)));
  const at = (clientX: number) => {
    const box = ref.current?.querySelector("svg")?.getBoundingClientRect();
    return (
      start + Math.max(0, Math.min(1, (clientX - (box?.left ?? 0)) / (box?.width ?? W))) * span
    );
  };
  const costs = cells.map((c) => {
      let sum = 0,
        has = false;
      for (const [day, value] of accounts)
        if (day >= localDay(c.a) && day < localDay(c.b)) {
          if (value === null) return null;
          sum += value;
          has = true;
        }
      return has ? sum : null;
    }),
    cmax = Math.max(1, ...costs.map((c) => c ?? 0));
  return (
    <div ref={ref} className="h-river" role="group" aria-label={t("g6.river")}>
      <div className="r-head">
        <b>
          {t(
            interval === timeDay
              ? "g6.daily"
              : interval === timeMonday
                ? "g6.weekly"
                : "g6.monthly",
          )}
        </b>
        <span className="r-names">
          {stacks.map((s, i) => (
            <span key={s}>
              <i style={{ background: palette[i] }} />
              {s === "other" ? t("g6.other") : s}
            </span>
          ))}
        </span>
        {costs.some((c) => c !== null) && (
          <span className="r-key">
            <i className="line" />
            {t("g6.allAccounts")} {activityMoney(cmax)}
          </span>
        )}
        <small>{t("g6.brushHint")}</small>
      </div>
      <svg
        width={W}
        height={H}
        viewBox={`0 0 ${W} ${H}`}
        onPointerDown={(e) => {
          setHover(null);
          drag.current = { x: e.clientX, t: at(e.clientX), moved: false };
          e.currentTarget.setPointerCapture?.(e.pointerId);
        }}
        onPointerMove={(e) => {
          const d = drag.current;
          if (d && Math.abs(e.clientX - d.x) > 6) {
            d.moved = true;
            setPreview([Math.min(d.t, at(e.clientX)), Math.max(d.t, at(e.clientX))]);
          }
        }}
        onPointerUp={() => {
          if (drag.current?.moved && preview) onRange(...preview);
          setPreview(null);
          drag.current = null;
        }}
        onPointerCancel={() => {
          setPreview(null);
          drag.current = null;
        }}
      >
        {cells.map((c, i) => {
          let y = H - pad;
          const total = totals[i],
            scale = (Math.min(1, max / total) * ih) / max,
            xa = Math.max(2, x(c.a)),
            step = x(c.b) - xa,
            w = Math.max(1.5, step - (step > 8 ? 2.2 : 1));
          return (
            <g key={c.a}>
              {stacks.map((s, j) => {
                const v =
                  s === "other"
                    ? sources.slice(6).reduce((sum, k) => sum + (c.values.get(k) ?? 0), 0)
                    : (c.values.get(s) ?? 0);
                if (!v) return null;
                const height = Math.max(1, v * scale);
                y -= height;
                return (
                  <rect
                    key={s}
                    x={xa}
                    y={y}
                    width={w}
                    height={Math.max(1, height - 0.6)}
                    fill={palette[j]}
                  />
                );
              })}
              {total > max && (
                <text className="r-cap" x={xa + w / 2} y={13}>
                  {activityNumber(total)}
                </text>
              )}
              <rect
                className="r-hit"
                onPointerEnter={(e) =>
                  setHover({
                    x: e.clientX,
                    y: e.clientY,
                    text: `${localDay(c.a)} – ${localDay(c.b - DAY)}，${activityNumber(total)} ${t("g6.cards")}\n${[
                      ...c.values,
                    ]
                      .sort((a, b) => b[1] - a[1])
                      .slice(0, 5)
                      .map(
                        ([source, count]) =>
                          activitySourceName(source, undefined, sourceLabels) +
                          " " +
                          activityNumber(count),
                      )
                      .join(
                        ", ",
                      )}${costs[i] !== null ? "\n" + t("g6.allAccounts") + " " + activityMoney(costs[i]!) : ""}`,
                  })
                }
                onPointerLeave={() => setHover(null)}
                x={xa}
                y={16}
                width={Math.max(2, step)}
                height={ih}
              >
                <title>
                  {localDay(c.a)} {t("g6.to")} {localDay(c.b - DAY)}
                  {": "}
                  {activityNumber(total)} {t("g6.cards")}
                  {costs[i] !== null ? ` · ${t("g6.allAccounts")} ${activityMoney(costs[i]!)}` : ""}
                </title>
              </rect>
            </g>
          );
        })}
        {mapAxisTicks({ from: start, to: now }, W, 66).ticks.map((time) =>
          x(+time) > 20 && x(+time) < W - 20 ? (
            <text
              key={+time}
              x={x(+time)}
              y={H - 6}
              fill="var(--fg2)"
              fontSize={12}
              textAnchor="middle"
            >
              {localDay(+time).slice(5)}
            </text>
          ) : null,
        )}
        {costs.some((c) => c !== null) && (
          <polyline
            className="r-cost"
            points={costs
              .flatMap((v, i) =>
                v === null
                  ? []
                  : [`${(x(cells[i].a) + x(cells[i].b)) / 2},${H - pad - (v / cmax) * ih}`],
              )
              .join(" ")}
            fill="none"
          />
        )}
        <rect
          className="r-shade"
          x={0}
          y={10}
          width={clamp(preview?.[0] ?? from)}
          height={ih + 6}
        />
        <rect
          className="r-shade"
          x={clamp(preview?.[1] ?? to)}
          y={10}
          width={W - clamp(preview?.[1] ?? to)}
          height={ih + 6}
        />
        {(preview ?? [from, to]).map((time, i) => (
          <line
            key={i ? "end" : "start"}
            role="slider"
            aria-label={t(i ? "g6.rangeEnd" : "g6.rangeStart")}
            aria-valuemin={start}
            aria-valuemax={now + DAY}
            aria-valuenow={time}
            tabIndex={0}
            x1={clamp(time)}
            x2={clamp(time)}
            y1={10}
            y2={H - pad}
            className="r-edge"
            onKeyDown={(e) => {
              if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) {
                e.preventDefault();
                const next =
                  e.key === "Home"
                    ? start
                    : e.key === "End"
                      ? now
                      : time + (e.key === "ArrowLeft" ? -DAY : DAY);
                if (i) onRange(from, Math.max(from + 1, Math.min(now + DAY, next)));
                else onRange(Math.min(to - 1, Math.max(start, next)), to);
              }
            }}
          />
        ))}
      </svg>
      {hover && (
        <div
          role="tooltip"
          className="tb-activity-tooltip"
          style={{ left: Math.min(hover.x + 12, Math.max(8, width - 280)), top: hover.y + 14 }}
        >
          {hover.text}
        </div>
      )}
    </div>
  );
}
