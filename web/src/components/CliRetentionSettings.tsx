// One Host's idle-CLI retention, shown as a fold on its System status card:
// the policy this Host owns (idle threshold, per-family cap, close on
// archive), the counts the Runner last reported, and the releases that have
// happened here. The policy is stored per Host, so this panel needs no Host
// picker: the card it sits on is the Host.

import { useEffect, useMemo, useState } from "react";
import { AlertTriangleIcon } from "lucide-react";

import { HelpTip } from "@/components/HelpTip";
import { SettingRow } from "@/components/SettingsFields";
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
  type Host,
  type HostCliRelease,
  type HostCliRetentionResponse,
  useHostCliReleases,
  useHostCliRetention,
  useReplaceHostCliRetention,
  useResetHostCliRetention,
} from "@/hooks/useHosts";
import { useArchiveWorktreePreference } from "@/lib/archiveWorktreePreferences";
import { Link } from "@/lib/routing";
import { relativeTime } from "@/lib/relativeTime";
import { cn } from "@/lib/utils";

interface RetentionDraft {
  hostId: string;
  sourceRevision: number;
  idleThresholdMinutes: string;
  maxIdleClis: string;
  closeOnArchive: boolean;
}

const STATUS_LABELS: Record<CliRetentionApplicationStatus, string> = {
  legacy: "Using the built-in rule",
  host_offline: "Waiting for the Host",
  other_replica: "Available on another replica",
  pending: "Waiting to apply",
  unknown: "Runtime unknown",
  partial: "Partially applied",
  unsupported: "Unsupported",
  applied: "Applied",
};

function draftFromResponse(hostId: string, response: HostCliRetentionResponse): RetentionDraft {
  // The response's policy is what the Runner applies, configured or not, so
  // the fields start from it rather than from a second set of defaults.
  return {
    hostId,
    sourceRevision: response.revision,
    idleThresholdMinutes: String(response.policy.idle_threshold_minutes),
    maxIdleClis:
      response.policy.max_idle_clis === null ? "" : String(response.policy.max_idle_clis),
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
    draft.maxIdleClis !== baseline.maxIdleClis ||
    draft.closeOnArchive !== baseline.closeOnArchive
  );
}

function familyLabel(family: string): string {
  const known: Record<string, string> = { claude: "Claude", codex: "Codex", qwen: "Qwen" };
  return known[family] ?? `${family.charAt(0).toUpperCase()}${family.slice(1)}`;
}

function applicationLabel(status: CliRetentionApplicationStatus, configured: boolean): string {
  if (!configured) {
    if (status === "pending") return "Restoring the built-in rule";
    if (status === "partial") return "Partially restored the built-in rule";
    if (status === "host_offline") return "Restoring when the Host reconnects";
  }
  return STATUS_LABELS[status];
}

function applicationDetail(status: CliRetentionApplicationStatus, configured: boolean): string {
  if (!configured) {
    if (status === "pending") {
      return "The Host policy was removed, but no current result confirms that retained CLIs went back to the built-in rule.";
    }
    if (status === "partial") {
      return "Some CLIs went back to the built-in rule, while CLIs that were unreachable still need that reset.";
    }
    if (status === "host_offline") {
      return "The Host policy was removed. Cleanup will be confirmed after this Host reconnects.";
    }
  }
  switch (status) {
    case "legacy":
      return "This Host keeps the built-in idle rule until you save a policy here.";
    case "host_offline":
      return "You can save changes now. They apply when this Host reconnects.";
    case "other_replica":
      return "This Host is connected through another server. Refresh after it reconnects.";
    case "pending":
      return "The policy is saved and waits for the next Runner check.";
    case "partial":
      return "Some bound sessions could not confirm the current policy.";
    case "unsupported":
      return "The CLIs bound here do not support managed idle retention yet.";
    case "unknown":
      return "The server could not read the current Runner state.";
    case "applied":
      return "The Runner pool has this revision.";
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
    return "This Host is connected through another server. Retry after it reconnects.";
  }
  return error.message;
}

function observedLabel(epochSeconds: number | null | undefined): string {
  if (epochSeconds == null) return "No reading yet";
  return `Read ${new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(epochSeconds * 1_000))}`;
}

function releaseReasonLabel(reason: HostCliRelease["reason"]): string {
  return reason === "archive" ? "Session archived" : "Over the idle limit";
}

function releaseStatusLabel(status: string): string | null {
  if (status === "completed") return null;
  if (status === "claimed") return "releasing";
  if (status === "pending") return "waiting";
  if (status === "cancelled") return "cancelled";
  return status;
}

