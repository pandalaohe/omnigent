import { useIsMutating, useMutation, useQueryClient } from "@tanstack/react-query";
import { RefreshCwIcon } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { childSessionsQueryKey } from "@/hooks/useChildSessions";
import { authenticatedFetch } from "@/lib/identity";

interface ReconciliationResult {
  corrected: number;
  unchanged: number;
  unverified: number;
}

export async function reconcileSubagents(sessionId: string): Promise<ReconciliationResult> {
  const response = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(sessionId)}/child_sessions/reconcile`,
    { method: "POST" },
  );
  if (!response.ok) {
    if (response.status === 404 || response.status === 501) {
      throw new Error("Update the Server and Host to recheck agent status.");
    }
    if (response.status === 503) {
      throw new Error("Could not verify agent status. Check that the Host is online and updated.");
    }
    throw new Error("Could not recheck agent status. Please try again.");
  }
  const result: unknown = await response.json();
  if (
    !result ||
    typeof result !== "object" ||
    !["corrected", "unchanged", "unverified"].every((key) => {
      const value = (result as Record<string, unknown>)[key];
      return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
    })
  ) {
    throw new Error("The Server returned an invalid status check. Please refresh and try again.");
  }
  return result as ReconciliationResult;
}

export function ReconcileSubagentsButton({
  rootSessionId,
  childIds,
}: {
  rootSessionId: string;
  childIds: string[];
}) {
  const queryClient = useQueryClient();
  const pending = useIsMutating({ mutationKey: ["reconcile-subagents", rootSessionId] }) > 0;
  const reconciliation = useMutation({
    mutationKey: ["reconcile-subagents", rootSessionId],
    mutationFn: () => reconcileSubagents(rootSessionId),
    retry: false,
    onSuccess: (result) => {
      const message = `${result.corrected} corrected · ${result.unchanged} unchanged · ${result.unverified} not verified`;
      if (result.unverified > 0) {
        toast.info(message, { description: "Unverified agents keep their current state." });
      } else {
        toast.success(message);
      }
    },
    onError: (error) => toast.error(error.message),
    onSettled: async () => {
      await Promise.all([
        ...[rootSessionId, ...childIds].flatMap((id) => [
          queryClient.invalidateQueries({ queryKey: childSessionsQueryKey(id) }),
          queryClient.invalidateQueries({ queryKey: ["session", id] }),
        ]),
        queryClient.invalidateQueries({ queryKey: ["conversations"] }),
        queryClient.invalidateQueries({ queryKey: ["project-sessions"] }),
      ]);
    },
  });
  return (
    <Button
      type="button"
      variant="ghost"
      size="icon-xs"
      className="size-9 sm:size-6"
      aria-label="Recheck agent status"
      title="Recheck agent status"
      aria-busy={pending}
      disabled={pending}
      onClick={() => reconciliation.mutate()}
    >
      <RefreshCwIcon className={pending ? "size-3.5 animate-spin" : "size-3.5"} />
    </Button>
  );
}
