import type { TaskDisplay } from "./taskBoardDisplay";
import { safeSourceLink } from "./taskBoardData";
import { t } from "./taskBoardText";
import { isBlankDetailValue } from "./taskBoardPresentation";
import { TaskBoardFoldText } from "./TaskBoardFoldText";

export function TaskSourceDetails({ display }: { display?: TaskDisplay }) {
  const rows = display?.rows?.filter((row) => !isBlankDetailValue(row.value)) ?? [];
  if (!display || !(display.badges?.length || rows.length || display.links?.length)) return null;
  return (
    <section
      aria-label={t("detail.sourceDetails")}
      className="mt-4 space-y-3 border-t border-border pt-4"
    >
      <h4>{t("detail.sourceDetails")}</h4>
      {!!display.badges?.length && (
        <ul className="flex flex-wrap gap-2">
          {display.badges.map((badge) => (
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
        <dl className="space-y-2">
          {rows.map((row) => (
            <div key={row.id} className="min-w-0">
              <dt className="text-xs text-muted-foreground">{row.label}</dt>
              <dd>
                <TaskBoardFoldText key={row.value} value={row.value} />
              </dd>
            </div>
          ))}
        </dl>
      )}
      {!!display.links?.length && (
        <ul className="space-y-1">
          {display.links.map((link) => {
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
      )}
    </section>
  );
}
