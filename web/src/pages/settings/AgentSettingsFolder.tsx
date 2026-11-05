import { useId, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FolderIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { type Host, useHostModelOptions } from "@/hooks/useHosts";
import {
  agentFolderQueryKey,
  requestAgentFolderSettings,
  type AgentFolderChange,
} from "@/lib/agentFolderSettings";
import { copyText } from "@/lib/clipboard";
import { revealFile, supportsFileReveal } from "@/lib/nativeBridge";
import { ApiError } from "@/lib/sessionsApi";
import { revealLabel, useDesktopHostId } from "@/shell/RevealInFileManager";
import { WorkspacePickerDialog } from "@/shell/WorkspacePickerDialog";

const LABELS: Record<string, string> = {
  model: "Model",
  effortLevel: "Thinking",
  model_reasoning_effort: "Thinking",
  defaultThinkingLevel: "Thinking",
  "permissions.defaultMode": "Permissions",
  approval_policy: "Approval policy",
  sandbox_mode: "Sandbox",
  defaultProvider: "Provider",
  defaultModel: "Model",
  agentMode: "Agent mode",
  toolPermission: "Tool permission",
  "auth.type": "Authentication",
};

function failure(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 404 || error.status === 501)
      return "Update this host to see its settings.";
    if (error.status === 409)
      return error.message.startsWith("Changed since. Not undone.")
        ? "Changed since. Not undone."
        : "Changed since. Review the current values and try again.";
    if (error.status === 400) return error.message;
  }
  return "Couldn't reach the settings folder. Try again.";
}

export function AgentSettingsFolder({ host, harness }: { host: Host; harness: string }) {
  // A target change discards drafts and operation notices, including in-flight results.
  return <FolderCard key={`${host.host_id}:${harness}`} host={host} harness={harness} />;
}

