import { useEffect, useRef, useState } from "react";
import { ChevronDown, X, ArrowUpRight } from "lucide-react";
import { type BoardTask, readBoardJson, taskIdentity } from "./taskBoardData";
import type { BoardSession } from "./taskBoardNative";
import { accountDayAmounts, type SourceCache } from "./taskBoardSnapshotData";
import type { ClosedDay } from "./taskBoardActivityData";
import { localDay } from "./taskBoardActivityData";
import {
  accountTotals,
  DAY,
  activityColors,
  historyCards,
  historyRoll,
  historySort,
  type HistoryCard,
} from "./taskBoardHistoryData";
import { TaskBoardHistoryChart } from "./TaskBoardHistoryChart";
import {
  ActivityChevron,
  ActivityMoney,
  ActivityPages,
  ActivitySegments,
  ActivityTile,
  activityDuration,
  activityMoney,
  activityNumber,
  activitySourceName,
  activityAgentName,
  activityStamp,
} from "./TaskBoardActivityParts";
import { BoardAgentLogo, BoardIdentityMark } from "./BoardIdentityMark";
import { TaskBoardDetail, taskTitle } from "./TaskBoardDetail";
import { t, formatDate } from "./taskBoardText";
import { boardExitProps } from "@/lib/boardFrame";
import { Link } from "@/lib/routing";

export function HistoryStrip({
  cards,
  from,
  to,
  color,
}: {
  color: string;
  cards: HistoryCard[];
  from: number;
  to: number;
}) {
  const stripRef = useRef<SVGSVGElement>(null),
    [stripWidth, setStripWidth] = useState(250);
  useEffect(() => {
    const parent = stripRef.current?.parentElement;
    if (!parent) return;
    const update = () => setStripWidth(Math.max(220, parent.clientWidth));
    const observer = new ResizeObserver(update);
    update();
    observer.observe(parent);
    return () => observer.disconnect();
  }, []);
  const W = stripWidth,
    y = 12,
    ts = cards.map((c) => c.end).sort((a, b) => a - b),
    xs = ts.map((time) =>
      Math.max(4, Math.min(W - 4, ((time - from) / Math.max(1, to - from)) * (W - 8) + 4)),
    ),
    roll = historyRoll(cards),
    room = xs.every((x, i) => !i || x - xs[i - 1] >= 13);
  const columns = Math.max(1, Math.floor(W / 4)),
    step = W / columns,
    bins = Array.from({ length: columns }, () => 0);
  ts.forEach(
    (time) =>
      bins[
        Math.max(
          0,
          Math.min(columns - 1, Math.floor(((time - from) / Math.max(1, to - from)) * columns)),
        )
      ]++,
  );
  const max = Math.max(6, ...bins);
  return (
    <svg ref={stripRef} className="cl" width={W} height={31} viewBox={`0 0 ${W} 31`}>
      <title>
        {t("g6.stripOutcome", {
          good: activityNumber(roll.good),
          muted: activityNumber(roll.muted),
          warn: activityNumber(roll.n - roll.good - roll.muted),
        })}
      </title>
      {ts.length <= 8 && room ? (
        <>
          {xs.length > 1 && (
            <line
              x1={xs[0]}
              x2={xs.at(-1)}
              y1={y}
              y2={y}
              stroke={color}
              strokeWidth={5}
              strokeLinecap="round"
            />
          )}
          {xs.map((x, i) => (
            <circle
              key={String(ts[i]) + "-" + String(x)}
              cx={x}
              cy={y}
              r={4.5}
              fill="var(--panel)"
              stroke="var(--fg)"
              strokeWidth={2}
            />
          ))}
        </>
      ) : (
        <>
          {xs.length > 1 && (
            <line
              x1={xs[0]}
              x2={xs.at(-1)}
              y1={y}
              y2={y}
              stroke={color}
              strokeWidth={2}
              opacity={0.35}
            />
          )}
          {bins.map((n, i) =>
            n ? (
              <rect
                key={String(i * step)}
                x={i * step}
                y={y - Math.sqrt(n / max) * 11}
                width={step - 0.8}
                height={Math.max(3, Math.sqrt(n / max) * 22)}
                fill={color}
              />
            ) : null,
          )}
        </>
      )}
      {[roll.good, roll.n - roll.good - roll.muted, roll.muted].map((n, i, all) => (
        <rect
          key={["good", "warn", "muted"][i]}
          x={(all.slice(0, i).reduce((a, b) => a + b, 0) / Math.max(1, roll.n)) * W}
          y={27}
          width={Math.max(0, (n / Math.max(1, roll.n)) * W - 1)}
          height={3}
          rx={1}
          fill={["var(--good)", "var(--warn)", "var(--fg3)"][i]}
        />
      ))}
    </svg>
  );
}
function HistoryNumbers({ cards }: { cards: HistoryCard[] }) {
  const r = historyRoll(cards);
  return (
    <>
      <span className="h-num">
        <b>{activityNumber(r.n)}</b>
      </span>
      <span className="h-num">
        <b>{activityDuration(r.seconds)}</b>
      </span>
      <span className="h-num h-usd">
        <ActivityMoney amount={r.usd} />
        <small>
          {t(r.unpriced ? "g6.unpriced" : "g6.allPriced", { count: activityNumber(r.unpriced) })}
        </small>
      </span>
      <ActivityChevron />
    </>
  );
}

