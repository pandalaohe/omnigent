import {
  BanIcon,
  CheckIcon,
  ExternalLinkIcon,
  ListTodoIcon,
  PlayIcon,
  TriangleAlertIcon,
} from "lucide-react";
import { Link } from "@/lib/routing";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { taskBoardAvailable } from "@/lib/capabilities";
import { useSessionNavigationPreferences } from "@/hooks/useSessionNavigationPreferences";
import { Button } from "@/components/ui/button";
import { PageScroll } from "@/components/PageScroll";
import { Spinner } from "@/components/ui/spinner";
import {
  safeSourceLink,
  useTaskBoardData,
  type BoardTask,
  type TaskProgress,
} from "./taskBoardData";
import { t } from "./taskBoardText";

const statusIcons = {
  "needs-action": [ListTodoIcon, "text-muted-foreground"],
  "in-process": [PlayIcon, "text-blue-500"],
  completed: [CheckIcon, "text-emerald-600"],
  failed: [TriangleAlertIcon, "text-destructive"],
  cancelled: [BanIcon, "text-muted-foreground"],
} as const;

export function TaskProgressIcon({ progress }: { progress: TaskProgress }) {
  const [Icon, color] = statusIcons[progress] ?? statusIcons["needs-action"];
  return (
    <Icon
      className={`size-4 shrink-0 ${color}`}
      role="img"
      aria-label={t(`progress.${progress}`)}
    />
  );
}

function TaskRow({ task }: { task: BoardTask }) {
  const href = safeSourceLink(
    Object.values(task.links ?? {}).find((link) => link.rel === "alternate")?.href,
  );
  return (
    <li className="flex items-start gap-3 border-t border-border/60 px-4 py-3 text-sm">
      <span className="pt-0.5">
        <TaskProgressIcon progress={task.progress} />
      </span>
      <div className="min-w-0 flex-1 break-words">
        {task["omnigent.ai:key"] && (
          <span className="mr-2 font-mono text-xs text-muted-foreground">
            {task["omnigent.ai:key"]}
          </span>
        )}
        {task.title || task.uid || t("task.untitled")}
      </div>
      {href ? (
        <a
          href={href}
          target="_blank"
          rel="noopener noreferrer"
          className="shrink-0 rounded p-1 text-muted-foreground hover:text-foreground focus-visible:outline-2"
          aria-label={t("source.open")}
        >
          <ExternalLinkIcon className="size-4" />
        </a>
      ) : (
        <span className="max-w-28 shrink-0 text-xs text-muted-foreground">
          {t("source.noPage")}
        </span>
      )}
    </li>
  );
}

function TaskBoardList() {
  const query = useTaskBoardData();
  if (query.isPending)
    return <Spinner aria-label={t("board.loading")} className="mx-auto my-16 size-5" />;
  if (query.isError)
    return (
      <div role="alert" className="space-y-3 p-6 text-sm">
        <p>{t("board.loadFailed")}</p>
        <Button variant="outline" onClick={() => void query.refetch()}>
          {t("board.retry")}
        </Button>
      </div>
    );
  const { tasks, sources } = query.data;
  return (
    <div>
      <header className="mb-6 flex items-baseline justify-between gap-3">
        <h1 className="text-xl font-semibold">{t("board.title")}</h1>
        <span className="text-sm text-muted-foreground">
          {t("board.tasks", { count: tasks.length })}
        </span>
      </header>
      {tasks.length === 0 ? (
        <div className="rounded-xl border border-border px-5 py-8">
          <h2 className="text-sm font-medium">{t("board.empty")}</h2>
          <p className="mt-2 text-sm text-muted-foreground">{t("board.connect")}</p>
        </div>
      ) : (
        sources.map((source) => {
          const sourceTasks = tasks.filter((task) => task["omnigent.ai:source"] === source.id);
          if (!sourceTasks.length) return null;
          const groups = new Map<string, { name: string; tasks: BoardTask[] }>();
          for (const task of sourceTasks) {
            const group = task["omnigent.ai:group"];
            const id = group?.id ?? "";
            if (!groups.has(id))
              groups.set(id, { name: group?.name || id || t("group.default"), tasks: [] });
            groups.get(id)!.tasks.push(task);
          }
          const href = safeSourceLink(source.source);
          return (
            <section key={source.id} className="mb-6" aria-label={source.name || source.id}>
              <h2 className="mb-3 flex items-center gap-2 text-base font-semibold">
                <span className="min-w-0 break-words">{source.name || source.id}</span>
                {href && (
                  <a
                    href={href}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="shrink-0 rounded p-1 text-muted-foreground"
                    aria-label={t("source.open")}
                  >
                    <ExternalLinkIcon className="size-3.5" />
                  </a>
                )}
              </h2>
              <div className="space-y-3">
                {Array.from(groups, ([id, group]) => (
                  <div key={id} className="overflow-hidden rounded-xl border border-border">
                    <h3 className="break-words bg-muted/40 px-4 py-2 text-xs font-medium text-muted-foreground">
                      {group.name}
                    </h3>
                    <ul>
                      {group.tasks.map((task) => (
                        <TaskRow key={task.uid} task={task} />
                      ))}
                    </ul>
                  </div>
                ))}
              </div>
            </section>
          );
        })
      )}
    </div>
  );
}

function TaskBoardContent() {
  const info = useServerInfo();
  const { showTaskBoard } = useSessionNavigationPreferences();
  if (info === "loading")
    return <Spinner aria-label={t("board.loading")} className="m-auto size-5" />;
  if (!taskBoardAvailable(info))
    return <h1 className="p-6 text-lg font-semibold">{t("board.serverOff")}</h1>;
  if (!showTaskBoard)
    return (
      <div className="space-y-3 p-6">
        <h1 className="text-lg font-semibold">{t("board.userOff")}</h1>
        <Link
          to="/settings/general#task-board-settings"
          className="text-sm underline underline-offset-2"
        >
          {t("board.enable")}
        </Link>
      </div>
    );
  return <TaskBoardList />;
}

export function TaskBoardRoute() {
  return (
    <PageScroll maxWidthClassName="max-w-4xl" contentClassName="px-4 sm:px-6">
      <TaskBoardContent />
    </PageScroll>
  );
}