function FolderCard({ host, harness }: { host: Host; harness: string }) {
  const client = useQueryClient();
  const online = host.status === "online";
  const queryKey = agentFolderQueryKey(host.host_id, harness);
  const query = useQuery({
    queryKey,
    queryFn: () => requestAgentFolderSettings(host.host_id, harness),
    enabled: online,
    retry: false,
  });
  const view = query.data;
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [choosing, setChoosing] = useState(false);
  const [notice, setNotice] = useState<{ text: string; error?: boolean } | null>(null);
  const localHostId = useDesktopHostId();
  const models = useHostModelOptions(host.host_id, harness, online && editing === "model", {
    poll: false,
  });
  const listId = useId();
  const operation = useMutation({
    mutationFn: (change: AgentFolderChange) =>
      requestAgentFolderSettings(host.host_id, harness, change),
    onSuccess: (next, change) => {
      client.setQueryData(queryKey, next);
      client.setQueriesData(
        {
          queryKey: ["agent-folder-settings", host.host_id],
          predicate: (cachedQuery) =>
            (cachedQuery.state.data as { family?: string } | undefined)?.family === next.family,
        },
        next,
      );
      setEditing(null);
      if (
        change.action === "write" &&
        !next.fields.some(
          (field) => field.key === change.key && field.present && field.value === change.value,
        )
      ) {
        setNotice({
          text: "The agent didn't keep this value. Current settings are shown.",
          error: true,
        });
        return;
      }
      setNotice({
        text:
          change.action === "root"
            ? "Settings folder changed."
            : change.action === "undo"
              ? `${LABELS[change.key] ?? change.key} restored.`
              : `${LABELS[change.key] ?? change.key} saved.`,
      });
      if (change.action === "root") {
        void client.resetQueries({ queryKey: ["host-model-options", host.host_id] });
        void client.invalidateQueries({ queryKey: ["hosts"] });
        void client.invalidateQueries({ queryKey: ["harness-startup", host.host_id] });
      }
    },
    onError: (error) => {
      setNotice({ text: failure(error), error: true });
      if (error instanceof ApiError && error.status === 409) {
        setEditing(null);
        void query.refetch();
      }
    },
  });
  const busy = operation.isPending;
  const locked = !online || busy || query.isFetching;

  async function openPath() {
    if (!view) return;
    try {
      if (supportsFileReveal() && localHostId === host.host_id) {
        if (!(await revealFile(host.host_id, view.root))) throw new Error("reveal failed");
        setNotice({ text: "Settings folder opened." });
      } else {
        await copyText(view.root);
        setNotice({ text: "Path copied." });
      }
    } catch {
      setNotice({ text: "Couldn't open or copy this path.", error: true });
    }
  }

  return (
    <section aria-label="Settings folder" className="rounded-xl border border-border bg-card p-4">
      <div className="flex flex-wrap items-center gap-2">
        <FolderIcon className="size-4 text-muted-foreground" aria-hidden />
        <h2 className="text-ui font-medium">Settings folder</h2>
        {view?.choosable && (
          <Button
            size="sm"
            variant="outline"
            disabled={locked || editing !== null}
            onClick={() => setChoosing(true)}
            aria-label="Change settings folder"
          >
            Change
          </Button>
        )}
        {view && (
          <Button size="sm" variant="ghost" onClick={() => void openPath()}>
            {supportsFileReveal() && localHostId === host.host_id ? revealLabel(true) : "Copy path"}
          </Button>
        )}
      </div>
      {view && <p className="mt-2 break-all font-mono text-sm">{view.root}</p>}
      {view && (
        <p className="mt-1 text-xs text-muted-foreground">
          {view.family === "codex"
            ? "config.toml · TOML"
            : view.family === "agy-acp"
              ? "settings.json · Hjson"
              : "settings.json · JSON"}
        </p>
      )}
      {!online && (
        <p className="mt-2 text-sm text-muted-foreground">
          Host offline. Connect it to read or change settings.
        </p>
      )}
      {online && query.isPending && (
        <p className="mt-2 text-sm text-muted-foreground">Loading settings…</p>
      )}
      {query.isError && (
        <div className="mt-2 flex flex-wrap items-center gap-2 text-sm">
          <p role="alert">{failure(query.error)}</p>
          <Button
            size="sm"
            variant="outline"
            disabled={!online || busy || query.isFetching}
            onClick={() => void query.refetch()}
          >
            Retry
          </Button>
        </div>
      )}
      {view?.account && (
        <p
          className={`mt-2 text-sm ${view.account.kind === "none" ? "text-warning" : "text-muted-foreground"}`}
        >
          {view.account.label}
        </p>
      )}
      {view?.read_only_reason && (
        <p className="mt-3 text-sm text-warning">{view.read_only_reason}</p>
      )}
      {view && !view.exists && !view.read_only_reason && (
        <p className="mt-2 text-sm text-muted-foreground">No settings file yet.</p>
      )}
      {view && !query.isError && (
        <div className="mt-3 divide-y divide-border">
          {view.fields.map((field) => {
            const label = LABELS[field.key] ?? field.key;
            return (
              <div key={field.key} className="py-3 first:pt-0 last:pb-0">
                <div className="flex flex-wrap items-center gap-2 text-sm">
                  <span className="font-medium">{label}</span>
                  <span className="min-w-0 break-all text-muted-foreground">
                    {field.present ? String(field.value) : "Not set"}
                  </span>
                  {field.writable && editing !== field.key && (
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={locked || editing !== null}
                      aria-label={`Change ${label}`}
                      onClick={() => {
                        setEditing(field.key);
                        setDraft(field.present ? String(field.value ?? "") : "");
                        setNotice(null);
                      }}
                    >
                      Change
                    </Button>
                  )}
                  {field.writable && field.undo_available && (
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={locked || editing !== null}
                      aria-label={`Undo ${label}`}
                      onClick={() => operation.mutate({ action: "undo", key: field.key })}
                    >
                      Undo
                    </Button>
                  )}
                  {!field.writable && (
                    <span className="text-xs text-muted-foreground">Read only</span>
                  )}
                </div>
                {editing === field.key && (
                  <form
                    className="mt-2 space-y-2"
                    onSubmit={(event) => {
                      event.preventDefault();
                      if (!locked)
                        operation.mutate({
                          action: "write",
                          key: field.key,
                          value: draft,
                          revision: view.revision,
                        });
                    }}
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <Input
                        autoFocus
                        aria-label={`New ${label}`}
                        className="h-8 min-w-40 flex-1"
                        value={draft}
                        onChange={(event) => setDraft(event.target.value)}
                        disabled={locked}
                        list={field.key === "model" ? listId : undefined}
                      />
                      <Button size="sm" type="submit" disabled={locked}>
                        {busy ? "Saving…" : "Save"}
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        type="button"
                        disabled={busy}
                        onClick={() => setEditing(null)}
                      >
                        Cancel
                      </Button>
                    </div>
                    <p className="text-xs text-muted-foreground">
                      Applies outside Omnigent too. Nothing else in the file changes.
                      {view.family !== "codex" && " Spacing may change."}
                      {view.family === "agy" && " A running Antigravity may overwrite this."}
                    </p>
                    {field.key === "model" && models.isError && (
                      <p className="text-xs text-warning">
                        Couldn't load model choices. Enter a model by name.
                      </p>
                    )}
                  </form>
                )}
              </div>
            );
          })}
        </div>
      )}
      <datalist id={listId}>
        {models.data?.map((model) => (
          <option key={model.id} value={model.model ?? model.id}>
            {model.displayName ?? model.id}
          </option>
        ))}
      </datalist>
      {notice && (
        <p
          role={notice.error ? "alert" : "status"}
          className={`mt-3 text-sm ${notice.error ? "text-destructive" : "text-success"}`}
        >
          {notice.text}
        </p>
      )}
      {busy && editing === null && (
        <p role="status" className="mt-3 text-sm text-muted-foreground">
          Updating settings…
        </p>
      )}
      {choosing && view && (
        <WorkspacePickerDialog
          open
          onOpenChange={setChoosing}
          hostId={host.host_id}
          initialPath={view.root}
          title="Select settings folder"
          description="Choose the settings folder for new agent sessions. The account and model list follow this folder."
          onConfirm={(root) => operation.mutate({ action: "root", root })}
        />
      )}
    </section>
  );
}
