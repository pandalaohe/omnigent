import { boardAttention } from "./boardAttentionData";
import { TaskBoardAttention } from "./TaskBoardAttention";
import { boardExitProps } from "@/lib/boardFrame";
import { useState } from "react";
import {
  ActivityIcon,
  ChevronDownIcon,
  ChevronRightIcon,
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
  sessionTree,
  useBoardQuota,
  useBoardSchedule,
  useBoardSessionDetails,
  useBoardSessions,
  useBoardSilentSessions,
  useBoardSystem,
  type BoardSession,
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
import { TaskBoardRoutes, BoardPager } from "./TaskBoardRoutes";
import { TaskBoardSearch } from "./TaskBoardSearch";
import { TaskProgressIcon } from "./TaskProgressIcon";
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
function SessionsView({
  sessions,
  selected,
  onSelect,
  silent,
}: {
  sessions: BoardSession[];
  selected: string[];
  onSelect: (ids: string[]) => void;
  silent: Set<string>;
}) {
  const [page, setPage] = useState(0);
  const [expanded, setExpanded] = useState<string[]>([]);
  const parents = new Set(
    sessions.flatMap((session) => (session.parent_session_id ? [session.parent_session_id] : [])),
  );
  let foldedDepth = Infinity;
  const tree = sessionTree(
    sessions.filter((session) => !selected.length || selected.includes(session.id)),
  ).filter((row) => {
    if (row.depth > foldedDepth) return false;
    foldedDepth =
      parents.has(row.session.id) && !expanded.includes(row.session.id) ? row.depth : Infinity;
    return true;
  });
  const pages = Math.max(1, Math.ceil(tree.length / 20)),
    currentPage = Math.min(page, pages - 1);
  const visible = tree.slice(currentPage * 20, (currentPage + 1) * 20);
  const details = useBoardSessionDetails(
    visible.filter((row) => !row.session.archived).map((row) => row.session.id),
  );
  const byId = new Map(
    details.flatMap((detail) => (detail.data ? [[detail.data.id, detail.data] as const] : [])),
  );
  return (
    <section className="tb-sessions" aria-label={t("view.sessions")}>
      <div className="tb-section-toolbar">
        <h2>{t("sessions.title", { count: sessions.length })}</h2>
        <TaskBoardSearch
          options={sessions.map((session) => ({
            id: session.id,
            label: session.title || session.id,
            hint: session.workspace ?? session.host_id ?? undefined,
          }))}
          selected={selected}
          onChange={(ids) => {
            onSelect(ids);
            setPage(0);
          }}
        />
      </div>
      {!tree.length && <p className="tb-empty">{t("sessions.empty")}</p>}
      <ul>
        {visible.map(({ session: row, depth }) => {
          const detail = byId.get(row.id);
          const session = {
            ...row,
            ...detail,
            pending_elicitations_count:
              detail?.pending_elicitations_count ?? row.pending_elicitations_count,
          };
          return (
            <li
              key={session.id}
              className="tb-session-row"
              style={{ paddingLeft: 12 + Math.min(depth, 3) * 18 }}
            >
              <div className="flex items-center gap-1">
                {parents.has(session.id) && (
                  <Button
                    size="icon-sm"
                    variant="ghost"
                    aria-expanded={expanded.includes(session.id)}
                    aria-label={t(
                      expanded.includes(session.id) ? "sessions.collapse" : "sessions.expand",
                      { title: session.title || session.id },
                    )}
                    onClick={() =>
                      setExpanded((current) =>
                        current.includes(session.id)
                          ? current.filter((id) => id !== session.id)
                          : [...current, session.id],
                      )
                    }
                  >
                    <ChevronDownIcon
                      className={`size-4 ${expanded.includes(session.id) ? "" : "-rotate-90"}`}
                    />
                  </Button>
                )}
                <Link
                  {...boardExitProps()}
                  to={`/c/${encodeURIComponent(session.id)}`}
                  className="tb-session-link min-w-0 flex-1"
                >
                  <span className="tb-session-status">
                    <TaskProgressIcon
                      progress={
                        session.status === "running"
                          ? "in-process"
                          : session.status === "failed"
                            ? "failed"
                            : "needs-action"
                      }
                      waiting={(session.pending_elicitations_count ?? 0) > 0}
                    />
                  </span>
                  <div className="min-w-0 flex-1">
                    <h3 className="truncate">{session.title || session.id}</h3>
                    <p className="truncate text-xs text-muted-foreground">
                      {[session.host_id, session.workspace, session.git_branch]
                        .filter(Boolean)
                        .join(" · ")}
                    </p>
                  </div>
                  <span className="tb-session-state">
                    {session.archived
                      ? t("sessions.archived")
                      : silent.has(session.id)
                        ? t("sessions.silent")
                        : t(`session.${session.status}`)}
                  </span>
                  <ChevronRightIcon className="size-4 shrink-0 text-muted-foreground" />
                </Link>
              </div>
              {session.todos?.length ? (
                <ol className="tb-todos">
                  {session.todos.map((todo, i) => (
                    // Native todo snapshots carry no stable item id.
                    // eslint-disable-next-line react/no-array-index-key
                    <li key={i}>
                      <TaskProgressIcon
                        progress={
                          todo.status === "completed"
                            ? "completed"
                            : todo.status === "in_progress"
                              ? "in-process"
                              : "needs-action"
                        }
                      />
                      <span>
                        {todo.status === "in_progress" && todo.activeForm
                          ? todo.activeForm
                          : todo.content}
                      </span>
                    </li>
                  ))}
                </ol>
              ) : null}
            </li>
          );
        })}
      </ul>
      {details.some((detail) => detail.isError) && (
        <p role="status" className="mt-3 text-xs text-muted-foreground">
          {t("sessions.detailsFailed")}
        </p>
      )}
      <BoardPager page={currentPage} pages={pages} onChange={setPage} />
    </section>
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
  const [sessionSelection, setSessionSelection] = useState<string[]>([]);
  const [taskSelection, setTaskSelection] = useState<string[]>([]);
  const [panel, setPanel] = useState<
    "waiting" | "exceptions" | "running" | "quota" | "hosts" | null
  >(null);
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
  const select = (task: BoardTask) => {
    setSelectedTask(task);
  };
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
                                tools: running.filter((row) => row.session?.tool).length,
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
          <TaskBoardAttention key={panel} rows={selectedRows} onSelect={select} />
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
            <SessionsView
              sessions={sessions}
              selected={sessionSelection}
              onSelect={setSessionSelection}
              silent={silentIds}
            />
          )
        ) : tasksQuery.isPending ||
          (!tasks.length && Object.keys(tasksQuery.data?.sourceStates ?? {}).length > 0) ? (
          <Spinner aria-label={t("board.loading")} className="mx-auto my-12 size-5" />
        ) : (
          <TaskBoardRoutes
            tasks={tasks.filter(
              (task) =>
                !preferences.hiddenSources.includes(task["omnigent.ai:source"]) &&
                (!taskSelection.length || taskSelection.includes(taskIdentity(task))),
            )}
            sources={sources}
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
      {selected && (
        <TaskBoardDetail
          key={taskIdentity(selected)}
          task={selected}
          sourceCache={tasksQuery.data?.sourceCaches?.[selected["omnigent.ai:source"]]}
          tasks={tasks}
          sessions={sessions}
          onSelect={select}
          onClose={() => setSelectedTask(null)}
        />
      )}
    </div>
  );
}
