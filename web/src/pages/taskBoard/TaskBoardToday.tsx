import type { BoardTask } from "./taskBoardData";
import type { BoardSession } from "./taskBoardNative";
import { boardSessionRows } from "./taskBoardSessionData";
import { accountTotals } from "./taskBoardHistoryData";
import { latestRelease, type SourceCache } from "./taskBoardSnapshotData";
import { localDay, repositoryCommitTotals } from "./taskBoardActivityData";
import { t } from "./taskBoardText";
import { BoardIdentityMark, boardIdentityColor } from "./BoardIdentityMark";
import {
  activityNumber,
  activityTaskName,
  activityTaskKey,
  activityAgentName,
  activityStamp,
  activityMoney,
} from "./TaskBoardActivityParts";
import type { UsageActivity } from "@/pages/usageBoard/usageActivityData";
import {
  Coins,
  FlaskConical,
  Network,
  PackageCheck,
  Quote,
  CircleCheck,
  CircleX,
  TriangleAlert,
} from "lucide-react";

export function TaskBoardToday({
  tasks,
  sessions,
  caches,
  now,
  onSelect,
  usage,
}: {
  tasks: BoardTask[];
  sessions: BoardSession[];
  caches: Record<string, SourceCache>;
  now: number;
  onSelect: (task: BoardTask) => void;
  usage?: UsageActivity;
  sourceLabels?: Record<string, string>;
}) {
  const today = localDay(now),
    closed = tasks
      .filter((task) => {
        const d = task["omnigent.ai:sourceDisplay"];
        const end = d?.historyState?.end ?? task.progressUpdated;
        return (
          end &&
          localDay(end) === today &&
          Date.parse(end) <= now &&
          (!!d?.historyState?.end || task.progress === "completed")
        );
      })
      .sort(
        (a, b) =>
          Date.parse(b["omnigent.ai:sourceDisplay"]?.historyState?.end ?? b.progressUpdated ?? "") -
          Date.parse(a["omnigent.ai:sourceDisplay"]?.historyState?.end ?? a.progressUpdated ?? ""),
      );
  const commits = new Map<string, { source: string }>();
  tasks.forEach((task) =>
    (task["omnigent.ai:sourceDisplay"]?.commits ?? [])
      .filter((c) => localDay(c.at) === today && Date.parse(c.at) <= now)
      .forEach((c) =>
        commits.set(JSON.stringify([task["omnigent.ai:source"], c.id]), {
          source: task["omnigent.ai:source"],
        }),
      ),
  );
  const repositoryTotals = repositoryCommitTotals(caches, now);
  const release = latestRelease(caches),
    hosts = [...new Set(Object.values(caches).flatMap((c) => c.display.checkHosts ?? []))].slice(
      0,
      2,
    );
  const receipts = tasks
    .filter((task) => task["omnigent.ai:sourceDisplay"]?.historyState?.active !== false)
    .map((task) => ({
      task,
      checks: hosts.map(
        (host) =>
          task["omnigent.ai:sourceDisplay"]?.checks
            ?.filter((check) => check.host === host)
            .sort((a, b) => Date.parse(b.at) - Date.parse(a.at))[0],
      ),
    }))
    .filter((row) => row.checks.some(Boolean))
    .sort((a, b) => {
      const priority = (checks: typeof a.checks) =>
        Math.min(
          ...checks.map((check) =>
            check?.verdict === "failed" ? 0 : check?.verdict === "flaky" ? 1 : 2,
          ),
        );
      return (
        priority(a.checks) - priority(b.checks) ||
        Date.parse(b.task["omnigent.ai:sourceDisplay"]?.lastActivity ?? b.task.updated ?? "") -
          Date.parse(a.task["omnigent.ai:sourceDisplay"]?.lastActivity ?? a.task.updated ?? "")
      );
    });
  const quotes = tasks
    .flatMap((task) =>
      (task["omnigent.ai:sourceDisplay"]?.quotes ?? [])
        .filter((q) => q.text.length > 8 && !/^推荐|^按表格/.test(q.text))
        .map((q) => ({ task, q })),
    )
    .sort(
      (a, b) =>
        String(b.q.date ?? b.q.at ?? "").localeCompare(String(a.q.date ?? a.q.at ?? "")) ||
        (a.q.rank ?? 0) - (b.q.rank ?? 0),
    );
  const projected = boardSessionRows(tasks, sessions, caches).map((row) => row.session);
  const agentLabels = new Map(
    tasks.flatMap((task) =>
      (task["omnigent.ai:sourceDisplay"]?.actors ?? []).map(
        (actor) => [actor.agentId, actor.label] as const,
      ),
    ),
  );
  const agents = new Map<string, { n: number; live: number; cost: number | null }>();
  for (const s of projected)
    if (
      localDay(s.lastActivityAt) === today ||
      (s.archivedAt && localDay(s.archivedAt) === today)
    ) {
      const a = agents.get(s.agentId) ?? { n: 0, live: 0, cost: null };
      a.n++;
      if (s.status !== "archived") a.live++;
      if (s.cost?.amount != null) a.cost = (a.cost ?? 0) + s.cost.amount;
      agents.set(s.agentId, a);
    }
  const maxN = Math.max(1, ...[...agents.values()].map((a) => a.n)),
    accounts = [...accountTotals(caches, today, today)].map(([id, a]) => ({ id, ...a }));
  const nativeAccounts = (usage?.accounts ?? []).map((a) => ({
    id: "native:" + a.account,
    label: a.account,
    amount: a.totals.cost_usd,
  }));
  const rows = [...accounts, ...nativeAccounts]
      .filter((a) => a.amount !== 0)
      .sort((a, b) => (b.amount ?? 0) - (a.amount ?? 0))
      .slice(0, 6),
    maxCost = Math.max(1, ...rows.map((a) => a.amount ?? 0));
  return (
    <div
      className={`td${receipts.length ? " has-receipts" : ""}${quotes.length ? " has-decisions" : ""}`}
    >
      <section className="td-p td-ship">
        <h3>
          <PackageCheck size={16} />
          {t("g6.landed")}
          <em>{t("g6.reportedGit")}</em>
        </h3>
        <div className="td-ship-body">
          <div>
            <div className="td-stats td-commit-stats">
              <div>
                <b>{repositoryTotals.total ?? t("g6.unavailable")}</b>
                <span title={t("g6.repositoryCommitsHint")}>
                  {t("g6.repositoryCommits")}
                  {repositoryTotals.partial ? ` · ${t("g6.partial")}` : ""}
                </span>
              </div>
              <div>
                <b>{activityNumber(closed.length)}</b>
                <span>{t("today.closed")}</span>
              </div>
              <div className="td-secondary">
                <b>{activityNumber(commits.size)}</b>
                <span title={t("g6.taskCommitsHint")}>{t("g6.taskCommits")}</span>
              </div>
              <div className="td-secondary">
                <b>{release?.name ?? "—"}</b>
                <span>
                  {t("g6.latestRelease")}
                  {release ? `, ${activityStamp(Date.parse(release.publishedAt))}` : ""}
                </span>
              </div>
            </div>
            <div className="td-roots">
              {repositoryTotals.rows.map(({ repository, count }) => (
                <span key={repository}>
                  {count !== null && (
                    <i
                      style={{
                        background: boardIdentityColor(repository),
                        opacity: 0.22,
                        width: `${Math.max(
                          4,
                          (count / Math.max(1, repositoryTotals.total ?? 0)) * 100,
                        )}%`,
                      }}
                    />
                  )}
                  <b>{repository}</b>
                  <em>{count ?? t("g6.unavailable")}</em>
                </span>
              ))}
            </div>
          </div>
          <ul className="td-list">
            {closed.slice(0, 6).map((task) => (
              <li key={JSON.stringify([task["omnigent.ai:source"], task.uid])}>
                <span className="td-t">
                  {activityStamp(
                    Date.parse(
                      task["omnigent.ai:sourceDisplay"]?.historyState?.end ?? task.progressUpdated!,
                    ),
                    true,
                  )}
                </span>
                <button type="button" className="td-x" onClick={() => onSelect(task)}>
                  <span className="td-task-title">{task.title || activityTaskName(task)}</span>
                  {activityTaskKey(task) && <code>{activityTaskKey(task)}</code>}
                </button>
              </li>
            ))}
          </ul>
        </div>
      </section>
      {receipts.length > 0 && (
        <section className="td-p td-rcp">
          <h3>
            <FlaskConical size={16} />
            {t("g6.testReceipts")}
            <em>{t("g6.reportedChecks")}</em>
          </h3>
          <div className="overflow-x-auto">
            <table
              className="w-full table-fixed"
              style={{ minWidth: `${240 + hosts.length * 140 + 120}px` }}
            >
              <colgroup>
                <col style={{ width: 240 }} />
                {hosts.map((host) => (
                  <col key={host} style={{ width: 140 }} />
                ))}
                <col style={{ width: 120 }} />
              </colgroup>
              <thead>
                <tr>
                  <th>{t("g6.task")}</th>
                  {hosts.map((host) => (
                    <th key={host}>
                      <span className="block truncate" title={host}>
                        {host}
                      </span>
                    </th>
                  ))}
                  <th>{t("g6.lastCheck")}</th>
                </tr>
              </thead>
              <tbody>
                {receipts.slice(0, 11).map(({ task, checks }) => (
                  <tr key={JSON.stringify([task["omnigent.ai:source"], task.uid])}>
                    <th>
                      <button
                        type="button"
                        className="block w-full min-w-0 text-left"
                        onClick={() => onSelect(task)}
                      >
                        <b className="block truncate" title={activityTaskName(task)}>
                          {activityTaskName(task)}
                        </b>
                      </button>
                    </th>
                    {hosts.map((host, index) => {
                      const check = checks[index];
                      return (
                        <td key={host} className={`v-${check?.verdict ?? "none"}`}>
                          {check?.verdict === "passed" ? (
                            <CircleCheck size={13} />
                          ) : check?.verdict === "failed" ? (
                            <CircleX size={13} />
                          ) : check ? (
                            <TriangleAlert size={13} />
                          ) : null}
                          {check ? t("sessions.check." + check.verdict) : t("g6.noReceipt")}
                          {check && <small>{activityNumber(check.count)}</small>}
                        </td>
                      );
                    })}
                    <td className="td-t">
                      {activityStamp(
                        Math.max(
                          ...checks.flatMap((check) => (check ? [Date.parse(check.at)] : [])),
                        ),
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
      {quotes.length > 0 && (
        <section className="td-p td-dec">
          <h3>
            <Quote size={16} />
            {t("g6.decisions")}
            <em>{t("g6.reportedQuotes")}</em>
          </h3>
          {quotes.slice(0, 5).map(({ task, q }) => (
            <blockquote key={JSON.stringify([task["omnigent.ai:source"], task.uid, q.id])}>
              <button
                type="button"
                onClick={() => {
                  const target = tasks.find(
                    (candidate) =>
                      candidate.uid === q.task.uid &&
                      candidate["omnigent.ai:source"] === q.task.source,
                  );
                  if (target) onSelect(target);
                }}
              >
                {"「"}
                {q.text.slice(0, 80)}
                {q.text.length > 80 ? "…" : ""}
                {"」"}
                <cite>
                  {activityTaskName(task)}
                  {q.date ? " · " + q.date : ""}
                </cite>
              </button>
            </blockquote>
          ))}
        </section>
      )}
      <section className="td-p td-who">
        <h3>
          <Network size={16} />
          {t("g6.whoWorked")}
          <em>{t("g6.reportedSessions")}</em>
        </h3>
        {!agents.size && <p className="td-empty">{t("visual.noSessions")}</p>}
        {[...agents]
          .sort((a, b) => b[1].n - a[1].n)
          .slice(0, 6)
          .map(([agent, a]) => (
            <div className="td-a" key={agent}>
              <BoardIdentityMark
                id={agent}
                product={agent}
                label={activityAgentName(agent, agentLabels.get(agent))}
                size={26}
              />
              <b>{activityAgentName(agent, agentLabels.get(agent))}</b>
              <span className="td-agent-count">{activityNumber(a.n)}</span>
              <i className={`td-bar ag-${agent}`}>
                <u style={{ width: `${(a.n / maxN) * 100}%` }} />
              </i>
              <em>
                {t("g6.workerCount", { count: activityNumber(a.n), live: activityNumber(a.live) })}
                {" · "}
                {a.cost !== null ? activityMoney(a.cost) : t("g6.unbilled")}
              </em>
            </div>
          ))}
      </section>
      <section className="td-p td-res">
        <h3>
          <Coins size={18} />
          {t("g6.accountCosts")}
          <em title={t("g6.costFootnote")}>{t("g6.estimatedUsage")}</em>
        </h3>
        {!rows.length && <p className="td-empty">{t("visual.noUsage")}</p>}
        {rows.map((a) => (
          <div className="td-h" key={a.id}>
            <BoardIdentityMark id={a.id} label={a.label} size={20} />
            <b>
              {a.label}
              {a.id.startsWith("native:") ? ` · ${t("g6.nativeUsage")}` : ""}
            </b>
            <i className="td-bar">
              <u
                style={{
                  background: boardIdentityColor(a.id),
                  width: `${((a.amount ?? 0) / maxCost) * 100}%`,
                }}
              />
            </i>
            <span title={t("sessions.cost.estimate.hint")}>
              {a.amount === null ? "—" : activityMoney(a.amount)}
            </span>
          </div>
        ))}
      </section>
      {(!receipts.length || !quotes.length) && (
        <p className="td-missing">
          {t("visual.notReported")}{" "}
          {[!receipts.length ? t("g6.testReceipts") : "", !quotes.length ? t("g6.decisions") : ""]
            .filter(Boolean)
            .join(" · ")}
        </p>
      )}
    </div>
  );
}
