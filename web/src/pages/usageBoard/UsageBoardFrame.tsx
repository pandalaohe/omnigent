import { boardPath, boardExitProps } from "@/lib/boardFrame";
// Page chrome shared by every Usage board route: the title row, the actions on
// its right, and the loading / failed / off states, so no route renders a
// half-filled page while the reading is still arriving.

import { SettingsIcon } from "lucide-react";
import type { ReactNode } from "react";
import { useLocation, Link } from "@/lib/routing";

import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { t } from "./usageText";
import { formatAge } from "./usageBoardFormat";
import "./usageBoard.css";

interface UsageBoardFrameProps {
  title: ReactNode;
  /** Where the back control goes; the board itself has none. */
  back?: { to: string; label: string };
  actions?: ReactNode;
  loading?: boolean;
  error?: unknown;
  generatedAt?: number;
  children: ReactNode;
}

import { UsageModelPoolsLink } from "./UsageModelPoolsLink";

export function UsageBoardFrame({
  title,
  back,
  actions,
  loading = false,
  error,
  generatedAt,
  children,
}: UsageBoardFrameProps) {
  const location = useLocation();
  const analysis = location.pathname.endsWith("/cost");
  return (
    <div className="usage-board @container">
      <header className="usage-top">
        <h1>{title === "Cost" ? t("boardTitle") : title}</h1>
        <nav className="usage-tabs" aria-label="Usage pages">
          <Link to={boardPath("/usage/board")} aria-current={!analysis ? "page" : undefined}>
            {t("usageTab")}
          </Link>
          <Link to={boardPath("/usage/board/cost")} aria-current={analysis ? "page" : undefined}>
            {t("analysisTab")}
          </Link>
        </nav>
        {generatedAt != null ? (
          <span className="usage-data-age text-xs text-muted-foreground">
            {Date.now() / 1000 - generatedAt < 60
              ? "Data just now"
              : `Data ${formatAge(Math.max(0, Date.now() / 1000 - generatedAt))} ago`}
          </span>
        ) : null}
        <span className="usage-top-actions">
          {location.pathname.endsWith("/usage/board") || analysis ? <UsageModelPoolsLink /> : null}
          {analysis ? (
            <Link
              {...boardExitProps()}
              className="inline-flex min-h-11 items-center px-2 text-xs underline"
              to="/settings/general#usage-settings"
              aria-label="Pricing settings"
            >
              <SettingsIcon className="mr-1 size-3.5" aria-hidden="true" /> Pricing
            </Link>
          ) : null}
          {actions}
        </span>
      </header>
      {back ? (
        <Button variant="ghost" size="sm" asChild>
          <Link to={boardPath(back.to)}>
            <span aria-hidden="true">‹ </span>
            {back.label}
          </Link>
        </Button>
      ) : null}
      {error ? <FailedReading error={error} /> : null}
      {loading ? (
        <div className="flex items-center gap-2 py-10 text-sm text-muted-foreground">
          <Spinner className="size-4" />
          Reading your usage…
        </div>
      ) : null}
      {!loading ? children : null}
    </div>
  );
}

function FailedReading({ error }: { error: unknown }) {
  const message =
    error instanceof Error ? error.message : "The reading could not be fetched from this server.";
  return (
    <div className="mb-3 rounded-[11px] border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm">
      <p className="font-medium text-destructive">Usage could not be read</p>
      <p className="text-xs text-muted-foreground">{message}</p>
    </div>
  );
}
