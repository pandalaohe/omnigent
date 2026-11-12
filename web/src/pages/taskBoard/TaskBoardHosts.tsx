import { useState } from "react";
import type { SystemStatusHost } from "@/hooks/useSystemStatus";
import { BoardMeter } from "./TaskBoardQuota";
import { BoardPager } from "./TaskBoardRoutes";
import { formatNumber, t } from "./taskBoardText";

export function hostReading(host: SystemStatusHost) {
  const load = host.state === "online" && host.level !== "unknown" ? host.load_per_core : null;
  const heavy = host.lines?.heavy;
  const ratio = load != null && heavy != null && heavy > 0 ? load / heavy : null;
  return { load, heavy, ratio, memory: host.mem_pct ?? null };
}
export function rankedHosts(hosts: SystemStatusHost[]) {
  return [...hosts].sort((a, b) => (hostReading(b).ratio ?? -1) - (hostReading(a).ratio ?? -1));
}
function HostLoad({ host }: { host: SystemStatusHost }) {
  const reading = hostReading(host);
  return reading.ratio == null ? (
    <small>{t(host.state === "offline" ? "hosts.offline" : "quota.unknown")}</small>
  ) : (
    <BoardMeter value={reading.ratio * 100} warm={reading.ratio >= 0.7} hot={reading.ratio >= 1} />
  );
}
export function TaskBoardHostsBand({
  hosts,
  onOpen,
  loading,
  failed,
  retry,
}: {
  hosts: SystemStatusHost[];
  onOpen: () => void;
  loading: boolean;
  failed: boolean;
  retry: () => void;
}) {
  const worst = rankedHosts(hosts).slice(0, 2);
  return (
    <section className="tb-host-band" aria-label={t("band.hosts")}>
      <h2>
        {t("band.hosts")}
        {hosts.length > 2 && (
          <button type="button" onClick={onOpen}>
            {t("band.more", { count: hosts.length - 2 })}
          </button>
        )}
      </h2>
      {failed ? (
        <button type="button" onClick={retry}>
          {t("band.retryFailure", { message: t("hosts.failed"), retry: t("board.retry") })}
        </button>
      ) : !hosts.length ? (
        <p className="tb-band-empty">{t(loading ? "board.loading" : "hosts.empty")}</p>
      ) : (
        <button type="button" className="tb-hostbox" onClick={onOpen}>
          {worst.map((host) => {
            const { load, heavy, memory } = hostReading(host);
            return (
              <span
                key={host.host_id}
                title={t("hosts.hover", {
                  name: host.name,
                  load: load == null ? t("quota.unknown") : formatNumber(load),
                  heavy: heavy == null ? t("quota.unknown") : formatNumber(heavy),
                  memory: memory == null ? t("quota.unknown") : formatNumber(memory),
                })}
              >
                <b>{host.name}</b>
                <HostLoad host={host} />
              </span>
            );
          })}
        </button>
      )}
    </section>
  );
}
export function TaskBoardHostsPanel({ hosts }: { hosts: SystemStatusHost[] }) {
  const [page, setPage] = useState(0);
  const pages = Math.max(1, Math.ceil(hosts.length / 8)),
    current = Math.min(page, pages - 1);
  return (
    <div className="tb-resource-panel">
      <p>
        {t("hosts.panelHint", {
          count: hosts.filter((host) => (hostReading(host).ratio ?? 0) >= 0.7).length,
          total: hosts.length,
        })}
      </p>
      <div className="tb-host-grid">
        {hosts.slice(current * 8, (current + 1) * 8).map((host) => {
          const { load, heavy, memory } = hostReading(host);
          return (
            <article key={host.host_id}>
              <h3>{host.name}</h3>
              <div className="tb-host-load">
                <b>
                  {load == null
                    ? t(host.state === "offline" ? "hosts.offline" : "quota.unknown")
                    : formatNumber(load)}
                </b>
                <small>
                  {heavy != null && t("hosts.perCore", { threshold: formatNumber(heavy) })}
                </small>
              </div>
              <HostLoad host={host} />
              <small>
                {t("hosts.resources", {
                  cores: host.cpu_count ?? t("quota.unknown"),
                  memory: memory == null ? t("quota.unknown") : formatNumber(memory),
                })}
              </small>
            </article>
          );
        })}
      </div>
      <BoardPager page={current} pages={pages} onChange={setPage} />
    </div>
  );
}
