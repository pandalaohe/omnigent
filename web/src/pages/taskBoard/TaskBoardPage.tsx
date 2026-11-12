import { boardAttention } from "./boardAttentionData";
import { TaskBoardAttention } from "./TaskBoardAttention";
import { boardExitProps } from "@/lib/boardFrame";
import { useRef, useState } from "react";
import {
  ActivityIcon,
  CircleAlertIcon,
  HandIcon,
  ListTreeIcon,
  RouteIcon,
  HistoryIcon,
  CalendarDaysIcon,
  ScrollTextIcon,
  SettingsIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Link } from "@/lib/routing";
import { Spinner } from "@/components/ui/spinner";
import { taskIdentity, useTaskBoardData, type BoardTask } from "./taskBoardData";
import {
  useBoardQuota,
  useBoardQuotaCosts,
  useBoardSchedule,
  useBoardSessions,
  useBoardSilentSessions,
  useBoardSystem,
} from "./taskBoardNative";
import { BoardRefreshContext, useBoardPreferences } from "./taskBoardPreferences";
import {
  BoardAppearanceControls,
  BoardClock,
  TaskBoardDisplaySettings,
  useBoardAppearance,
} from "./TaskBoardDisplaySettings";
import { TaskBoardQuotaBand, TaskBoardQuotaPanel } from "./TaskBoardQuota";
import { TaskBoardHostsBand, TaskBoardHostsPanel } from "./TaskBoardHosts";
import { TaskBoardDetail, BoardDate, taskTitle } from "./TaskBoardDetail";
import { TaskBoardRoutes } from "./TaskBoardRoutes";
import { TaskBoardSearch } from "./TaskBoardSearch";
import { TaskBoardSessions } from "./TaskBoardSessions";
import { formatNumber, t } from "./taskBoardText";

import { TaskBoardActivity } from "./TaskBoardActivity";

