import { boardPath } from "@/lib/boardFrame";
// Page chrome shared by every Usage board route: the title row, the actions on
// its right, and the loading / failed / off states, so no route renders a
// half-filled page while the reading is still arriving.

import type { ReactNode } from "react";
import { Link } from "@/lib/routing";

import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";

interface UsageBoardFrameProps {
  title: ReactNode;
  /** Where the back control goes; the board itself has none. */
  back?: { to: string; label: string };
  actions?: ReactNode;
  loading?: boolean;
  error?: unknown;
  children: ReactNode;
}

export function UsageBoardFrame({
  title,
  back,
  actions,
  loading = false,
  error,
  children,
}: UsageBoardFrameProps) {
  return (
    <div className="@container mx-auto w-full max-w-[1120px] px-4 pb-16 pt-4 sm:px-6">
      <div className="mb-2.5 flex min-h-9 flex-wrap items-center gap-2">
        {back ? (
          <Button variant="ghost" size="sm" asChild className="-ml-2 gap-1">
            <Link to={boardPath(back.to)}>
              <span aria-hidden="true">‹</span>
              {back.label}
            </Link>
          </Button>
        ) : null}
        <span className="flex-1" />
        {actions}
      </div>
      <h1 className="mb-3 text-2xl font-semibold tracking-tight">{title}</h1>
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
