import { useEffect, useState } from "react";
import { Copy, Check, FileText } from "lucide-react";
import { boardFiles } from "./taskBoardVisual";
import type { TaskDisplay } from "./taskBoardDisplay";
import { safeSourceLink } from "./taskBoardData";
import { t, formatDate } from "./taskBoardText";
import { isBlankDetailValue } from "./taskBoardPresentation";
import { TaskBoardFoldText } from "./TaskBoardFoldText";
import { Input } from "@/components/ui/input";

export function TaskSourceDetails({
  display,
  hasParent = false,
}: {
  display?: TaskDisplay;
  hasParent?: boolean;
}) {
  const [oldFiles, setOldFiles] = useState(false);
  const [fileQuery, setFileQuery] = useState("");
  const [linkQuery, setLinkQuery] = useState("");
  const files = boardFiles(display?.rows ?? []),
    current = files.filter((file) => !file.old),
    old = files.filter((file) => file.old);
  const visibleCurrent = current.filter((file) =>
    file.path.toLowerCase().includes(fileQuery.trim().toLowerCase()),
  );
  const visibleOld = old.filter((file) =>
    file.path.toLowerCase().includes(fileQuery.trim().toLowerCase()),
  );
  const visibleLinks = (display?.links ?? []).filter((link) =>
    `${link.label} ${link.href}`.toLowerCase().includes(linkQuery.trim().toLowerCase()),
  );
  const badges = display?.badges?.filter((badge) => !["state", "series"].includes(badge.id)) ?? [];
  const rows =
    display?.rows?.filter(
      (row) =>
        !isBlankDetailValue(row.value) &&
        !/^path(?:[._-]|$)/.test(row.id) &&
        row.id !== "event_age" &&
        !(
          row.id === "parent" &&
          (hasParent || display.relations?.some((edge) => edge.kind === "parent"))
        ),
    ) ?? [];
  if (!display || !(badges.length || rows.length || files.length || display.links?.length))
    return null;
  return (
    <section aria-label={t("detail.sourceDetails")} className="tb-source-details">
      <h4>{t("detail.sourceDetails")}</h4>
      {!!files.length && (
        <div className="tb-files">
          <h5>
            {t("visual.files")} <small>{current.length}</small>
          </h5>
          <Input
            aria-label="Search files"
            placeholder="Search files"
            value={fileQuery}
            onChange={(event) => setFileQuery(event.target.value)}
          />
          <p className="text-xs text-muted-foreground">
            Showing {visibleCurrent.length} of {current.length}
          </p>
          <div className="max-h-64 overflow-auto">
            {visibleCurrent.map((file) => (
              <FilePath key={file.path} file={file} />
            ))}
          </div>
          {old.length > 0 && (
            <>
              <button type="button" aria-expanded={oldFiles} onClick={() => setOldFiles(!oldFiles)}>
                {t("visual.olderFiles", { count: old.length })}
              </button>
              {oldFiles && (
                <>
                  <p className="text-xs text-muted-foreground">
                    Showing {visibleOld.length} of {old.length}
                  </p>
                  <div className="max-h-64 overflow-auto">
                    {visibleOld.map((file) => (
                      <FilePath key={file.path} file={file} />
                    ))}
                  </div>
                </>
              )}
            </>
          )}
        </div>
      )}
      {!!badges.length && (
        <ul className="flex flex-wrap gap-2">
          {badges.map((badge) => (
            <li
              key={badge.id}
              data-tone={badge.tone ?? "neutral"}
              className="break-words rounded border border-border px-2 py-1 text-xs"
            >
              {badge.label}
            </li>
          ))}
        </ul>
      )}
      {!!rows.length && (
        <dl className="tb-source-fields">
          {rows.map((row) => (
            <div key={row.id} className="min-w-0">
              <dt className="text-xs text-muted-foreground">{row.label}</dt>
              <dd className={row.id === "task_id" ? "tb-source-id" : undefined}>
                <TaskBoardFoldText
                  key={row.value}
                  value={row.value.replace(/\d{4}-\d\d-\d\dT[\d:.]+Z/g, (value) =>
                    formatDate(Date.parse(value), {
                      month: "2-digit",
                      day: "2-digit",
                      hour: "2-digit",
                      minute: "2-digit",
                      hourCycle: "h23",
                    }),
                  )}
                />
              </dd>
            </div>
          ))}
        </dl>
      )}
      {!!display.links?.length && (
        <div>
          <Input
            aria-label="Search links"
            placeholder="Search links"
            value={linkQuery}
            onChange={(event) => setLinkQuery(event.target.value)}
          />
          <p className="text-xs text-muted-foreground">
            Showing {visibleLinks.length} of {display.links.length}
          </p>
          <ul className="max-h-64 space-y-1 overflow-auto">
            {visibleLinks.map((link) => {
              const href = safeSourceLink(link.href);
              return (
                <li key={link.id} className="break-words">
                  {href ? (
                    <a
                      href={href}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="underline underline-offset-2"
                    >
                      {link.label}
                    </a>
                  ) : (
                    <span>{link.label}</span>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </section>
  );
}

function FilePath({ file }: { file: ReturnType<typeof boardFiles>[number] }) {
  const [result, setResult] = useState<"idle" | "copied" | "failed">("idle");
  useEffect(() => {
    if (result !== "copied") return;
    const timer = setTimeout(() => setResult("idle"), 1500);
    return () => clearTimeout(timer);
  }, [result]);
  return (
    <button
      type="button"
      className={`tb-file ${result}`}
      title={file.path}
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(file.path);
          setResult("copied");
        } catch {
          setResult("failed");
        }
      }}
    >
      <FileText size={13} />
      <span className="tb-file-kind">{file.kind}</span>
      <span className="tb-file-name">{file.name}</span>
      {file.old ? <small>{file.version}</small> : file.showExt ? <small>{file.ext}</small> : null}
      {result === "copied" ? <Check size={13} /> : <Copy size={13} />}
      <span role="status">
        {t(
          result === "copied"
            ? "visual.copied"
            : result === "failed"
              ? "visual.copyFailed"
              : "visual.copyPath",
        )}
      </span>
    </button>
  );
}
