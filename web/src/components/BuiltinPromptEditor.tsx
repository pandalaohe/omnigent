import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { authenticatedFetch } from "@/lib/identity";
import { apiErrorFromResponse } from "@/lib/sessionsApi";

interface BuiltinPrompt {
  text: string;
  builtin_text: string;
  base_sha256: string;
  saved_base_sha256: string | null;
  customized: boolean;
  builtin_changed: boolean;
}

async function requestPrompt(
  agentId: string,
  patch?: { text: string | null; base_sha256: string },
) {
  const response = await authenticatedFetch(`/v1/agents/${encodeURIComponent(agentId)}/prompt`, {
    ...(patch
      ? {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(patch),
        }
      : {}),
  });
  if (!response.ok) throw await apiErrorFromResponse(response);
  return (await response.json()) as BuiltinPrompt;
}

export function BuiltinPromptEditor({ agentId }: { agentId: string }) {
  const client = useQueryClient();
  const key = ["builtin-agent-prompt", agentId];
  const prompt = useQuery({ queryKey: key, queryFn: () => requestPrompt(agentId) });
  const [draft, setDraft] = useState<string | null>(null);
  const [undo, setUndo] = useState<{ text: string; base_sha256: string } | null>(null);
  const [status, setStatus] = useState("");
  const mutation = useMutation({
    mutationFn: (patch: { text: string | null; base_sha256: string }) =>
      requestPrompt(agentId, patch),
    onSuccess: (data) => {
      client.setQueryData(key, data);
      setDraft(null);
    },
  });
  const data = prompt.data;
  if (prompt.isPending)
    return (
      <p role="status" className="text-sm">
        Loading starting prompt…
      </p>
    );
  if (prompt.isError)
    return (
      <p role="alert" className="text-sm text-destructive">
        Could not load starting prompt.{" "}
        <Button variant="link" onClick={() => void prompt.refetch()}>
          Retry
        </Button>
      </p>
    );
  if (!data) return null;
  return (
    <section aria-label="Starting prompt" className="min-w-0 space-y-3">
      <label className="block space-y-1.5 text-sm">
        <span className="font-medium">Starting prompt</span>
        <Textarea
          value={draft ?? data.text}
          maxLength={262144}
          disabled={mutation.isPending}
          onChange={(event) => setDraft(event.target.value)}
          className="min-h-32 font-mono text-xs"
        />
      </label>
      <p className="text-xs text-muted-foreground">
        New chats use this. A running chat picks it up after it reconnects.
      </p>
      {data.builtin_changed && (
        <p className="text-xs">The built-in changed since you edited this.</p>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <Button
          size="sm"
          disabled={mutation.isPending || draft === null || draft === data.text}
          onClick={async () => {
            await mutation
              .mutateAsync({ text: draft ?? data.text, base_sha256: data.base_sha256 })
              .then(() => {
                setUndo(null);
                setStatus("Starting prompt saved.");
              })
              .catch(() => {});
          }}
        >
          Save prompt
        </Button>
        {data.customized && (
          <Button
            variant="outline"
            size="sm"
            disabled={mutation.isPending}
            onClick={async () => {
              const previous = {
                text: data.text,
                base_sha256: data.saved_base_sha256 ?? data.base_sha256,
              };
              await mutation
                .mutateAsync({ text: null, base_sha256: data.base_sha256 })
                .then(() => {
                  setUndo(previous);
                  setStatus("Built-in restored.");
                })
                .catch(() => {});
            }}
          >
            Restore built-in
          </Button>
        )}
        {undo && (
          <Button
            variant="link"
            size="sm"
            disabled={mutation.isPending}
            onClick={async () => {
              await mutation
                .mutateAsync(undo)
                .then(() => {
                  setUndo(null);
                  setStatus("Starting prompt restored.");
                })
                .catch(() => {});
            }}
          >
            Undo
          </Button>
        )}
        <span role="status" className="text-xs text-muted-foreground">
          {status}
        </span>
      </div>
      {mutation.isError && (
        <p role="alert" className="text-sm text-destructive">
          {mutation.error.message}
        </p>
      )}
    </section>
  );
}
