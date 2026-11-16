import { useId, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangleIcon, XIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  checkModelReferences,
  replaceModelReferences,
  saveModelProviderAliases,
  type ModelReferenceRow,
  type ModelReferenceReplacement,
} from "@/lib/modelReferencesApi";
import { refreshUserPreferencesFromServer } from "@/lib/userPreferencesSync";
import { refreshChoiceConsumers } from "./sessionChoice/choiceInvalidation";

export function ModelReferenceNotice() {
  const client = useQueryClient();
  const query = useQuery({
    queryKey: ["model-references"],
    queryFn: checkModelReferences,
    retry: false,
  });
  const [aliasDraft, setAliasDraft] = useState<[string, string][] | null>(null);
  const [previous, setPrevious] = useState("");
  const [current, setCurrent] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [feedbackError, setFeedbackError] = useState<string | null>(null);
  const pairs = aliasDraft ?? query.data?.aliases.pairs ?? [];
  const refreshed = async () => {
    await Promise.all([
      refreshUserPreferencesFromServer(),
      client.invalidateQueries({ queryKey: ["sessionChoice"] }),
      client.invalidateQueries({ queryKey: ["model-references"] }),
      refreshChoiceConsumers(client),
    ]).catch(() =>
      setFeedbackError(
        "Saved, but the settings view could not refresh. Reload it before making more changes.",
      ),
    );
  };
  const replace = useMutation({
    mutationFn: (body: ModelReferenceReplacement) => replaceModelReferences(body),
    onMutate: () => {
      setMessage(null);
      setFeedbackError(null);
    },
    onSuccess: async (result) => {
      setMessage(
        result.complete
          ? `Replaced ${result.count} saved reference${result.count === 1 ? "" : "s"}.`
          : `Partly replaced: ${result.count} updated, ${result.conflicts.length} conflicts, ${result.failed.length} failed, ${result.remaining ?? "unknown"} remaining. Review the rows and run again.`,
      );
      if (result.aliases === "failed")
        setFeedbackError(
          "The provider rename could not be saved. Run the replacement again to finish.",
        );
      setAliasDraft(null);
      await refreshed();
    },
    onError: (error) =>
      setFeedbackError(
        error instanceof Error ? error.message : "Could not replace saved references.",
      ),
  });
  const save = useMutation({
    mutationFn: () => saveModelProviderAliases(pairs, query.data?.aliases_revision),
    onMutate: () => {
      setMessage(null);
      setFeedbackError(null);
    },
    onSuccess: async () => {
      setAliasDraft(null);
      setMessage("Provider renames saved.");
      await refreshed();
    },
    onError: (error) =>
      setFeedbackError(error instanceof Error ? error.message : "Could not save provider renames."),
  });
  const busy = replace.isPending || save.isPending;
  return (
    <section id="model-references" className="min-w-0 space-y-3" aria-label="Model references">
      <h2 className="text-base font-medium">Model references</h2>
      {query.isError && (
        <div role="alert" className="flex flex-wrap items-center gap-2 text-sm text-destructive">
          Could not check saved models.
          <Button variant="outline" size="sm" onClick={() => void query.refetch()}>
            Retry
          </Button>
        </div>
      )}
      {query.data && query.data.rows.length > 0 && (
        <div className="space-y-3 rounded-md border border-amber-500/40 bg-amber-500/5 p-3">
          <p className="flex items-center gap-2 text-sm font-medium">
            <AlertTriangleIcon className="size-4 shrink-0 text-amber-600" aria-hidden="true" />
            Review saved models
          </p>
          <p className="text-sm text-muted-foreground">
            Some saved models have changed or are absent from a host’s model list. A catalog miss
            still tries the saved model.
          </p>
          <ul className="divide-y divide-border">
            {query.data.rows.map((row) => (
              <ReferenceRow
                key={`${row.entity_id}:${row.path}:${row.host}:${row.harness}`}
                row={row}
                hosts={query.data.hosts}
                models={query.data.models ?? []}
                busy={busy}
                onReplace={(to) => replace.mutate({ mode: "model", from: row.saved, to })}
              />
            ))}
          </ul>
        </div>
      )}
      {message && (
        <p role="status" className="text-sm">
          {message}
        </p>
      )}
      {feedbackError && (
        <p role="alert" className="text-sm text-destructive">
          {feedbackError}
        </p>
      )}
      {query.data?.host_config_manual && (
        <p className="text-sm text-muted-foreground">
          Check provider names, default models and authentication in each host’s configuration
          separately.
        </p>
      )}
      <details className="space-y-3">
        <summary className="cursor-pointer text-sm font-medium">Provider renames</summary>
        <p className="text-sm text-muted-foreground">
          Renames work in both directions while hosts use different names. Saved model ids keep
          their full format.
        </p>
        {pairs.map(([from, to], index) => (
          // eslint-disable-next-line react/no-array-index-key -- Rename rows are stateless and may contain duplicate pairs.
          <div key={`${index}:${from}:${to}`} className="flex min-w-0 items-center gap-2 text-sm">
            <span className="min-w-0 flex-1 break-all">
              {from} ↔ {to}
            </span>
            <Button
              aria-label={`Remove rename ${from} to ${to}`}
              variant="ghost"
              size="icon"
              disabled={busy}
              onClick={() => setAliasDraft(pairs.filter((_, position) => position !== index))}
            >
              <XIcon className="size-4" />
            </Button>
          </div>
        ))}
        <div className="flex flex-wrap items-end gap-2">
          <label className="min-w-0 flex-1 space-y-1 text-xs">
            Previous provider
            <Input
              aria-label="Previous provider"
              value={previous}
              onChange={(event) => setPrevious(event.target.value)}
            />
          </label>
          <label className="min-w-0 flex-1 space-y-1 text-xs">
            Current provider
            <Input
              aria-label="Current provider"
              value={current}
              onChange={(event) => setCurrent(event.target.value)}
            />
          </label>
          <Button
            variant="outline"
            size="sm"
            disabled={busy || !previous || !current || previous === current}
            onClick={() => {
              setAliasDraft([...pairs, [previous, current]]);
              setPrevious("");
              setCurrent("");
            }}
          >
            Add rename
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={busy || !previous || !current || previous === current}
            onClick={() => replace.mutate({ mode: "provider", from: previous, to: current })}
          >
            Replace provider everywhere
          </Button>
        </div>
        <Button size="sm" disabled={busy || aliasDraft === null} onClick={() => save.mutate()}>
          Save renames
        </Button>
      </details>
    </section>
  );
}