function CountRow({
  family,
  stats,
  maxIdleClis,
}: {
  family: string;
  stats: CliRetentionFamilyStats;
  maxIdleClis: number | null;
}) {
  const overLimit = maxIdleClis !== null && stats.idle > maxIdleClis;
  return (
    <tr className="border-b last:border-b-0">
      <td className="py-1.5 pr-3">{familyLabel(family)}</td>
      <td className="py-1.5 pr-3 text-right tabular-nums">
        {stats.idle} / {maxIdleClis === null ? "No limit" : maxIdleClis}
      </td>
      <td className="py-1.5 pr-3 text-right tabular-nums text-muted-foreground">{stats.active}</td>
      <td className="py-1.5 pr-3 text-right tabular-nums text-muted-foreground">
        {stats.below_threshold}
      </td>
      <td className={cn("py-1.5 text-right", overLimit && "text-amber-700 dark:text-amber-400")}>
        {overLimit ? "Releasing" : "Within limit"}
      </td>
    </tr>
  );
}

function RecentReleases({ hostId, open }: { hostId: string; open: boolean }) {
  const releases = useHostCliReleases(hostId, open);
  return (
    <div className="mt-4" data-testid={`cli-recent-releases-${hostId}`}>
      <div className="mb-1 flex items-center gap-1.5">
        <h4 className="text-ui font-normal md:font-medium">Recent releases</h4>
        <HelpTip label="About recent releases" className="size-11 md:size-4">
          A CLI is released when you archive a session, or when it has been idle longest and its
          family is over the limit. These are this Host's latest records.
        </HelpTip>
      </div>
      {releases.isLoading ? (
        <p className="text-sm text-muted-foreground">Reading the release log…</p>
      ) : releases.isError ? (
        <p className="text-sm text-destructive">{requestFailureText(releases.error)}</p>
      ) : (releases.data ?? []).length === 0 ? (
        <p className="rounded-lg border border-dashed p-3 text-sm text-muted-foreground">
          No CLI has been released on this Host yet.
        </p>
      ) : (
        <ul
          className="max-h-40 overflow-y-auto rounded-lg border bg-muted/20 px-3 py-2 font-mono text-xs"
          data-testid="cli-release-log"
        >
          {(releases.data ?? []).map((release) => {
            const state = releaseStatusLabel(release.status);
            return (
              <li key={`${release.target_session_id}:${release.created_at}`} className="py-0.5">
                <span className="text-muted-foreground">
                  {relativeTime(release.created_at * 1000)} ago
                </span>{" "}
                {releaseReasonLabel(release.reason)}
                {release.family !== null && <> · {familyLabel(release.family)}</>}
                {state !== null && <span className="text-muted-foreground"> · {state}</span>}{" "}
                <Link to={`/c/${release.target_session_id}`} className="underline">
                  Open session
                </Link>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

/**
 * One Host's idle-CLI policy fold. Rendering is gated on the retention route
 * answering, so a Host that belongs to someone else shows no fold at all and
 * the route's own access rule is the only one.
 */
export function CliRetentionFold({ host }: { host: Host }) {
  const [open, setOpen] = useState(false);
  const retentionQuery = useHostCliRetention(host.host_id, open);
  const replacePolicy = useReplaceHostCliRetention(host.host_id);
  const resetPolicy = useResetHostCliRetention(host.host_id);
  const [draft, setDraft] = useState<RetentionDraft | null>(null);
  const [confirmBuiltIn, setConfirmBuiltIn] = useState(false);

  const response = retentionQuery.data;
  useEffect(() => {
    if (response === undefined) return;
    setDraft((current) =>
      current === null || !isDraftDirty(current, response)
        ? draftFromResponse(host.host_id, response)
        : current,
    );
  }, [response, host.host_id]);

  // A member who does not own this Host gets no fold; the card keeps its other
  // lines. Until the route answers, the fold stays closed and empty.
  const archivePreference = useArchiveWorktreePreference();
  const gitRemovesWorktrees = (archivePreference.preference.data?.mode ?? "never") !== "never";

  const dirty = draft !== null && response !== undefined && isDraftDirty(draft, response);
  const needsSave = response !== undefined && (!response.configured || dirty);
  const remoteChanged =
    draft !== null && response !== undefined && draft.sourceRevision !== response.revision;
  const idleThreshold = draft ? parseInteger(draft.idleThresholdMinutes, 1, 10_080) : null;
  const trimmedLimit = draft?.maxIdleClis.trim() ?? "";
  const limitIsBlank = trimmedLimit === "";
  const maxIdle = draft && !limitIsBlank ? parseInteger(draft.maxIdleClis, 0, 100) : null;
  const valid = draft !== null && idleThreshold !== null && (limitIsBlank || maxIdle !== null);
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
    if (result.isError !== true && result.data) {
      setDraft(draftFromResponse(host.host_id, result.data));
    }
  };

  const save = async () => {
    if (!draft || idleThreshold === null || (!limitIsBlank && maxIdle === null)) return;
    try {
      const next = await replacePolicy.mutateAsync({
        expected_revision: draft.sourceRevision,
        policy: {
          version: 1,
          idle_threshold_minutes: idleThreshold,
          max_idle_clis: limitIsBlank ? null : maxIdle,
          close_on_archive: draft.closeOnArchive,
        },
      });
      setDraft(draftFromResponse(draft.hostId, next));
    } catch {
      // React Query owns the rendered error while the local draft stays intact.
    }
  };

  const restoreBuiltInRule = async () => {
    if (!draft) return;
    try {
      const next = await resetPolicy.mutateAsync(draft.sourceRevision);
      setDraft(draftFromResponse(draft.hostId, next));
      setConfirmBuiltIn(false);
    } catch {
      // React Query owns the rendered error while the current policy stays visible.
    }
  };

  const isBusy = replacePolicy.isPending || resetPolicy.isPending;
  const applicationStatus = response?.application.status ?? "unknown";
  const statusText =
    response === undefined ? null : applicationLabel(applicationStatus, response.configured);

  return (
    <details
      className="mt-3 border-t pt-2"
      data-testid={`cli-retention-${host.host_id}`}
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary className="flex cursor-pointer items-center gap-1 text-sm text-muted-foreground hover:text-foreground">
        Idle CLIs
        {statusText !== null && (
          <span className="ml-1 text-xs" data-testid={`cli-retention-status-${host.host_id}`}>
            {statusText}
          </span>
        )}
      </summary>
      {open && retentionQuery.isLoading && response === undefined && (
        <p className="mt-2 text-sm text-muted-foreground">Reading this Host's policy…</p>
      )}
      {/* A Host this user does not own answers nothing here, so it shows no fold. */}
      {open && response !== undefined && draft !== null && (
        <div className="mt-3 flex flex-col gap-3">
          <p className="text-sm text-muted-foreground">
            {applicationDetail(applicationStatus, response.configured)}
          </p>
          {retentionQuery.isError && (
            <div
              role="alert"
              className="flex flex-col gap-2 rounded-lg border border-amber-500/40 bg-amber-500/5 p-3 text-sm sm:flex-row sm:items-center sm:justify-between"
            >
              <span>
                Showing the last reading. Refresh failed: {requestFailureText(retentionQuery.error)}
              </span>
              <Button
                type="button"
                size="sm"
                variant="outline"
                onClick={() => void retentionQuery.refetch()}
              >
                Retry
              </Button>
            </div>
          )}
          <SettingRow
            label="Count a CLI as idle after"
            hint={
              <>
                Time without work before a CLI joins its idle pool. This covers main sessions; child
                sessions close after their own idle timeout, not this number.
              </>
            }
            note="This Host's own value; other Hosts keep theirs."
          >
            <Input
              aria-label="Count a CLI as idle after, in minutes"
              className="w-20 text-right tabular-nums"
              type="number"
              min={1}
              max={10_080}
              step={1}
              disabled={isBusy}
              value={draft.idleThresholdMinutes}
              onChange={(event) => updateDraft({ idleThresholdMinutes: event.target.value })}
              aria-invalid={idleThreshold === null}
            />
            <span className="text-sm text-muted-foreground">minutes</span>
          </SettingRow>
          <div className="pl-6">
            <SettingRow
              label="Keep at most"
              hint={
                <>
                  Applied to each CLI family on its own. Active CLIs and CLIs below the idle
                  threshold do not count. Leave it blank for no limit.
                </>
              }
            >
              <Input
                aria-label="Keep at most, per family"
                className="w-20 text-right tabular-nums"
                type="number"
                min={0}
                max={100}
                step={1}
                placeholder="No limit"
                disabled={isBusy}
                value={draft.maxIdleClis}
                onChange={(event) => updateDraft({ maxIdleClis: event.target.value })}
                aria-invalid={!limitIsBlank && maxIdle === null}
              />
              {limitIsBlank && (
                <span
                  className="inline-flex items-center gap-1 text-amber-700 dark:text-amber-400"
                  data-testid="cli-limit-warning"
                >
                  <AlertTriangleIcon className="size-3.5 shrink-0" aria-hidden="true" />
                  <span className="text-sm">No limit</span>
                  <HelpTip label="About no limit" className="size-11 md:size-4">
                    Every idle CLI stays until this Host runs out of memory, or until you archive
                    its session.
                  </HelpTip>
                </span>
              )}
            </SettingRow>
          </div>
          <SettingRow
            label="Close CLI on archive"
            hint={
              <>
                Stop the session and its child work, then release its CLI. Conversation history and
                the workspace stay.
              </>
            }
          >
            <Switch
              aria-label="Close CLI on archive"
              checked={gitRemovesWorktrees ? true : draft.closeOnArchive}
              disabled={isBusy || gitRemovesWorktrees}
              onCheckedChange={(checked) => updateDraft({ closeOnArchive: checked })}
            />
          </SettingRow>
          {gitRemovesWorktrees && (
            <p
              className="pl-6 text-sm text-muted-foreground"
              data-testid="cli-close-on-archive-locked"
            >
              Locked on, because Git already removes worktrees when you archive a session.{" "}
              <Button asChild variant="link" size="sm" className="h-auto px-0">
                <Link to="/settings/git">Change Worktrees on archive</Link>
              </Button>
            </p>
          )}
          <div>
            <div className="mb-1 flex flex-wrap items-center justify-between gap-2">
              <h4 className="text-ui font-normal md:font-medium">Idle CLIs now</h4>
              <span className="text-xs text-muted-foreground">
                {observedLabel(response.runtime?.observed_at ?? response.application.observed_at)}
              </span>
            </div>
            {response.configured && families.length > 0 ? (
              <div className="overflow-x-auto rounded-lg border px-3">
                <table className="w-full text-sm" data-testid="cli-family-table">
                  <thead>
                    <tr className="border-b text-sm text-muted-foreground">
                      <th className="py-1.5 pr-3 text-left font-medium">CLI</th>
                      <th className="py-1.5 pr-3 text-right font-medium">Idle / limit</th>
                      <th className="py-1.5 pr-3 text-right font-medium">Working</th>
                      <th className="py-1.5 pr-3 text-right font-medium">Not idle yet</th>
                      <th className="py-1.5 text-right font-medium">Next</th>
                    </tr>
                  </thead>
                  <tbody>
                    {families.map(([family, stats]) => (
                      <CountRow
                        key={family}
                        family={family}
                        stats={stats}
                        maxIdleClis={response.policy.max_idle_clis}
                      />
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="rounded-lg border border-dashed p-3 text-sm text-muted-foreground">
                {response.configured
                  ? "No resident CLI was observed for this Host."
                  : "Counts appear after you save a policy here."}
              </p>
            )}
          </div>
          <RecentReleases hostId={host.host_id} open={open} />
          {remoteChanged && (
            <div className="flex flex-col gap-2 rounded-lg border border-amber-500/40 bg-amber-500/5 p-3 text-sm sm:flex-row sm:items-center sm:justify-between">
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
          {replaceErrorText(replacePolicy.error) !== null && (
            <p role="alert" className="text-sm text-destructive">
              {replaceErrorText(replacePolicy.error)}
            </p>
          )}
          {!valid && (
            <p role="alert" className="text-sm text-destructive">
              Use whole minutes from 1 to 10,080, and a limit from 0 to 100 or leave it blank.
            </p>
          )}
          <div className="flex flex-col-reverse gap-2 border-t pt-3 sm:flex-row sm:items-center sm:justify-between">
            <span className="text-sm text-muted-foreground" aria-live="polite">
              {dirty ? "Unsaved changes" : response.configured ? null : "No policy saved here yet"}
            </span>
            <div className="flex flex-wrap justify-end gap-2">
              {response.configured && (
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  disabled={isBusy || remoteChanged}
                  onClick={() => setConfirmBuiltIn(true)}
                >
                  Use the built-in rule
                </Button>
              )}
              <Button
                type="button"
                size="sm"
                disabled={!needsSave || !valid || isBusy || remoteChanged}
                onClick={() => void save()}
              >
                {replacePolicy.isPending ? "Saving…" : "Save"}
              </Button>
            </div>
          </div>
          <Dialog open={confirmBuiltIn} onOpenChange={setConfirmBuiltIn}>
            <DialogContent>
              <DialogHeader>
                <DialogTitle>Use the built-in idle rule?</DialogTitle>
                <DialogDescription>
                  This removes the policy on this Host. Retained CLIs go back to the built-in idle
                  timeout, which decides when they close.
                </DialogDescription>
              </DialogHeader>
              <DialogFooter>
                {resetPolicy.error && (
                  <p role="alert" className="mb-2 w-full text-left text-sm text-destructive">
                    {requestFailureText(resetPolicy.error)}
                  </p>
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
                  onClick={() => void restoreBuiltInRule()}
                >
                  {resetPolicy.isPending ? "Restoring…" : "Use the built-in rule"}
                </Button>
              </DialogFooter>
            </DialogContent>
          </Dialog>
        </div>
      )}
    </details>
  );
}

function replaceErrorText(error: unknown): string | null {
  return error === null || error === undefined ? null : requestFailureText(error);
}
