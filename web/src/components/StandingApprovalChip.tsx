import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ShieldCheckIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { changeStandingApproval, getStandingApproval } from "@/lib/standingApprovalApi";

const kindLabel = (kind: string) =>
  kind === "answer-child-approvals" ? "Answer child approvals" : kind;

export function StandingApprovalChip({
  sessionId,
  readOnly = false,
}: {
  sessionId: string;
  readOnly?: boolean;
}) {
  const client = useQueryClient();
  const query = useQuery({
    queryKey: ["standing-approval", sessionId],
    queryFn: ({ signal }) => getStandingApproval(sessionId, signal),
    retry: false,
  });
  const [error, setError] = useState<string | null>(null);
  const change = useMutation({
    mutationFn: ({ op, kind }: { op: "revoke" | "grant"; kind: string }) =>
      changeStandingApproval(sessionId, op, kind),
    onMutate: () => client.cancelQueries({ queryKey: ["standing-approval", sessionId] }),
    onSuccess: async (state) => {
      await client.cancelQueries({ queryKey: ["standing-approval", sessionId] });
      client.setQueryData(["standing-approval", sessionId], state);
      setError(null);
    },
    onError: (reason) =>
      setError(
        reason instanceof Error
          ? reason.message
          : "Could not change the standing approval. Try again.",
      ),
  });
  const state = query.data;
  const record = state?.record;
  const inherited = state !== undefined && state.root_id !== sessionId;
  const editable = !readOnly && !inherited;
  const revoked = record
    ? [
        ...new Set(
          record.events.filter((event) => event.op === "revoke").flatMap((event) => event.kinds),
        ),
      ].filter((kind) => !record.kinds.includes(kind))
    : [];
  const summary = query.isError
    ? "unavailable"
    : !record
      ? "loading"
      : record.kinds.length
        ? `${record.kinds.length} active`
        : "none";
  return (
    <Popover>
      <Tooltip>
        <TooltipTrigger asChild>
          <PopoverTrigger asChild>
            <button
              type="button"
              aria-label={`Standing approval: ${summary}`}
              className="flex h-7 shrink-0 items-center gap-1 rounded-full border border-border bg-background px-2 text-xs text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <ShieldCheckIcon className="size-3.5" aria-hidden="true" />
              <span>{record ? record.kinds.length : "…"}</span>
            </button>
          </PopoverTrigger>
        </TooltipTrigger>
        <TooltipContent>Standing approval: {summary}</TooltipContent>
      </Tooltip>
      <PopoverContent align="end" className="w-80 max-w-[calc(100vw-1rem)] p-4">
        <h2 className="font-medium">Standing approval</h2>
        <p className="text-xs text-muted-foreground">
          {inherited
            ? "Inherited from the parent session. Read-only here."
            : "Applies to this parent session and its children."}
        </p>
        {query.isError ? (
          <div role="alert">
            <p>Could not load the standing approval.</p>
            <Button type="button" variant="outline" size="sm" onClick={() => void query.refetch()}>
              Retry loading this approval
            </Button>
          </div>
        ) : !record ? (
          <p role="status">Loading standing approval…</p>
        ) : (
          <>
            {record.kinds.length === 0 && <p>No active kinds.</p>}
            {record.kinds.map((kind) => (
              <div key={kind} className="flex min-w-0 items-center justify-between gap-2">
                <span className="min-w-0 break-words">{kindLabel(kind)}</span>
                {editable && (
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    disabled={change.isPending}
                    aria-label={`Revoke ${kindLabel(kind)}`}
                    onClick={() => {
                      setError(null);
                      change.mutate({ op: "revoke", kind });
                    }}
                  >
                    Revoke
                  </Button>
                )}
              </div>
            ))}
            {revoked.map((kind) => (
              <div key={kind} className="flex min-w-0 items-center justify-between gap-2">
                <span className="min-w-0 break-words text-muted-foreground">
                  {kindLabel(kind)} (revoked)
                </span>
                {editable && (
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    disabled={change.isPending}
                    aria-label={`Grant back ${kindLabel(kind)}`}
                    onClick={() => {
                      setError(null);
                      change.mutate({ op: "grant", kind });
                    }}
                  >
                    Grant back
                  </Button>
                )}
              </div>
            ))}
            {record.exceptions.length > 0 && (
              <div className="space-y-1 border-t border-border pt-2">
                <h3 className="font-medium">Exceptions</h3>
                {record.exceptions.map((exception) => (
                  <p key={exception} className="break-words text-xs text-muted-foreground">
                    {exception}
                  </p>
                ))}
              </div>
            )}
            <p className="text-xs text-muted-foreground">
              Actions that always require approval remain subject to approval.
            </p>
          </>
        )}
        {change.isPending && (
          <p role="status">
            {change.variables.op === "grant"
              ? "Waiting for approval in this conversation…"
              : "Revoking…"}
          </p>
        )}
        {error && (
          <p role="alert" className="break-words text-destructive">
            {error}
          </p>
        )}
      </PopoverContent>
    </Popover>
  );
}