function ReadFailure({ label, retry }: { label: string; retry: () => void }) {
  return (
    <div role="alert" className="tb-read-error">
      <span>{label}</span>
      <Button size="sm" variant="ghost" onClick={retry}>
        {t("board.retry")}
      </Button>
    </div>
  );
}
export function TaskBoardPage() {
  const [preferences, updatePreferences, storage] = useBoardPreferences();
  return (
    <BoardRefreshContext.Provider value={preferences.refresh}>
      <TaskBoardContent
        preferences={preferences}
        updatePreferences={updatePreferences}
        storage={storage}
      />
    </BoardRefreshContext.Provider>
  );
}
function TaskBoardContent({
  preferences,
  updatePreferences,
  storage,
}: {
  preferences: ReturnType<typeof useBoardPreferences>[0];
  updatePreferences: ReturnType<typeof useBoardPreferences>[1];
  storage: string;
}) {
  const tasksQuery = useTaskBoardData(),
    sessionsQuery = useBoardSessions(),
    system = useBoardSystem(),
    quota = useBoardQuota(),
    schedule = useBoardSchedule();
  const appearance = useBoardAppearance(preferences);
  const [selectedTask, setSelectedTask] = useState<BoardTask | null>(null);
  const [taskSelection, setTaskSelection] = useState<string[]>([]);
  const [panel, setPanel] = useState<
    "waiting" | "exceptions" | "running" | "quota" | "hosts" | null
  >(null);
  const quotaCosts = useBoardQuotaCosts(panel === "quota");
  const tasks = tasksQuery.data?.tasks ?? [],
    sources = tasksQuery.data?.sources ?? [],
    sessions = sessionsQuery.data ?? [];
  const selected =
    selectedTask && tasks.find((task) => taskIdentity(task) === taskIdentity(selectedTask));
  const silentSessions = useBoardSilentSessions(sessions);
  const silentIds = new Set(silentSessions.map((session) => session.id));
  const attention = boardAttention(tasks, sessions, silentIds);
  const waiting = attention.waiting.length,
    exceptions = attention.anomalies.length;
  const running = attention.running;
  const selectedRows =
    panel === "waiting"
      ? attention.waiting
      : panel === "exceptions"
        ? attention.anomalies
        : running;
  const detailTrigger = useRef<HTMLElement | null>(null);
  const closeDetail = () => {
    setSelectedTask(null);
    detailTrigger.current?.focus();
  };
  const select = (task: BoardTask) => {
    if (!document.activeElement?.closest(".tb-dc"))
      detailTrigger.current = document.activeElement as HTMLElement;
    setSelectedTask(task);
  };
  const inlineDetail = selected && (
    <TaskBoardDetail
      key={taskIdentity(selected)}
      task={selected}
      sourceCache={tasksQuery.data?.sourceCaches?.[selected["omnigent.ai:source"]]}
      tasks={tasks}
      sessions={sessions}
      onSelect={select}
      onClose={closeDetail}
    />
  );
  return (
    <div className={`tb-page ${appearance.dark ? "dark" : ""}`} style={appearance.style}>
      <header className="tb-page-header">
        <nav className="tb-view-tabs" aria-label={t("board.views")}>
          <Button
            variant={preferences.view === "sessions" ? "secondary" : "ghost"}
            aria-pressed={preferences.view === "sessions"}
            onClick={() => updatePreferences({ view: "sessions" })}
          >
            <ListTreeIcon className="size-4" />
            {t("view.sessions")}
          </Button>
          <Button
            variant={preferences.view === "routes" ? "secondary" : "ghost"}
            aria-pressed={preferences.view === "routes"}
            onClick={() => updatePreferences({ view: "routes" })}
          >
            <RouteIcon className="size-4" />
            {t("view.routes")}
          </Button>
          {(
            [
              ["log", ScrollTextIcon],
              ["history", HistoryIcon],
              ["today", CalendarDaysIcon],
            ] as const
          ).map(([view, Icon]) => (
            <Button
              key={view}
              variant={preferences.view === view ? "secondary" : "ghost"}
              aria-pressed={preferences.view === view}
              onClick={() => updatePreferences({ view })}
            >
              <Icon className="size-4" />
              {t(`view.${view}`)}
            </Button>
          ))}
        </nav>
        <div className="tb-header-controls">
          <BoardClock />
          <BoardAppearanceControls preferences={preferences} update={updatePreferences} />
          <button
            type="button"
            className="tb-gear"
            aria-label={t("display.settings")}
            aria-pressed={preferences.view === "settings"}
            onClick={() => updatePreferences({ view: "settings" })}
          >
            <SettingsIcon size={17} />
          </button>
        </div>
      </header>
      <div className="tb-top-band">
        <section className="tb-status-band" aria-label={t("band.status")}>
          <h2>{t("band.status")}</h2>
          <div className="tb-status-items">
            {(
              [
                ["waiting", HandIcon, waiting, "tb-waiting"],
                ["exceptions", CircleAlertIcon, exceptions, "tb-exceptions"],
                ["running", ActivityIcon, running.length, "tb-running"],
              ] as const
            ).map(([id, Icon, count, color]) => (
              <button
                type="button"
                key={id}
                className={count ? color : ""}
                onClick={() => setPanel(panel === id ? null : id)}
                aria-expanded={panel === id}
                aria-label={t("band.count", { count, label: t(`band.${id}`) })}
              >
                <strong>{formatNumber(count)}</strong>
                <span>
                  <b>
                    <Icon className="size-3.5" />
                    {t(`band.${id}`)}
                  </b>
                  <small>
                    {sessionsQuery.isError || tasksQuery.isError
                      ? t("band.unavailable")
                      : id === "waiting"
                        ? attention.waiting[0]?.text || t("band.noWaiting")
                        : id === "exceptions"
                          ? attention.anomalies[0]?.text || t("band.noExceptions")
                          : running.length
                            ? t("band.runningPreview", {
                                tools: running.filter((row) => row.toolRunning).length,
                                idle: attention.idle,
                              })
                            : t("band.noRunning")}
                  </small>
                </span>
              </button>
            ))}
          </div>
        </section>
        <TaskBoardQuotaBand
          accounts={quota.data?.accounts ?? []}
          preferences={preferences}
          onOpen={() => setPanel(panel === "quota" ? null : "quota")}
          loading={quota.isPending}
          failed={quota.isError}
          retry={() => void quota.refetch()}
        />
        <TaskBoardHostsBand
          hosts={system.data?.hosts ?? []}
          onOpen={() => setPanel(panel === "hosts" ? null : "hosts")}
          loading={system.isPending}
          failed={system.isError}
          retry={() => void system.refetch()}
        />
      </div>
      {(panel === "quota" || panel === "hosts") && (
        <section className="tb-attention" aria-label={t(`band.${panel}`)}>
          <header>
            <h2>{t(`band.${panel}`)}</h2>
            <Button variant="ghost" size="sm" onClick={() => setPanel(null)}>
              {t("detail.close")}
            </Button>
          </header>
          {panel === "quota" ? (
            <TaskBoardQuotaPanel
              accounts={quota.data?.accounts ?? []}
              costs={quotaCosts.data?.accounts}
              preferences={preferences}
              update={updatePreferences}
            />
          ) : (
            <TaskBoardHostsPanel hosts={system.data?.hosts ?? []} />
          )}
        </section>
      )}
      {panel && panel !== "quota" && panel !== "hosts" && (
        <section className="tb-attention" aria-label={t(`band.${panel}`)}>
          <header>
            <h2>{t(`band.${panel}`)}</h2>
            <Button variant="ghost" size="sm" onClick={() => setPanel(null)}>
              {t("detail.close")}
            </Button>
          </header>
          {!selectedRows.length && <p>{t("waiting.empty")}</p>}
          <TaskBoardAttention
            inlineDetail={inlineDetail}
            key={panel}
            rows={selectedRows}
            onSelect={select}
          />
        </section>
      )}
      {tasksQuery.isError && (
        <ReadFailure label={t("board.loadFailed")} retry={() => void tasksQuery.refetch()} />
      )}
      {sessionsQuery.isError && (
        <ReadFailure label={t("sessions.failed")} retry={() => void sessionsQuery.refetch()} />
      )}
      {preferences.view === "routes" && (
        <div className="mb-3 flex justify-end">
          <TaskBoardSearch
            options={tasks.map((task) => ({
              id: taskIdentity(task),
              label: taskTitle(task),
              hint: [task["omnigent.ai:key"], task["omnigent.ai:source"]]
                .filter(Boolean)
                .join(" · "),
            }))}
            selected={taskSelection}
            onChange={setTaskSelection}
          />
        </div>
      )}
      {Object.keys(tasksQuery.data?.sourceStates ?? {}).length > 0 && (
        <p role="status" className="tb-band-empty">
          {t("board.sourceUpdating")}
        </p>
      )}
      {(["log", "history", "today"] as const).map((view) =>
        preferences.view === view ? (
          <TaskBoardActivity
            inlineDetail={panel ? undefined : inlineDetail}
            detailSelection={
              panel || selected ? undefined : { task: selectedTask, close: closeDetail }
            }
            key={view}
            view={view}
            hiddenSources={view === "history" ? preferences.hiddenSources : []}
            sourceCaches={tasksQuery.data?.sourceCaches}
            tasks={tasks}
            sessions={sessions}
            onSelect={select}
          />
        ) : null,
      )}
      {preferences.view === "settings" && (
        <TaskBoardDisplaySettings
          preferences={preferences}
          update={updatePreferences}
          storage={storage}
          sources={sources}
          accounts={quota.data?.accounts ?? []}
        />
      )}
      {(preferences.view === "sessions" || preferences.view === "routes") &&
        (preferences.view === "sessions" ? (
          sessionsQuery.isPending ? (
            <Spinner aria-label={t("board.loading")} className="mx-auto my-12 size-5" />
          ) : (
            <TaskBoardSessions
              inlineDetail={panel ? undefined : inlineDetail}
              sessions={sessions}
              tasks={tasks}
              sourceCaches={tasksQuery.data?.sourceCaches}
              onSelect={select}
              onView={(view) => updatePreferences({ view })}
            />
          )
        ) : tasksQuery.isPending ||
          (!tasks.length && Object.keys(tasksQuery.data?.sourceStates ?? {}).length > 0) ? (
          <Spinner aria-label={t("board.loading")} className="mx-auto my-12 size-5" />
        ) : (
          <TaskBoardRoutes
            sourceCaches={tasksQuery.data?.sourceCaches}
            sessions={sessions}
            tasks={tasks.filter(
              (task) =>
                !preferences.hiddenSources.includes(task["omnigent.ai:source"]) &&
                (!taskSelection.length || taskSelection.includes(taskIdentity(task))),
            )}
            sources={sources.filter((source) => !preferences.hiddenSources.includes(source.id))}
            aliases={preferences.aliases}
            boardHours={preferences.boardHours}
            lodStation={preferences.lodStation}
            lodPhase={preferences.lodPhase}
            mode={preferences.mode}
            onMode={(mode) => updatePreferences({ mode })}
            focus={preferences.focus}
            onFocus={(focus) => updatePreferences({ focus })}
            onSelect={select}
            scheduled={schedule.data?.scheduled_tasks ?? []}
          />
        ))}
      {preferences.view === "routes" &&
        preferences.mode === "time" &&
        schedule.data?.scheduled_tasks.some(
          (task) => task.state === "active" && task.next_run_at,
        ) && (
          <section className="tb-scheduled">
            <h2>{t("schedule.title")}</h2>
            <ul>
              {schedule.data.scheduled_tasks
                .filter((task) => task.state === "active" && task.next_run_at)
                .map((task) => (
                  <li key={task.id}>
                    <Link {...boardExitProps()} to={`/tasks/${encodeURIComponent(task.id)}`}>
                      {task.name}
                    </Link>
                    <BoardDate value={task.next_run_at ?? undefined} />
                  </li>
                ))}
            </ul>
          </section>
        )}
    </div>
  );
}
