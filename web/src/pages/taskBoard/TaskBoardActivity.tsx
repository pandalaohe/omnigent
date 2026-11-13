import { useQuery } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { usageRangeQuery } from "@/pages/usageBoard/UsageRangeControl";
import type { UsageActivity } from "@/pages/usageBoard/usageActivityData";
import { useBoardCadence } from "./taskBoardPreferences";
import { sourceHistory, type SourceCache } from "./taskBoardSnapshotData";
import { readBoardJson, type BoardTask, type TaskSource } from "./taskBoardData";
import type { BoardSession } from "./taskBoardNative";
import { localDay, useBoardHistory, readLocalClosedCounts } from "./taskBoardActivityData";
import { TaskBoardLog } from "./TaskBoardLog";
import { TaskBoardHistory, type HistoryDetailSelection } from "./TaskBoardHistory";
import { TaskBoardToday } from "./TaskBoardToday";
import { t } from "./taskBoardText";
import "./taskBoardActivity.css";

const EMPTY_HIDDEN: string[] = [];

export function TaskBoardActivity({
  view,
  tasks,
  sessions,
  onSelect,
  inlineDetail,
  detailSelection,
  sourceCaches,
  sources,
  hiddenSources = EMPTY_HIDDEN,
}: {
  view: "log" | "history" | "today";
  tasks: BoardTask[];
  sessions: BoardSession[];
  onSelect: (task: BoardTask) => void;
  inlineDetail?: ReactNode;
  detailSelection?: HistoryDetailSelection;
  sourceCaches?: Record<string, SourceCache>;
  sources?: TaskSource[];
  hiddenSources?: string[];
}) {
  const cadence = useBoardCadence(),
    info = useServerInfo(),
    [detail, setDetail] = useState(false);
  const excluded = view === "history" ? hiddenSources : EMPTY_HIDDEN;
  const caches = Object.fromEntries(
      Object.entries(sourceCaches ?? {}).filter(([id]) => !excluded.includes(id)),
    ),
    visible = tasks.filter((task) => !excluded.includes(task["omnigent.ai:source"]));
  const now = Date.now(),
    today = localDay(now),
    offset = -new Date(now).getTimezoneOffset();
  const history = useBoardHistory(view === "log", sourceCaches, sources);
  const counts = useQuery({
    queryKey: ["taskBoard", "closedCounts", offset],
    queryFn: () => readLocalClosedCounts(),
    enabled: !sourceCaches && view === "history",
    retry: false,
    refetchInterval: cadence,
  });
  const usage = useQuery({
    queryKey: ["taskBoard", "todayCost", today],
    enabled: view === "today" && info !== "loading" && !!info.usage_board_enabled,
    retry: false,
    queryFn: () =>
      readBoardJson<UsageActivity>(`/v1/me/usage/activity?${usageRangeQuery({ key: "today" })}`),
    refetchInterval: cadence,
  });
  const select = (task: BoardTask) => {
    setDetail(true);
    onSelect(task);
  };
  const events = (
    sourceCaches ? sourceHistory(caches).concat(history.events) : history.events
  ).filter((e) => !excluded.includes(e.source));
  const closedCounts = (
    sourceCaches ? Object.values(caches).flatMap((c) => c.counts) : (counts.data?.days ?? [])
  ).filter((c) => !excluded.includes(c.source));
  return (
    <section className="tb-activity-final" aria-label={t("view." + view)}>
      {view === "log" && history.isError && (
        <p role="alert">
          {t("activity.failed")}{" "}
          <button type="button" onClick={() => void history.refetch()}>
            {t("board.retry")}
          </button>
        </p>
      )}
      {view === "history" && counts.isError && (
        <p role="alert">
          {t("activity.countsFailed")}{" "}
          <button type="button" onClick={() => void counts.refetch()}>
            {t("board.retry")}
          </button>
        </p>
      )}
      {view === "today" && usage.isError && (
        <p role="alert">
          {t("today.costFailed")}{" "}
          <button type="button" onClick={() => void usage.refetch()}>
            {t("board.retry")}
          </button>
        </p>
      )}
      {view === "log" ? (
        <TaskBoardLog tasks={visible} sessions={sessions} events={events} onSelect={select} />
      ) : view === "history" ? (
        <TaskBoardHistory
          tasks={visible}
          sessions={sessions}
          caches={caches}
          counts={closedCounts}
          now={now}
          onSelect={select}
          detailSelection={detailSelection}
        />
      ) : (
        <TaskBoardToday
          tasks={visible}
          sessions={sessions}
          caches={caches}
          now={now}
          usage={usage.data}
          onSelect={select}
        />
      )}{" "}
      {view === "log" && history.hasEarlier && (
        <button
          type="button"
          disabled={history.loadingEarlier}
          onClick={() => void history.loadEarlier()}
        >
          {t("activity.loadEarlier")}
        </button>
      )}
      {view === "log" && history.earlierError && <p role="alert">{t("activity.failed")}</p>}
      {detail && inlineDetail}
    </section>
  );
}
