import { useQuery } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { usageRangeQuery } from "@/pages/usageBoard/UsageRangeControl";
import type { UsageActivity } from "@/pages/usageBoard/usageActivityData";
import { useBoardCadence } from "./taskBoardPreferences";
import { sourceHistory, type SourceCache } from "./taskBoardSnapshotData";
import { readBoardJson, type BoardTask } from "./taskBoardData";
import type { BoardSession } from "./taskBoardNative";
import { localDay, readBoardHistory, readLocalClosedCounts } from "./taskBoardActivityData";
import { TaskBoardLog } from "./TaskBoardLog";
import { TaskBoardHistory } from "./TaskBoardHistory";
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
  sourceCaches,
  hiddenSources = EMPTY_HIDDEN,
}: {
  view: "log" | "history" | "today";
  tasks: BoardTask[];
  sessions: BoardSession[];
  onSelect: (task: BoardTask) => void;
  inlineDetail?: ReactNode;
  sourceCaches?: Record<string, SourceCache>;
  hiddenSources?: string[];
}) {
  const cadence = useBoardCadence(),
    info = useServerInfo(),
    [detail, setDetail] = useState(false);
  const caches = Object.fromEntries(
      Object.entries(sourceCaches ?? {}).filter(([id]) => !hiddenSources.includes(id)),
    ),
    visible = tasks.filter((task) => !hiddenSources.includes(task["omnigent.ai:source"]));
  const now = Date.now(),
    today = localDay(now),
    offset = -new Date(now).getTimezoneOffset();
  const history = useQuery({
    queryKey: ["taskBoard", "history"],
    queryFn: () => readBoardHistory(),
    enabled: !sourceCaches,
    retry: false,
    refetchInterval: cadence,
  });
  const counts = useQuery({
    queryKey: ["taskBoard", "closedCounts", offset],
    queryFn: () => readLocalClosedCounts(),
    enabled: !sourceCaches,
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
  const events = (sourceCaches ? sourceHistory(caches) : (history.data ?? [])).filter(
    (e) => !hiddenSources.includes(e.source),
  );
  const closedCounts = (
    sourceCaches ? Object.values(caches).flatMap((c) => c.counts) : (counts.data?.days ?? [])
  ).filter((c) => !hiddenSources.includes(c.source));
  return (
    <section className="tb-activity-final" aria-label={t("view." + view)}>
      {!sourceCaches && history.isError && (
        <p role="alert">
          {t("activity.failed")}{" "}
          <button type="button" onClick={() => void history.refetch()}>
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
      {detail && inlineDetail}
    </section>
  );
}