function ReferenceRow({
  row,
  hosts,
  models,
  busy,
  onReplace,
}: {
  row: ModelReferenceRow;
  hosts: { id: string; name: string }[];
  models: string[];
  busy: boolean;
  onReplace: (target: string) => void;
}) {
  const id = useId();
  const suggested = row.resolved && row.resolved !== row.saved ? row.resolved : "";
  const [target, setTarget] = useState<string | null>(null);
  const value = target ?? suggested;
  const host = hosts.find((item) => item.id === row.host)?.name ?? row.host;
  return (
    <li className="space-y-2 py-3 first:pt-0 last:pb-0">
      <p className="text-sm font-medium">
        {row.label}
        {host ? ` · ${host}` : ""}
      </p>
      <p className="break-all font-mono text-xs">
        {row.saved}
        {suggested && <> → {suggested}</>}
      </p>
      <p className="text-xs text-muted-foreground">
        {row.manual
          ? "Review this setting manually."
          : row.outcome === "missing"
            ? "Not listed here; tried as unknown."
            : "Resolved automatically for this host."}
      </p>
      {!row.manual && (
        <div className="flex flex-wrap items-end gap-2">
          <label className="min-w-0 flex-1 space-y-1 text-xs">
            Replacement model
            <Input
              aria-label={`Replacement for ${row.label} ${row.saved}`}
              value={value}
              list={id}
              onChange={(event) => setTarget(event.target.value)}
            />
          </label>
          <datalist id={id}>
            {[...new Set([...row.candidates, ...models])].map((model) => (
              <option key={model} value={model} />
            ))}
          </datalist>
          <Button
            variant="outline"
            size="sm"
            disabled={busy || !value || value === row.saved}
            onClick={() => onReplace(value)}
          >
            Replace model everywhere
          </Button>
        </div>
      )}
      {row.still_listed_on.length > 0 && suggested && (
        <p className="text-xs text-muted-foreground">
          An equivalent saved id is still listed on {row.still_listed_on.length} host(s). Renames
          can bridge hosts during an upgrade.
        </p>
      )}
    </li>
  );
}
