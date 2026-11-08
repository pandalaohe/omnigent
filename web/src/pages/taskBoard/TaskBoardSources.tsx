import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { SettingsIcon, Trash2Icon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { authenticatedFetch } from "@/lib/identity";
import { safeSourceLink, type TaskSource } from "./taskBoardData";
import type { BoardPreferences } from "./taskBoardPreferences";
import { BoardDate } from "./TaskBoardDetail";
import { formatNumber, t } from "./taskBoardText";

export function TaskBoardSources({
  sources,
  preferences,
  update,
}: {
  sources: TaskSource[];
  preferences: BoardPreferences;
  update: (change: Partial<BoardPreferences>) => boolean | void;
}) {
  const client = useQueryClient();
  const [confirm, setConfirm] = useState<string | null>(null),
    [busy, setBusy] = useState(false),
    [message, setMessage] = useState("");
  async function remove(source: TaskSource) {
    setBusy(true);
    try {
      const response = await authenticatedFetch(
        `/v1/me/tasks/sources/${encodeURIComponent(source.id)}`,
        { method: "DELETE" },
      );
      if (!response.ok && response.status !== 404) throw new Error("Source deletion failed");
      setConfirm(null);
      setMessage(
        t("sources.deleted", { name: preferences.aliases[source.id] || source.name || source.id }),
      );
      await client.invalidateQueries({ queryKey: ["taskBoard"] });
    } catch {
      setMessage(t("sources.deleteFailed"));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="tb-sources" aria-label={t("view.sources")}>
      <h2>
        <SettingsIcon className="size-4" />
        {t("view.sources")}
      </h2>
      {message && <p role="status">{message}</p>}
      {!sources.length && <p className="tb-empty">{t("sources.empty")}</p>}
      {sources.map((source) => {
        const name = preferences.aliases[source.id] || source.name || source.id,
          counts = source.counts ?? { open: 0, closed: 0, gone: 0 },
          href = safeSourceLink(source.source);
        return (
          <article key={source.id} className="tb-source">
            <header>
              <h3>{name}</h3>
              {href && (
                <a href={href} target="_blank" rel="noreferrer">
                  {t("sources.open")}
                </a>
              )}
            </header>
            <div className="tb-source-controls">
              <label>
                {t("sources.alias")}
                <input
                  aria-label={t("sources.aliasFor", { name: source.name || source.id })}
                  value={preferences.aliases[source.id] ?? ""}
                  placeholder={source.name || source.id}
                  onChange={(e) => {
                    const saved = update({
                      aliases: { ...preferences.aliases, [source.id]: e.target.value },
                    });
                    setMessage(t(saved === false ? "sources.saveFailed" : "sources.saved"));
                  }}
                />
              </label>
              <label>
                <input
                  type="checkbox"
                  checked={!preferences.hiddenSources.includes(source.id)}
                  onChange={(e) => {
                    const saved = update({
                      hiddenSources: e.target.checked
                        ? preferences.hiddenSources.filter((id) => id !== source.id)
                        : [...preferences.hiddenSources, source.id],
                    });
                    setMessage(t(saved === false ? "sources.saveFailed" : "sources.saved"));
                  }}
                />
                {t("sources.visible")}
              </label>
            </div>
            <p>
              {t("sources.counts", { open: counts.open, closed: counts.closed, gone: counts.gone })}
            </p>
            <div className="tb-write">
              <span>{t("sources.lastWrite")}</span>
              {source.last_write ? (
                <>
                  <BoardDate value={source.last_write.time} />
                  <span>
                    {t("sources.outcome", {
                      applied: source.last_write.applied ?? 0,
                      unchanged: source.last_write.unchanged ?? 0,
                      held: source.last_write.held ?? 0,
                      gone: source.last_write.gone ?? 0,
                      history: source.last_write.history_added ?? 0,
                    })}
                  </span>
                </>
              ) : (
                <span>{t("sources.never")}</span>
              )}
            </div>
            {!!source.issues?.length && (
              <ul>
                {source.issues.map((issue, i) => (
                  <li key={`${issue.code}:${formatNumber(i)}`}>
                    {t(`issue.${issue.code}`, { ...issue.params, path: issue.path ?? "" })}
                    {issue.path && <code className="ml-2">{issue.path}</code>}
                  </li>
                ))}
              </ul>
            )}
            <details>
              <summary>{t("sources.connect")}</summary>
              <div className="tb-connect">
                <code>
                  {t("sources.report", {
                    url: `${window.location.origin}/v1/me/tasks/sources/${encodeURIComponent(source.id)}`,
                  })}
                </code>
                <code>{t("sources.header")}</code>
                <a
                  href="https://www.rfc-editor.org/rfc/rfc8984.html"
                  target="_blank"
                  rel="noreferrer"
                >
                  {t("sources.docs")}
                </a>
              </div>
            </details>
            {confirm === source.id ? (
              <div className="tb-delete-confirm" role="alert">
                <p>
                  {t("sources.confirm", { name, count: counts.open + counts.closed + counts.gone })}
                </p>
                <p>{t("sources.irreversible")}</p>
                <div className="flex gap-2">
                  <Button variant="destructive" disabled={busy} onClick={() => void remove(source)}>
                    {t(busy ? "sources.deleting" : "sources.delete")}
                  </Button>
                  <Button variant="outline" disabled={busy} onClick={() => setConfirm(null)}>
                    {t("sources.cancel")}
                  </Button>
                </div>
              </div>
            ) : (
              <Button
                variant="destructive"
                size="sm"
                onClick={() => {
                  setConfirm(source.id);
                  setMessage("");
                }}
              >
                <Trash2Icon className="size-4" />
                {t("sources.delete")}
              </Button>
            )}
          </article>
        );
      })}
      {!sources.length && (
        <a href="https://www.rfc-editor.org/rfc/rfc8984.html" target="_blank" rel="noreferrer">
          {t("sources.docs")}
        </a>
      )}
    </section>
  );
}
