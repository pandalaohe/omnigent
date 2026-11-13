import type { BoardTask } from "./taskBoardData";
import type { BoardSession } from "./taskBoardNative";
import { boardSessionRows } from "./taskBoardSessionData";
import { accountTotals } from "./taskBoardHistoryData";
import { latestRelease, type SourceCache } from "./taskBoardSnapshotData";
import { localDay } from "./taskBoardActivityData";
import { t } from "./taskBoardText";
import { BoardIdentityMark } from "./BoardIdentityMark";
import {
  activityNumber,
  activityTaskName,
  activityAgentName,
  activitySourceName,
  activityStamp,
  activityMoney,
} from "./TaskBoardActivityParts";
import type { UsageActivity } from "@/pages/usageBoard/usageActivityData";
import { Coins, FlaskConical, Network, PackageCheck, Quote } from "lucide-react";

export function TaskBoardToday({
  tasks,
  sessions,
  caches,
  now,
  onSelect,
  usage,
  sourceLabels,
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
    sourceName = (source: string, task?: BoardTask) =>
      activitySourceName(source, task, sourceLabels),
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
  const bySource = new Map<string, number>();
  commits.forEach((c) => bySource.set(c.source, (bySource.get(c.source) ?? 0) + 1));
  const sources = [...bySource].sort((a, b) => b[1] - a[1]);
  const topSources =
    sources.length <= 6
      ? sources
      : [
          ...sources.slice(0, 5),
          [
            t("g6.otherCount", { count: activityNumber(sources.length - 5) }),
            sources.slice(5).reduce((sum, v) => sum + v[1], 0),
          ] as [string, number],
        ];
  const release = latestRelease(caches),
    hosts = [...new Set(Object.values(caches).flatMap((c) => c.display.checkHosts ?? []))].slice(
      0,
      2,
    );
  const receipts = tasks
    .filter(
      (task) =>
        task["omnigent.ai:sourceDisplay"]?.historyState?.active !== false &&
        task["omnigent.ai:sourceDisplay"]?.checks?.some((c) => hosts.includes(c.host)),
    )
    .sort(
      (a, b) =>
        Date.parse(b["omnigent.ai:sourceDisplay"]?.lastActivity ?? b.updated ?? "") -
        Date.parse(a["omnigent.ai:sourceDisplay"]?.lastActivity ?? a.updated ?? ""),
    );
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
    <div className="td">
      <section className="td-p td-ship">
        <h3>
          <PackageCheck size={16} />
          {t("g6.landed")}
          <em>{t("g6.reportedGit")}</em>
        </h3>
        <div className="td-stats">
          <div>
            <b>{activityNumber(closed.length)}</b>
            <span>{t("today.closed")}</span>
          </div>
          <div>
            <b>{activityNumber(commits.size)}</b>
            <span title={t("g6.taskCommitsHint")}>{t("g6.taskCommits")}</span>
          </div>
          <div>
            <b>{release?.name ?? "—"}</b>
            <span>
              {t("g6.latestRelease")}
              {release ? `, ${activityStamp(Date.parse(release.publishedAt))}` : ""}
            </span>
          </div>
        </div>
        <div className="td-roots">
          {topSources.map(([source, n]) => (
            <span key={source}>
              <i style={{ width: `${Math.max(4, (n / Math.max(1, commits.size)) * 100)}%` }} />
              <b>{sourceName(source)}</b>
              <em>{activityNumber(n)}</em>
            </span>
          ))}
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
                {task.title || activityTaskName(task)}
              </button>
            </li>
          ))}
        </ul>
      </section>
      <section className="td-p td-rcp">
        <h3>
          <FlaskConical size={16} />
          {t("g6.testReceipts")}
          <em>{t("g6.reportedChecks")}</em>
        </h3>
        <table>
          <thead>
            <tr>
              <th>{t("g6.task")}</th>
              {hosts.map((host) => (
                <th key={host}>{host}</th>
              ))}
              <th>{t("g6.lastCheck")}</th>
            </tr>
          </thead>
          <tbody>
            {receipts.slice(0, 11).map((task) => (
              <tr key={JSON.stringify([task["omnigent.ai:source"], task.uid])}>
                <th>
                  <button type="button" onClick={() => onSelect(task)}>
                    <b>{activityTaskName(task)}</b>
                  </button>
                </th>
                {hosts.map((host) => {
                  const check = task["omnigent.ai:sourceDisplay"]?.checks
                    ?.filter((c) => c.host === host)
                    .sort((a, b) => Date.parse(b.at) - Date.parse(a.at))[0];
                  return (
                    <td key={host} className={`v-${check?.verdict ?? "none"}`}>
                      <i />
                      {check ? t("sessions.check." + check.verdict) : t("g6.noReceipt")}
                      {check && <small>{activityNumber(check.count)}</small>}
                    </td>
                  );
                })}
                <td className="td-t">
                  {activityStamp(
                    Math.max(
                      ...task["omnigent.ai:sourceDisplay"]!.checks!.map((c) => Date.parse(c.at)),
                    ),
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
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
      <section className="td-p td-who">
        <h3>
          <Network size={16} />
          {t("g6.whoWorked")}
          <em>{t("g6.reportedSessions")}</em>
        </h3>
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
              <i className="td-bar">
                <u style={{ width: `${(a.n / maxN) * 100}%` }} />
              </i>
              <em>
                {t("g6.workerCount", { count: activityNumber(a.n), live: activityNumber(a.live) })}
                {" · "}
                {a.cost ? activityMoney(a.cost) : t("g6.unbilled")}
              </em>
            </div>
          ))}
      </section>
      <section className="td-p td-res">
        <h3>
          <Coins size={18} />
          {t("g6.accountCosts")}
          <em>{t("g6.estimatedUsage")}</em>
        </h3>
        {rows.map((a) => (
          <div className="td-h" key={a.id}>
            <BoardIdentityMark id={a.id} label={a.label} size={20} />
            <b>
              {a.label}
              {a.id.startsWith("native:") ? ` · ${t("g6.nativeUsage")}` : ""}
            </b>
            <i className="td-bar">
              <u style={{ width: `${((a.amount ?? 0) / maxCost) * 100}%` }} />
            </i>
            <span title={t("sessions.cost.estimate.hint")}>
              {a.amount === null ? "—" : activityMoney(a.amount)}
            </span>
          </div>
        ))}
        <p className="muted td-foot">{t("g6.costFootnote")}</p>
      </section>
    </div>
  );
}
