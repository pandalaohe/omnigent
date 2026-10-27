import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ShieldCheckIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { changeStandingPass, getStandingPass } from "@/lib/standingPassApi";

export function StandingPassChip({
  sessionId,
  readOnly = false,
}: {
  sessionId: string;
  readOnly?: boolean;
}) {
  const client = useQueryClient();
  const query = useQuery({
    queryKey: ["standing-pass", sessionId],
    queryFn: ({ signal }) => getStandingPass(sessionId, signal),
    retry: false,
  });
  const [error, setError] = useState<string | null>(null);
  const change = useMutation({
    mutationFn: ({ op, kind }: { op: "revoke" | "grant"; kind: string }) =>
      changeStandingPass(sessionId, op, kind),
    onMutate: () => client.cancelQueries({ queryKey: ["standing-pass", sessionId] }),
    onSuccess: async (state) => {
      await client.cancelQueries({ queryKey: ["standing-pass", sessionId] });
      client.setQueryData(["standing-pass", sessionId], state);
      setError(null);
    },
    onError: (reason) =>
      setError(
        reason instanceof Error ? reason.message : "Could not change the standing pass. Try again.",
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
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label={`Standing pass: ${summary}`}
          className="flex h-7 shrink-0 items-center gap-1 rounded-full border border-border bg-background px-2 text-xs text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <ShieldCheckIcon className="size-3.5" />
          <span className="hidden sm:inline">Pass</span>
          <span>{record ? record.kinds.length : "…"}</span>
        </button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-80 max-w-[calc(100vw-1rem)] p-4">
        <h2 className="font-medium">Standing pass</h2>
        <p className="text-xs text-muted-foreground">
          {inherited
            ? "Inherited from the parent session. Read-only here."
            : "Applies to this parent session and its children."}
        </p>
        {query.isError ? (
          <div role="alert">
            <p>Could not load the standing pass.</p>
            <Button type="button" variant="outline" size="sm" onClick={() => void query.refetch()}>
              Retry
            </Button>
          </div>
        ) : !record ? (
          <p role="status">Loading standing pass…</p>
        ) : (
          <>
            {record.kinds.length === 0 && <p>No active kinds.</p>}
            {record.kinds.map((kind) => (
              <div key={kind} className="flex min-w-0 items-center justify-between gap-2">
                <span className="min-w-0 break-words">{kind}</span>
                {editable && (
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    disabled={change.isPending}
                    aria-label={`Revoke ${kind}`}
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
                <span className="min-w-0 break-words text-muted-foreground">{kind} (revoked)</span>
                {editable && (
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    disabled={change.isPending}
                    aria-label={`Grant back ${kind}`}
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
