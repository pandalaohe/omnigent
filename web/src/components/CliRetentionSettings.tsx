import { useEffect, useMemo, useState } from "react";
import { AlertTriangleIcon, CheckCircle2Icon, Clock3Icon, RotateCcwIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import {
  CliRetentionRequestError,
  type CliRetentionApplicationStatus,
  type CliRetentionFamilyStats,
  type HostCliRetentionResponse,
  useHostCliRetention,
  useHosts,
  useReplaceHostCliRetention,
  useResetHostCliRetention,
} from "@/hooks/useHosts";
import { cn } from "@/lib/utils";

interface RetentionDraft {
  hostId: string;
  sourceRevision: number;
  idleThresholdMinutes: string;
  maxIdleClis: string;
  unlimited: boolean;
  closeOnArchive: boolean;
}

const STATUS_LABELS: Record<CliRetentionApplicationStatus, string> = {
  legacy: "Using legacy rules",
  host_offline: "Waiting for Host",
  other_replica: "Available on another replica",
  pending: "Waiting to apply",
  unknown: "Runtime unknown",
  partial: "Partially applied",
  unsupported: "Unsupported",
  applied: "Applied",
};

function draftFromResponse(hostId: string, response: HostCliRetentionResponse): RetentionDraft {
  if (!response.configured) {
    return {
      hostId,
      sourceRevision: response.revision,
      idleThresholdMinutes: "60",
      maxIdleClis: "10",
      unlimited: false,
      closeOnArchive: true,
    };
  }
  return {
    hostId,
    sourceRevision: response.revision,
    idleThresholdMinutes: String(response.policy.idle_threshold_minutes),
    maxIdleClis:
      response.policy.max_idle_clis === null ? "10" : String(response.policy.max_idle_clis),
    unlimited: response.policy.max_idle_clis === null,
    closeOnArchive: response.policy.close_on_archive,
  };
}

function parseInteger(value: string, min: number, max: number): number | null {
  if (!/^\d+$/.test(value.trim())) return null;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed >= min && parsed <= max ? parsed : null;
}

function isDraftDirty(draft: RetentionDraft, response: HostCliRetentionResponse): boolean {
  const baseline = draftFromResponse(draft.hostId, response);
  return (
    draft.idleThresholdMinutes !== baseline.idleThresholdMinutes ||
    draft.unlimited !== baseline.unlimited ||
    (!draft.unlimited && draft.maxIdleClis !== baseline.maxIdleClis) ||
    draft.closeOnArchive !== baseline.closeOnArchive
  );
}

function familyLabel(family: string): string {
  const known: Record<string, string> = { claude: "Claude", codex: "Codex", qwen: "Qwen" };
  return known[family] ?? `${family.charAt(0).toUpperCase()}${family.slice(1)}`;
}

function applicationLabel(status: CliRetentionApplicationStatus, configured: boolean): string {
  if (!configured) {
    if (status === "pending") return "Restoring legacy rules";
    if (status === "partial") return "Partially restored legacy rules";
    if (status === "host_offline") return "Restoring when Host reconnects";
  }
  return STATUS_LABELS[status];
}

function applicationDetail(status: CliRetentionApplicationStatus, configured: boolean): string {
  if (!configured) {
    if (status === "pending") {
      return "The Host policy was removed, but no current reset result confirms that retained runtimes returned to legacy cleanup.";
    }
    if (status === "partial") {
      return "Some runtimes returned to legacy cleanup, while unavailable runtimes still need to be reset.";
    }
    if (status === "host_offline") {
      return "The Host policy was removed. Runtime cleanup will be confirmed after this Host reconnects.";
    }
  }
  switch (status) {
    case "legacy":
      return "Existing pane and harness lifetime rules remain in control until you save a Host policy.";
    case "host_offline":
      return "You can save changes now. They will apply when this Host reconnects.";
    case "other_replica":
      return "This Host is connected through another Server replica. Refresh after routing recovers.";
    case "pending":
      return "The policy is saved and waiting for its next Runner reconciliation.";
    case "partial":
      return "Some bound sessions could not confirm the current policy.";
    case "unsupported":
      return "The bound runtimes do not support managed CLI retention yet.";
    case "unknown":
      return "The Server could not verify current Runner state.";
    case "applied":
      return "The current policy revision has been observed by the Runner pool.";
  }
}

function requestFailureText(error: unknown): string {
  if (!(error instanceof CliRetentionRequestError)) {
    return error instanceof Error ? error.message : "The request could not be completed.";
  }
  if (error.failure === "conflict") {
    return "These settings changed elsewhere. Reload the current policy before saving again.";
  }
  if (error.failure === "busy") {
    return "CLI retention is reconciling now. Your draft is intact; retry in a moment.";
  }
  if (error.failure === "host_offline") {
    return "The Host went offline before the request completed. Your draft is still here.";
  }
  if (error.failure === "other_replica") {
    return "This Host is connected through another Server replica. Retry after routing recovers.";
  }
  return error.message;
}

function observedLabel(epochSeconds: number | null | undefined): string {
  if (epochSeconds == null) return "No runtime observation yet";
  return `Observed ${new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(epochSeconds * 1_000))}`;
}

function FamilyRow({
  family,
  stats,
  maxIdleClis,
  status,
}: {
  family: string;
  stats: CliRetentionFamilyStats;
  maxIdleClis: number | null;
  status: CliRetentionApplicationStatus;
}) {
  const state =
    status === "applied" && maxIdleClis !== null && stats.idle > maxIdleClis
      ? "Releasing excess"
      : STATUS_LABELS[status];
  return (
    <div className="grid gap-3 px-4 py-4 sm:grid-cols-[minmax(8rem,1fr)_minmax(15rem,2fr)_auto] sm:items-center">
      <div className="flex items-center gap-3 font-medium">
        <span className="flex size-8 items-center justify-center rounded-lg bg-muted text-xs text-muted-foreground">
          {familyLabel(family).slice(0, 2)}
        </span>
        {familyLabel(family)}
      </div>
      <div>
        <p className="text-ui font-medium tabular-nums">
          {stats.idle} / {maxIdleClis === null ? "Unlimited" : maxIdleClis}{" "}
          <span className="text-sm font-normal text-muted-foreground">idle at threshold</span>
        </p>
        <p className="mt-0.5 text-sm text-muted-foreground">
          Active {stats.active} · Below threshold {stats.below_threshold} · Total {stats.total}
        </p>
      </div>
      <span className="w-fit rounded-full bg-muted px-2.5 py-1 text-xs font-medium text-muted-foreground">
        {state}
      </span>
    </div>
  );
}

export function CliRetentionSettings() {
  const hostsQuery = useHosts();
  const hosts = useMemo(() => hostsQuery.data ?? [], [hostsQuery.data]);
  const [selectedHostId, setSelectedHostId] = useState<string | null>(null);
  const [draft, setDraft] = useState<RetentionDraft | null>(null);
  const [confirmLegacy, setConfirmLegacy] = useState(false);
  const retentionQuery = useHostCliRetention(selectedHostId, selectedHostId !== null);
  const replacePolicy = useReplaceHostCliRetention(selectedHostId);
  const resetPolicy = useResetHostCliRetention(selectedHostId);

  useEffect(() => {
    if (hosts.length === 0) {
      setSelectedHostId(null);
      return;
    }
    setSelectedHostId((current) =>
      current && hosts.some((host) => host.host_id === current) ? current : hosts[0]!.host_id,
    );
  }, [hosts]);

  useEffect(() => {
    const response = retentionQuery.data;
    if (selectedHostId === null || response === undefined) return;
    setDraft((current) => {
      if (current?.hostId !== selectedHostId || !isDraftDirty(current, response)) {
        return draftFromResponse(selectedHostId, response);
      }
      return current;
    });
  }, [retentionQuery.data, selectedHostId]);

  const response = retentionQuery.data;
  const selectedHost = hosts.find((host) => host.host_id === selectedHostId);
  const dirty = draft !== null && response !== undefined && isDraftDirty(draft, response);
  const needsSave = response !== undefined && (!response.configured || dirty);
  const remoteChanged =
    draft !== null && response !== undefined && draft.sourceRevision !== response.revision;
  const idleThreshold = draft ? parseInteger(draft.idleThresholdMinutes, 1, 10_080) : null;
  const maxIdle = draft?.unlimited ? null : draft ? parseInteger(draft.maxIdleClis, 0, 100) : null;
  const valid = draft !== null && idleThreshold !== null && (draft.unlimited || maxIdle !== null);
  const replaceError = replacePolicy.error;
  const families = useMemo(
    () =>
      Object.entries(response?.runtime?.families ?? {}).sort(([left], [right]) =>
        left.localeCompare(right),
      ),
    [response?.runtime?.families],
  );

  const clearMutationState = () => {
    replacePolicy.reset();
    resetPolicy.reset();
  };
  const updateDraft = (patch: Partial<RetentionDraft>) => {
    clearMutationState();
    setDraft((current) => (current ? { ...current, ...patch } : current));
  };

  const reloadCurrent = async () => {
    clearMutationState();
    const result = await retentionQuery.refetch();
    if (selectedHostId && result.isError !== true && result.data) {
      setDraft(draftFromResponse(selectedHostId, result.data));
    }
  };

  const save = async () => {
    if (!draft || idleThreshold === null || (!draft.unlimited && maxIdle === null)) return;
    try {
      const next = await replacePolicy.mutateAsync({
        expected_revision: draft.sourceRevision,
        policy: {
          version: 1,
          idle_threshold_minutes: idleThreshold,
          max_idle_clis: draft.unlimited ? null : maxIdle,
          close_on_archive: draft.closeOnArchive,
        },
      });
      setDraft(draftFromResponse(draft.hostId, next));
    } catch {
      // React Query owns the rendered error while the local draft stays intact.
    }
  };

  const restoreLegacy = async () => {
    if (!draft) return;
    try {
      const next = await resetPolicy.mutateAsync(draft.sourceRevision);
      setDraft(draftFromResponse(draft.hostId, next));
      setConfirmLegacy(false);
    } catch {
      // React Query owns the rendered error while the current policy stays visible.
    }
  };

  if (hostsQuery.isLoading) {
    return <p className="text-sm text-muted-foreground">Loading Hosts…</p>;
  }
  if (hostsQuery.isError) {
    return <p className="text-sm text-destructive">Could not load Hosts.</p>;
  }
  if (hosts.length === 0) {
    return (
      <div className="rounded-lg border border-dashed p-5 text-sm text-muted-foreground">
        Connect a Host to configure idle CLI retention.
      </div>
    );
  }

  const applicationStatus = response?.application.status ?? "unknown";
  const isBusy = replacePolicy.isPending || resetPolicy.isPending;

  return (
    <div className="grid gap-5" data-testid="cli-retention-settings">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h2 className="text-ui font-medium">Idle CLI retention</h2>
          <p className="mt-1 max-w-3xl text-sm text-muted-foreground">
            A CLI enters its family pool only after the idle threshold. When a family exceeds its
            limit, Omnigent releases the instance that has been idle longest.
          </p>
        </div>
        <label className="grid min-w-56 gap-1 text-sm font-medium">
          Host
          <select
            aria-label="Host"
            disabled={isBusy}
            className="h-9 rounded-md border border-input bg-background px-3 text-sm shadow-xs outline-none focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50"
            value={selectedHostId ?? ""}
            onChange={(event) => {
              clearMutationState();
              setDraft(null);
              setSelectedHostId(event.target.value);
            }}
          >
            {hosts.map((host) => (
              <option key={host.host_id} value={host.host_id}>
                {host.name} · {host.status === "online" ? "Online" : "Offline"}
              </option>
            ))}
          </select>
        </label>
      </div>

      {retentionQuery.isLoading && !response ? (
        <p className="text-sm text-muted-foreground">Loading retention policy…</p>
      ) : retentionQuery.isError && !response ? (
        <div className="flex items-center justify-between gap-3 rounded-lg border border-destructive/40 bg-destructive/5 p-4 text-sm">
          <span>{requestFailureText(retentionQuery.error)}</span>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => void retentionQuery.refetch()}
          >
            Retry
          </Button>
        </div>
      ) : response && draft ? (
        <>
          {retentionQuery.isError && (
            <div
              role="alert"
              className="flex flex-col gap-3 rounded-lg border border-amber-500/40 bg-amber-500/5 p-4 text-sm sm:flex-row sm:items-center sm:justify-between"
            >
              <span>
                Showing the last successful policy and runtime. Refresh failed:{" "}
                {requestFailureText(retentionQuery.error)}
              </span>
              <Button
                type="button"
                size="sm"
                variant="outline"
                onClick={() => void retentionQuery.refetch()}
              >
                Retry refresh
              </Button>
            </div>
          )}
          <div
            className={cn(
              "flex items-start gap-3 rounded-lg border p-4 text-sm",
              applicationStatus === "applied"
                ? "border-emerald-500/30 bg-emerald-500/5"
                : "border-amber-500/30 bg-amber-500/5",
            )}
          >
            {applicationStatus === "applied" ? (
              <CheckCircle2Icon className="mt-0.5 size-4 shrink-0 text-emerald-600" />
            ) : (
              <Clock3Icon className="mt-0.5 size-4 shrink-0 text-amber-600" />
            )}
            <div>
              <p className="font-medium">
                {applicationLabel(applicationStatus, response.configured)}
              </p>
              <p className="mt-0.5 text-muted-foreground">
                {applicationDetail(applicationStatus, response.configured)}
              </p>
            </div>
          </div>

          <div className="divide-y rounded-xl border bg-card">
            <div className="flex flex-col gap-3 p-4 sm:flex-row sm:items-center sm:justify-between">
              <div>
                <label htmlFor="cli-idle-threshold" className="text-sm font-medium">
                  Idle threshold
                </label>
                <p className="mt-1 text-sm text-muted-foreground">
                  Time without work before a CLI counts toward its idle family pool.
                </p>
              </div>
              <div className="flex items-center gap-2">
                <Input
                  id="cli-idle-threshold"
                  aria-label="Idle threshold in minutes"
                  className="w-28 text-right tabular-nums"
                  type="number"
                  min={1}
                  max={10_080}
                  step={1}
                  disabled={isBusy}
                  value={draft.idleThresholdMinutes}
                  onChange={(event) => updateDraft({ idleThresholdMinutes: event.target.value })}
                  aria-invalid={idleThreshold === null}
                />
                <span className="w-16 text-sm text-muted-foreground">minutes</span>
              </div>
            </div>

            <div className="flex flex-col gap-3 p-4 sm:flex-row sm:items-center sm:justify-between">
              <div>
                <label htmlFor="cli-idle-limit" className="text-sm font-medium">
                  Idle CLI limit
                </label>
                <p className="mt-1 text-sm text-muted-foreground">
                  Applied separately to each family. Active and below-threshold CLIs do not count.
                </p>
              </div>
              <div className="flex flex-wrap items-center justify-end gap-2">
                <Input
                  id="cli-idle-limit"
                  aria-label="Idle CLI limit per family"
                  className="w-24 text-right tabular-nums"
                  type="number"
                  min={0}
                  max={100}
                  step={1}
                  disabled={draft.unlimited || isBusy}
                  value={draft.maxIdleClis}
                  onChange={(event) => updateDraft({ maxIdleClis: event.target.value })}
                  aria-invalid={!draft.unlimited && maxIdle === null}
                />
                <span className="text-sm text-muted-foreground">per family</span>
                <label className="ml-2 flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    disabled={isBusy}
                    checked={draft.unlimited}
                    onChange={(event) => updateDraft({ unlimited: event.target.checked })}
                  />
                  Unlimited
                </label>
              </div>
            </div>

            <div className="flex items-center justify-between gap-5 p-4">
              <div>
                <label htmlFor="cli-close-on-archive" className="text-sm font-medium">
                  Close CLI on archive
                </label>
                <p className="mt-1 text-sm text-muted-foreground">
                  Stop the session and its child work, then release the CLI. Conversation history
                  and the workspace remain available.
                </p>
              </div>
              <Switch
                id="cli-close-on-archive"
                aria-label="Close CLI on archive"
                checked={draft.closeOnArchive}
                disabled={isBusy}
                onCheckedChange={(checked) => updateDraft({ closeOnArchive: checked })}
              />
            </div>
          </div>

          <div className="flex items-start gap-2 rounded-lg border border-amber-500/30 bg-amber-500/5 p-3 text-sm text-muted-foreground">
            <AlertTriangleIcon className="mt-0.5 size-4 shrink-0 text-amber-600" />
            Retained idle CLIs keep Runners, terminals, and related processes available. Watch this
            Host's memory use, especially with a high or unlimited cap.
          </div>

          {remoteChanged && (
            <div className="flex flex-col gap-3 rounded-lg border border-amber-500/40 bg-amber-500/5 p-4 text-sm sm:flex-row sm:items-center sm:justify-between">
              <span>The saved policy changed while you were editing. Reload it before saving.</span>
              <Button
                type="button"
                size="sm"
                variant="outline"
                onClick={() => void reloadCurrent()}
              >
                Reload current policy
              </Button>
            </div>
          )}
          {replaceError && (
            <div
              role="alert"
              className="rounded-lg border border-destructive/40 bg-destructive/5 p-4 text-sm text-destructive"
            >
              {requestFailureText(replaceError)}
            </div>
          )}
          {!valid && (
            <p role="alert" className="text-sm text-destructive">
              Use whole minutes from 1 to 10,080 and a per-family limit from 0 to 100.
            </p>
          )}

          <div>
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
              <h3 className="text-sm font-medium">Current idle pools</h3>
              <span className="text-xs text-muted-foreground">
                {observedLabel(response.runtime?.observed_at ?? response.application.observed_at)}
              </span>
            </div>
            {response.configured && families.length > 0 ? (
              <div className="divide-y overflow-hidden rounded-xl border bg-card">
                {families.map(([family, stats]) => (
                  <FamilyRow
                    key={family}
                    family={family}
                    stats={stats}
                    maxIdleClis={response.policy.max_idle_clis}
                    status={applicationStatus}
                  />
                ))}
              </div>
            ) : (
              <div className="rounded-xl border border-dashed p-5 text-sm text-muted-foreground">
                {response.configured
                  ? "No supported resident CLI runtimes were observed for this Host."
                  : "Runtime family counts will appear after you save a Host policy."}
              </div>
            )}
          </div>

          <div className="flex flex-col-reverse gap-3 border-t pt-4 sm:flex-row sm:items-center sm:justify-between">
            <div className="text-sm text-muted-foreground" aria-live="polite">
              {dirty
                ? "Unsaved changes"
                : response.configured
                  ? `Saved · ${STATUS_LABELS[applicationStatus]}`
                  : applicationStatus === "legacy"
                    ? "Legacy rules active · Save to make this Host policy the owner"
                    : `${applicationLabel(applicationStatus, false)} · Runtime reset is not fully confirmed`}
              {selectedHost ? ` · ${selectedHost.name}` : ""}
            </div>
            <div className="flex flex-wrap justify-end gap-2">
              {response.configured && (
                <Button
                  type="button"
                  variant="outline"
                  disabled={isBusy || remoteChanged}
                  onClick={() => setConfirmLegacy(true)}
                >
                  <RotateCcwIcon className="size-4" />
                  Restore legacy rules
                </Button>
              )}
              <Button
                type="button"
                disabled={!needsSave || !valid || isBusy || remoteChanged}
                onClick={() => void save()}
              >
                {replacePolicy.isPending ? "Saving…" : "Save"}
              </Button>
            </div>
          </div>

          <Dialog open={confirmLegacy} onOpenChange={setConfirmLegacy}>
            <DialogContent>
              <DialogHeader>
                <DialogTitle>Restore legacy CLI rules?</DialogTitle>
                <DialogDescription>
                  This removes the Host policy and returns retained panes and harnesses to the
                  existing TTL cleanup rules. Their configured legacy timeouts will control when
                  idle CLI processes close.
                </DialogDescription>
              </DialogHeader>
              <DialogFooter>
                {remoteChanged && (
                  <div className="mb-2 flex w-full flex-col items-start gap-2 rounded-md border border-amber-500/40 bg-amber-500/5 p-3 text-left text-sm">
                    <span>The saved policy changed after this confirmation opened.</span>
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      onClick={() => void reloadCurrent()}
                    >
                      Reload current policy
                    </Button>
                  </div>
                )}
                {resetPolicy.error && (
                  <div
                    role="alert"
                    className="mb-2 w-full rounded-md border border-destructive/40 bg-destructive/5 p-3 text-left text-sm text-destructive"
                  >
                    {requestFailureText(resetPolicy.error)}
                  </div>
                )}
                <DialogClose asChild>
                  <Button type="button" variant="outline">
                    Cancel
                  </Button>
                </DialogClose>
                <Button
                  type="button"
                  variant="destructive"
                  disabled={resetPolicy.isPending || remoteChanged}
                  onClick={() => void restoreLegacy()}
                >
                  {resetPolicy.isPending ? "Restoring…" : "Restore legacy rules"}
                </Button>
              </DialogFooter>
            </DialogContent>
          </Dialog>
        </>
      ) : null}
    </div>
  );
}