export interface HistoryDetailSelection {
  task: BoardTask | null;
  close: () => void;
}

export function TaskBoardHistory({
  tasks,
  sessions,
  caches,
  counts,
  onSelect,
  detailSelection,
  now,
  sourceLabels,
}: {
  tasks: BoardTask[];
  sessions: BoardSession[];
  caches: Record<string, SourceCache>;
  counts: ClosedDay[];
  onSelect: (task: BoardTask) => void;
  detailSelection?: HistoryDetailSelection;
  now: number;
  sourceLabels?: Record<string, string>;
}) {
  const names = {
    ...Object.fromEntries(
      Object.entries(caches).map(([id, c]) => [id, c.display.identity?.label ?? ""]),
    ),
    ...sourceLabels,
  };
  const sourceName = (source: string) => activitySourceName(source, undefined, names);
  const [showMonths, setShowMonths] = useState(false);
  const [archiveGeneration, setArchiveGeneration] = useState<string | null>(null);
  const hasOldMonths = counts.some((c) => Date.parse(c.day + "T00:00:00") < now - 365 * DAY);
  const [range, setRange] = useState<[number, number]>([now - 84 * DAY, now + 3600000]),
    [sets, setSets] = useState<Record<string, string[]>>({ source: [], agent: [], outcome: [] }),
    [cost, setCost] = useState("all"),
    [sort, setSort] = useState("recent"),
    [pop, setPop] = useState<string | null>(null),
    [search, setSearch] = useState(""),
    [open, setOpen] = useState<string[]>([]),
    [pages, setPages] = useState<Record<string, number>>({}),
    [month, setMonth] = useState(""),
    [extra, setExtra] = useState<BoardTask[]>([]),
    [loading, setLoading] = useState(false),
    [error, setError] = useState(false);
  const monthSources = [
    ...new Set([
      ...Object.keys(caches),
      ...tasks.map((task) => task["omnigent.ai:source"]),
      ...counts.map((count) => count.source),
    ]),
  ].sort();
  const generationKey = JSON.stringify(
    monthSources.map((id) => [id, caches[id]?.pin.sourceGeneration, caches[id]?.pin.id]),
  );
  const currentGeneration = useRef(generationKey);
  currentGeneration.current = generationKey;
  useEffect(() => {
    request.current++;
    setExtra([]);
    setLoading(false);
    setError(false);
    setArchiveGeneration(null);
    const requestState = request;
    return () => {
      requestState.current++;
    };
  }, [generationKey]);
  useEffect(() => {
    if (!pop) return;
    const close = (event: PointerEvent) => {
      if (event.target instanceof Element && !event.target.closest(".fpop,.fdd,.ftok")) {
        setPop(null);
        setSearch("");
      }
    };
    document.addEventListener("pointerdown", close);
    return () => document.removeEventListener("pointerdown", close);
  }, [pop]);
  const request = useRef(0),
    [from, to] = range,
    merged = new Map(tasks.map((task) => [taskIdentity(task), task]));
  extra.forEach((task) => merged.set(taskIdentity(task), task));
  const archiveTask =
    detailSelection?.task && archiveGeneration === generationKey
      ? merged.get(taskIdentity(detailSelection.task))
      : undefined;
  const selectTask = (task: BoardTask) => {
    setArchiveGeneration(generationKey);
    onSelect(task);
  };
  const historyStart = Math.min(
    now,
    ...[...merged.values()]
      .map((task) =>
        Date.parse(task["omnigent.ai:sourceDisplay"]?.historyState?.first ?? task.created ?? ""),
      )
      .filter(Number.isFinite),
  );
  const displayFrom = Math.max(from, historyStart);
  const sourceColor = (source: string) =>
    activityColors[Math.max(0, Object.keys(caches).indexOf(source)) % activityColors.length];
  const all = historyCards([...merged.values()], sessions, now, caches),
    inRange = all.filter((c) => c.end >= from && c.end <= to);
  const matches = (c: HistoryCard) =>
    (!sets.source.length || sets.source.includes(c.source)) &&
    (!sets.agent.length || c.agents.some((a) => sets.agent.includes(a))) &&
    (!sets.outcome.length || sets.outcome.includes(c.outcome)) &&
    (cost === "all" ||
      (cost === "priced" && c.usd !== null) ||
      (["10", "50"].includes(cost) && c.usd !== null && c.usd >= Number(cost)));
  const filtered = inRange.filter(matches),
    total = historyRoll(filtered),
    sources = [...new Set(filtered.map((c) => c.source))];
  const startDay = new Date(from);
  startDay.setHours(0, 0, 0, 0);
  if (+startDay < from) startDay.setDate(startDay.getDate() + 1);
  const accounts = accountTotals(caches, localDay(startDay), localDay(to)),
    accountValues = [...accounts.values()],
    accountSum =
      accountValues.length && accountValues.every((v) => v.amount !== null)
        ? accountValues.reduce((a, b) => a + (b.amount ?? 0), 0)
        : null;
  const daily = new Map<string, number | null>();
  for (const [key, amount] of accountDayAmounts(caches)) {
    const date = (JSON.parse(key) as string[])[2];
    const old = daily.get(date);
    daily.set(date, amount === null || old === null ? null : (old ?? 0) + amount);
  }
  const reset = () => {
    setOpen([]);
    setPages({});
  };
  const setTime = (a: number, b: number) => {
    request.current++;
    setLoading(false);
    setError(false);
    setArchiveGeneration(null);
    setRange([a, b]);
    reset();
  };
  const pageOf = <T,>(id: string, list: T[], size: number) => {
    const page = Math.min(pages[id] ?? 0, Math.max(0, Math.ceil(list.length / size) - 1));
    return list.slice(page * size, page * size + size);
  };
  const pager = (id: string, count: number, size: number) => (
    <ActivityPages
      page={pages[id] ?? 0}
      total={count}
      size={size}
      onChange={(p) => setPages({ ...pages, [id]: p })}
    />
  );
  const rollSort = (a: HistoryCard[], b: HistoryCard[]) => {
    const x = historyRoll(a),
      y = historyRoll(b);
    return sort === "cost"
      ? (y.usd ?? 0) - (x.usd ?? 0)
      : sort === "time"
        ? y.seconds - x.seconds
        : sort === "size"
          ? y.n - x.n
          : y.latest - x.latest;
  };
  const sourceRows = sources
    .map((source) => ({ source, cards: filtered.filter((c) => c.source === source) }))
    .sort((a, b) => rollSort(a.cards, b.cards));
  const changeSet = (key: string, values: string[]) => {
    setSets({ ...sets, [key]: values });
    reset();
  };
  const loadMonth = async (wantedMonth = month) => {
    const ticket = ++request.current;
    const generation = generationKey;
    setLoading(true);
    setError(false);
    try {
      const batches = await Promise.all(
        monthSources.map(async (source) => {
          const cache = caches[source];
          const loaded: BoardTask[] = [];
          const query = new URLSearchParams({
            source,
            state: "closed",
            include_history: "1",
            closed_after: new Date(wantedMonth + "-01T00:00:00").toISOString(),
            closed_before: new Date(
              Number(wantedMonth.slice(0, 4)),
              Number(wantedMonth.slice(5)),
              1,
            ).toISOString(),
          });
          if (cache?.pin.id) {
            query.set("snapshot", cache.pin.id);
            query.set("source_generation", cache.pin.sourceGeneration);
          }
          const seen = new Set<string>();
          for (;;) {
            // eslint-disable-next-line no-await-in-loop
            const page = await readBoardJson<{
              entries: BoardTask[];
              "omnigent.ai:cursor"?: string;
            }>(`/v1/me/tasks?${query}`);
            if (ticket !== request.current || generation !== currentGeneration.current) return [];
            loaded.push(...page.entries);
            const cursor = page["omnigent.ai:cursor"];
            if (!cursor) break;
            if (seen.has(cursor)) throw Error("Repeated task cursor");
            seen.add(cursor);
            query.set("cursor", cursor);
          }
          if (cache?.pin.id) {
            const verify = new URLSearchParams({
              snapshot: cache.pin.id,
              source_generation: cache.pin.sourceGeneration,
            });
            await readBoardJson(
              `/v1/me/tasks/sources/${encodeURIComponent(source)}/display?${verify}`,
            );
          }
          return loaded;
        }),
      );
      if (ticket !== request.current || generation !== currentGeneration.current) return;
      setExtra(batches.flat());
      setRange([
        new Date(wantedMonth + "-01T00:00:00").getTime(),
        new Date(Number(wantedMonth.slice(0, 4)), Number(wantedMonth.slice(5)), 1).getTime() - 1,
      ]);
      reset();
    } catch {
      if (ticket === request.current && generation === currentGeneration.current) setError(true);
    } finally {
      if (ticket === request.current && generation === currentGeneration.current) setLoading(false);
    }
  };
  const oldYears = [...new Set(counts.map((c) => c.day.slice(0, 4)))].sort().reverse();
  return (
    <div
      className="h"
      onKeyDown={(e) => {
        if (e.key === "Escape" && pop) {
          e.stopPropagation();
          if (search) setSearch("");
          else setPop(null);
        }
      }}
    >
      {extra.length > 0 && (
        <button
          type="button"
          className="f-clear"
          onClick={() => {
            request.current++;
            setExtra([]);
            setMonth("");
            setShowMonths(false);
            setTime(now - 84 * DAY, now + 3600000);
          }}
        >
          {t("g6.recent")}
        </button>
      )}
      <div className="h-range">
        <b>
          {formatDate(displayFrom, { month: "long", day: "numeric" })} {t("g6.to")}{" "}
          {formatDate(Math.min(to - 1, now), { month: "long", day: "numeric" })}
        </b>
        <ActivitySegments
          value={String(Math.round((now - from) / DAY))}
          items={[7, 28, 84, 182, 365].map((n) => [String(n), t("g6.range." + n)])}
          onChange={(v) => setTime(now - Number(v) * DAY, now + 3600000)}
        />
      </div>
      <div className="h-stats">
        <ActivityTile
          value={activityNumber(total.n)}
          label={t("g6.cards")}
          note={t("g6.groupCounts", {
            sources: activityNumber(sources.length),
            groups: activityNumber(
              new Set(filtered.map((c) => JSON.stringify([c.source, c.group]))).size,
            ),
          })}
        />
        <ActivityTile
          value={t("g6.hours", { count: activityNumber(total.seconds / 3600) })}
          label={t("g6.workTime")}
          note={t("g6.nodes", { count: activityNumber(total.nodes) })}
        />
        <ActivityTile
          value={total.usd ? activityMoney(total.usd) : "—"}
          label={
            <>
              {t("g6.attributedCost")} <i className="est">{t("sessions.cost.estimate")}</i>
            </>
          }
          note={t("g6.pricedNodes", {
            priced: activityNumber(total.nodes - total.unpriced),
            unpriced: activityNumber(total.unpriced),
          })}
        />
        <ActivityTile
          value={accountSum ? activityMoney(accountSum) : "—"}
          label={
            <>
              {t("g6.allAccounts")} <i className="est">{t("sessions.cost.estimate")}</i>
            </>
          }
          note={t("g6.unattributed")}
        />
      </div>
      <TaskBoardHistoryChart
        cards={all.filter(matches)}
        counts={counts.filter((c) => !sets.source.length || sets.source.includes(c.source))}
        accounts={daily}
        from={from}
        to={to}
        now={now}
        historyStart={historyStart}
        capped={hasOldMonths}
        sourceOrder={Object.keys(caches)}
        sourceLabels={names}
        onRange={setTime}
      />
      <div className="h-filters">
        <div className="f-row">
          {["source", "agent", "outcome"].map((key) => {
            const count = new Map<string, number>();
            inRange.forEach((c) =>
              (key === "source" ? [c.source] : key === "agent" ? c.agents : [c.outcome]).forEach(
                (v) => count.set(v, (count.get(v) ?? 0) + 1),
              ),
            );
            const list = [...count.keys()]
                .sort((a, b) => count.get(b)! - count.get(a)!)
                .filter(
                  (v) =>
                    key !== "source" || sourceName(v).toLowerCase().includes(search.toLowerCase()),
                ),
              selected = sets[key],
              label = (v: string) =>
                key === "outcome"
                  ? t("g6.outcome." + (v || "unknown"))
                  : key === "agent"
                    ? activityAgentName(v)
                    : sourceName(v);
            return (
              <div key={key} className="fwrap">
                <button
                  type="button"
                  className={`fdd ${pop === key ? "open" : ""} ${selected.length ? "has" : ""}`}
                  onClick={() => {
                    setPop(pop === key ? null : key);
                    setSearch("");
                  }}
                >
                  <span>{t("g6.filter." + key)}</span>
                  <em>
                    {selected.length ? `${activityNumber(selected.length)} / ` : ""}
                    {activityNumber(count.size)}
                  </em>
                  <ChevronDown size={13} />
                </button>
                {pop === key && (
                  <div
                    className="fpop"
                    onKeyDown={(e) => {
                      if (e.key === "Escape") {
                        e.stopPropagation();
                        if (search) setSearch("");
                        else setPop(null);
                      }
                    }}
                  >
                    <div className="fpop-h">
                      <b>{t("g6.filter." + key)}</b>
                      <button
                        type="button"
                        onClick={() =>
                          changeSet(
                            key,
                            list.every((v) => selected.includes(v))
                              ? selected.filter((v) => !list.includes(v))
                              : [...new Set([...selected, ...list])],
                          )
                        }
                      >
                        {t(
                          list.length && list.every((v) => selected.includes(v))
                            ? "search.none"
                            : "search.all",
                        )}
                      </button>
                      <button
                        type="button"
                        onClick={() =>
                          changeSet(key, [
                            ...selected.filter((v) => !list.includes(v)),
                            ...list.filter((v) => !selected.includes(v)),
                          ])
                        }
                      >
                        {t("search.invert")}
                      </button>
                      <button
                        type="button"
                        className="fpop-x"
                        aria-label={t("detail.close")}
                        onClick={() => setPop(null)}
                      >
                        <X size={14} />
                      </button>
                    </div>
                    {key === "source" && (
                      <div className="fsearch">
                        <input
                          autoFocus
                          placeholder={t("g6.searchSources")}
                          value={search}
                          onChange={(e) => setSearch(e.target.value)}
                          onKeyDown={(e) => {
                            if (e.key === "Enter") {
                              e.preventDefault();
                              if (e.ctrlKey || e.metaKey)
                                changeSet(key, [...new Set([...selected, ...list])]);
                              else if (list[0])
                                changeSet(
                                  key,
                                  selected.includes(list[0])
                                    ? selected.filter((v) => v !== list[0])
                                    : [...selected, list[0]],
                                );
                              e.currentTarget.select();
                            }
                          }}
                        />
                      </div>
                    )}
                    <ul className="fopts">
                      {list.map((v) => (
                        <li key={v}>
                          <button
                            type="button"
                            className={selected.includes(v) ? "on" : ""}
                            aria-pressed={selected.includes(v)}
                            onClick={() =>
                              changeSet(
                                key,
                                selected.includes(v)
                                  ? selected.filter((s) => s !== v)
                                  : [...selected, v],
                              )
                            }
                          >
                            <i className="fck">{selected.includes(v) ? "✓" : ""}</i>
                            {key === "agent" ? (
                              <BoardAgentLogo id={v} size={13} />
                            ) : key === "source" ? (
                              <i className="dot" style={{ background: sourceColor(v) }} />
                            ) : null}
                            <span>{label(v)}</span>
                            <em>{activityNumber(count.get(v)!)}</em>
                          </button>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
            );
          })}
          <span className="f-sep" />
          <span className="f-l">{t("g6.cost")}</span>
          <ActivitySegments
            value={cost}
            items={["all", "priced", "10", "50"].map((v) => [v, t("g6.cost." + v)])}
            onChange={(v) => {
              setCost(v);
              reset();
            }}
          />
          <span className="f-sep" />
          <span className="f-l">{t("g6.sort")}</span>
          <ActivitySegments
            value={sort}
            items={["recent", "cost", "time", "size"].map((v) => [v, t("g6.sort." + v)])}
            onChange={(v) => {
              setSort(v);
              setPages({});
            }}
          />
        </div>
        <div className="f-tokens">
          {Object.entries(sets)
            .flatMap(([key, values]) =>
              values.map((v) => (
                <button
                  type="button"
                  className="ftok"
                  key={key + v}
                  onClick={() =>
                    changeSet(
                      key,
                      values.filter((s) => s !== v),
                    )
                  }
                >
                  {key === "outcome" ? t("g6.outcome." + (v || "unknown")) : v}
                  <X size={12} />
                </button>
              )),
            )
            .slice(0, 8)}
          {(Object.values(sets).some((v) => v.length) || cost !== "all") && (
            <button
              type="button"
              className="f-clear"
              onClick={() => {
                setSets({ source: [], agent: [], outcome: [] });
                setCost("all");
                reset();
              }}
            >
              {t("g6.clear")}
            </button>
          )}
        </div>
      </div>
      <div className="h-list">
        <div className="h-lhead">
          <h3>{t("g6.sourceCount", { count: activityNumber(sources.length) })}</h3>
          <span>{t("g6.hierarchyHint", { count: activityNumber(total.n) })}</span>
        </div>
        <div className="h-row h-cols">
          <span />
          <span>{t("g6.filter.source")}</span>
          <span className="h-axis">
            <em>{localDay(displayFrom).slice(5)}</em>
            <em>{t("g6.closedOutcome")}</em>
            <em>{localDay(Math.min(to, now)).slice(5)}</em>
          </span>
          <span>{t("g6.cards")}</span>
          <span>{t("g6.workTime")}</span>
          <span>{t("g6.cost")}</span>
          <span />
        </div>
        {pageOf("source", sourceRows, 10).map((p) => {
          const sourceId = JSON.stringify(["source", p.source]),
            groups = [...new Set(p.cards.map((c) => c.group))]
              .map((group) => ({ group, cards: p.cards.filter((c) => c.group === group) }))
              .sort((a, b) => rollSort(a.cards, b.cards));
          return (
            <section key={p.source} className={`h-g ${open.includes(sourceId) ? "expanded" : ""}`}>
              <button
                type="button"
                className="h-row h-p"
                aria-expanded={open.includes(sourceId)}
                onClick={() => setOpen(open.includes(sourceId) ? [] : [sourceId])}
              >
                <span className="h-bullet" style={{ background: sourceColor(p.source) }}>
                  {p.source[0].toUpperCase()}
                </span>
                <span className="h-name">
                  <b>{sourceName(p.source)}</b>
                  <small>
                    {t("g6.groups", { count: activityNumber(groups.length) })}
                    {" · "}
                    {localDay(Math.min(...p.cards.map((c) => c.end))).slice(5)} {t("g6.to")}{" "}
                    {localDay(historyRoll(p.cards).latest).slice(5)}
                  </small>
                </span>
                <span className="h-line">
                  <HistoryStrip
                    cards={p.cards}
                    from={displayFrom}
                    to={Math.min(to, now)}
                    color={sourceColor(p.source)}
                  />
                </span>
                <HistoryNumbers cards={p.cards} />
              </button>
              {open.includes(sourceId) && (
                <div className="h-sub">
                  {pageOf(sourceId, groups, 8).map((g) => {
                    const groupId = JSON.stringify([p.source, g.group]);
                    return (
                      <div
                        key={groupId}
                        className={`h-s ${open.includes(groupId) ? "expanded" : ""}`}
                      >
                        <button
                          type="button"
                          className="h-row h-srow"
                          aria-expanded={open.includes(groupId)}
                          onClick={() =>
                            setOpen(open.includes(groupId) ? [sourceId] : [sourceId, groupId])
                          }
                        >
                          <BoardIdentityMark id={groupId} label={g.group} size={24} />
                          <span className="h-name">
                            <b>{g.cards[0].groupLabel}</b>
                            <small>
                              {g.group} {" · "} {localDay(historyRoll(g.cards).latest).slice(5)}
                            </small>
                          </span>
                          <span className="h-line">
                            <HistoryStrip
                              cards={g.cards}
                              from={displayFrom}
                              to={Math.min(to, now)}
                              color={sourceColor(p.source)}
                            />
                          </span>
                          <HistoryNumbers cards={g.cards} />
                        </button>
                        {open.includes(groupId) && (
                          <div className="h-detail">
                            <div className="h-toks">
                              {pageOf(groupId, [...g.cards].sort(historySort(sort)), 10).map(
                                (c) => (
                                  <div
                                    key={c.id}
                                    className={`h-t ${open.includes(c.id) ? "expanded" : ""}`}
                                  >
                                    <button
                                      type="button"
                                      className="h-trow"
                                      aria-expanded={open.includes(c.id)}
                                      onClick={() =>
                                        setOpen(
                                          open.includes(c.id)
                                            ? [sourceId, groupId]
                                            : [sourceId, groupId, c.id],
                                        )
                                      }
                                    >
                                      <span
                                        className={`o-dot o-${["completed", "merged"].includes(c.outcome) ? "good" : ["dropped", "split"].includes(c.outcome) ? "muted" : "warn"}`}
                                      />
                                      <span className="h-ttl">
                                        <b>{c.task.title || taskTitle(c.task)}</b>
                                        <small>
                                          {" · "}
                                          {t("g6.outcome." + (c.outcome || "unknown"))}
                                          {" · "}
                                          {activityStamp(c.end)}
                                          {c.task["omnigent.ai:sourceDisplay"]?.historyState
                                            ?.commitCount
                                            ? ` · ${activityNumber(c.task["omnigent.ai:sourceDisplay"]!.historyState!.commitCount!)} ${t("g6.commits")}`
                                            : ""}
                                        </small>
                                      </span>
                                      <span className="h-ag">
                                        {c.agents.map((a) => (
                                          <BoardAgentLogo key={a} id={a} size={13} />
                                        ))}
                                      </span>
                                      <span className="h-dur">
                                        {c.seconds ? activityDuration(c.seconds) : ""}
                                      </span>
                                      <ActivityMoney
                                        amount={c.usd}
                                        actual={c.nodes
                                          .filter((n) => n.cost?.amount != null)
                                          .every((n) => n.cost?.kind === "actual")}
                                      />
                                    </button>
                                    {open.includes(c.id) && (
                                      <>
                                        <ul className="h-nodes">
                                          {pageOf(c.id, c.nodes, 8).map((n) => (
                                            <li key={n.id}>
                                              <BoardIdentityMark
                                                id={n.agent}
                                                label={n.agent}
                                                product={n.agent}
                                                size={22}
                                              />
                                              <span className="n-t">
                                                {activityStamp(n.from)} {t("g6.to")}{" "}
                                                {activityStamp(n.to)}
                                              </span>
                                              <span className="n-d">
                                                {activityDuration(
                                                  Math.max(60, (n.to - n.from) / 1000),
                                                )}
                                              </span>
                                              <span className="n-h">{n.host}</span>
                                              <span className="n-k">
                                                {n.tokens
                                                  ? `${activityNumber(n.tokens)} token`
                                                  : ""}
                                              </span>
                                              <ActivityMoney
                                                amount={n.cost?.amount ?? null}
                                                actual={n.cost?.kind === "actual"}
                                              />
                                              {n.session ? (
                                                <Link
                                                  {...boardExitProps()}
                                                  to={`/c/${encodeURIComponent(n.session)}`}
                                                >
                                                  <ArrowUpRight size={14} />
                                                </Link>
                                              ) : (
                                                <span className="n-sp" />
                                              )}
                                            </li>
                                          ))}
                                        </ul>
                                        {!c.nodes.length && (
                                          <p className="h-nonode">{t("g6.noNodes")}</p>
                                        )}
                                        {pager(c.id, c.nodes.length, 8)}
                                        <button
                                          type="button"
                                          className="f-clear"
                                          onClick={() => selectTask(c.task)}
                                        >
                                          {t("g6.openTask")}
                                        </button>
                                      </>
                                    )}
                                  </div>
                                ),
                              )}
                            </div>
                            {pager(groupId, g.cards.length, 10)}
                          </div>
                        )}
                      </div>
                    );
                  })}
                  {pager(sourceId, groups.length, 8)}
                </div>
              )}
            </section>
          );
        })}
        {pager("source", sourceRows.length, 10)}
        {!filtered.length && <p className="h-empty">{t("g6.noRecords")}</p>}
      </div>
      {archiveTask && detailSelection && (
        <TaskBoardDetail
          key={taskIdentity(archiveTask)}
          task={archiveTask}
          tasks={[...merged.values()]}
          sessions={sessions}
          sourceCache={caches[archiveTask["omnigent.ai:source"]]}
          onSelect={selectTask}
          onClose={detailSelection.close}
        />
      )}
      {(hasOldMonths || month) && (
        <button type="button" className="f-clear" onClick={() => setShowMonths(!showMonths)}>
          {t("g6.findMonth")}
        </button>
      )}
      {(hasOldMonths || month) && showMonths && (
        <div className="h-month">
          <div className="cap-m">
            {oldYears.map((year) => (
              <div className="cap-y" key={year}>
                <b>{year}</b>
                {Array.from(
                  { length: 12 },
                  (_, i) => `${year}-${String(i + 1).padStart(2, "0")}`,
                ).map((value) => {
                  const count = counts
                    .filter((c) => c.day.startsWith(value))
                    .reduce((sum, c) => sum + c.closed, 0);
                  return (
                    <button
                      type="button"
                      className={`cap-c ${month === value ? "on" : ""}`}
                      key={value}
                      disabled={!count || loading}
                      onClick={() => {
                        setMonth(value);
                        void loadMonth(value);
                      }}
                    >
                      <em>{value.slice(5)}</em>
                      <small>{activityNumber(count)}</small>
                    </button>
                  );
                })}
              </div>
            ))}
          </div>
          <label>
            {t("history.month")}{" "}
            <input
              type="month"
              value={month}
              onChange={(e) => {
                request.current++;
                setLoading(false);
                setMonth(e.target.value);
              }}
            />
          </label>
          <button type="button" disabled={!month || loading} onClick={() => void loadMonth()}>
            {t(error ? "board.retry" : "g6.loadMonth")}
          </button>
          {loading && (
            <button
              type="button"
              onClick={() => {
                request.current++;
                setLoading(false);
              }}
            >
              {t("sources.cancel")}
            </button>
          )}
          {error && <p role="alert">{t("activity.failed")}</p>}
        </div>
      )}
    </div>
  );
}
