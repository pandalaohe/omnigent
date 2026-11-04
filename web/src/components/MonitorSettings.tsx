// The Monitor settings panel on /system: the workspace-wide values behind the
// status page — default load lines and the child-session queue switch, the
// ops health check, and the monitor's own measured cost. The per-host load
// lines are edited on each host's ruler; the alert lines under the metrics.

import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { HelpTip } from "@/components/HelpTip";
import { ProjectSinglePicker } from "@/components/ProjectSinglePicker";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { useAgents } from "@/hooks/useAgents";
import { useProjects } from "@/hooks/useConversations";
import { useHosts } from "@/hooks/useHosts";
import {
  loadLinePairError,
  MAX_LOAD_LINE,
  parseLoadLine,
  useSystemStatusSettings,
  useUpdateSystemStatusSettings,
  type SystemLoadLinesSettings,
  type SystemMonitorOverhead,
  type SystemStatusSettings,
} from "@/hooks/useSystemStatus";
import { resolveCallingDefaults } from "@/lib/callingDefaultsApi";
import { listProjects } from "@/lib/projectsApi";
import { formatBytes } from "@/shell/fileStatusUtils";

function formatPct(value: number, digits = 1): string {
  return `${value.toFixed(digits)}%`;
}

// Radix Select values must be non-empty strings, so unset maps to sentinels.
const HEALTH_CHECK_HOST_UNSET = "__health_check_host_unset__";

interface LoadLinesDraft {
  heavy: string;
  newSession: string;
  queueEnabled: boolean;
}

function toLoadLinesDraft(lines: SystemLoadLinesSettings): LoadLinesDraft {
  return {
    heavy: String(lines.default.heavy),
    newSession: String(lines.default.new_session),
    queueEnabled: lines.queue_enabled,
  };
}

/** The workspace's default load lines and the queue switch; the pair is checked before saving. */
function LoadLinesSettingsForm({ lines }: { lines: SystemLoadLinesSettings }) {
  const update = useUpdateSystemStatusSettings();
  const [draft, setDraft] = useState<LoadLinesDraft>(() => toLoadLinesDraft(lines));
  const [invalid, setInvalid] = useState<string | null>(null);
  const lastSynced = useRef(JSON.stringify(toLoadLinesDraft(lines)));

  useEffect(() => {
    const next = toLoadLinesDraft(lines);
    setDraft((current) => (JSON.stringify(current) === lastSynced.current ? next : current));
    lastSynced.current = JSON.stringify(next);
  }, [lines]);

  const unchanged = JSON.stringify(draft) === JSON.stringify(toLoadLinesDraft(lines));

  return (
    <form
      data-testid="load-lines-form"
      className="mt-4 flex flex-col gap-4"
      onSubmit={(event) => {
        event.preventDefault();
        const pair = {
          heavy: parseLoadLine(draft.heavy),
          new_session: parseLoadLine(draft.newSession),
        };
        const error = loadLinePairError(pair.heavy, pair.new_session);
        setInvalid(error);
        if (error !== null) return;
        update.mutate({ load_lines: { default: pair, queue_enabled: draft.queueEnabled } });
      }}
    >
      <div className="grid grid-cols-2 gap-3">
        <div className="flex flex-col gap-1">
          <label className="flex flex-col gap-1">
            <span className="text-ui font-normal md:font-medium">Heavy work (per core)</span>
            <Input
              type="number"
              min={0}
              max={MAX_LOAD_LINE}
              step="any"
              value={draft.heavy}
              onChange={(event) =>
                setDraft((current) => ({ ...current, heavy: event.target.value }))
              }
            />
          </label>
          <span className="text-sm text-muted-foreground">
            At or above it, new heavy work on a host waits. Memory above its alert line counts too.
          </span>
        </div>
        <div className="flex flex-col gap-1">
          <label className="flex flex-col gap-1">
            <span className="text-ui font-normal md:font-medium">No new session (per core)</span>
            <Input
              type="number"
              min={0}
              max={MAX_LOAD_LINE}
              step="any"
              value={draft.newSession}
              onChange={(event) =>
                setDraft((current) => ({ ...current, newSession: event.target.value }))
              }
            />
          </label>
          <span className="text-sm text-muted-foreground">
            Must be above Heavy work, at most {MAX_LOAD_LINE}.
          </span>
        </div>
      </div>
      <div className="flex items-start gap-3 pl-6">
        <Switch
          id="load-lines-queue-enabled"
          checked={draft.queueEnabled}
          onCheckedChange={(queueEnabled) => setDraft((current) => ({ ...current, queueEnabled }))}
          className="mt-0.5 shrink-0"
        />
        <div className="flex flex-col gap-0.5">
          <label htmlFor="load-lines-queue-enabled" className="text-ui font-normal md:font-medium">
            Queue new child sessions
          </label>
          <span className="text-sm text-muted-foreground">
            Child sessions an agent starts on a host above its No new session line wait, and start
            once the host drops below it. Sessions you start yourself are never queued.
          </span>
        </div>
      </div>
      {(invalid !== null || update.isError) && (
        <p role="alert" className="text-sm text-destructive">
          {invalid ?? update.error?.message}
        </p>
      )}
      <div>
        <Button type="submit" loading={update.isPending} disabled={unchanged || update.isPending}>
          Save
        </Button>
      </div>
    </form>
  );
}

interface HealthCheckDraft {
  projectId: string;
  hostId: string;
  prompt: string;
}

function toHealthCheckDraft(settings: SystemStatusSettings): HealthCheckDraft {
  return {
    projectId: settings.health_check.project_id ?? "",
    hostId: settings.health_check.host_id ?? "",
    prompt: settings.health_check.prompt ?? settings.default_health_check_prompt,
  };
}

/** The shipped default is stored as null, so text equal to it is sent as null. */
function healthCheckPromptPayload(text: string, defaultPrompt: string): string | null {
  const trimmed = text.trim();
  if (trimmed === "" || trimmed === defaultPrompt.trim()) return null;
  return trimmed;
}

function HealthCheckSettingsForm({ settings }: { settings: SystemStatusSettings }) {
  const projects = useProjects();
  const allProjects = useQuery({
    queryKey: ["projects", "with-archived"],
    queryFn: () => listProjects({ includeArchived: true }),
  });
  const hosts = useHosts();
  // Session-discovered agents only: the resolved id falls back to itself when
  // the default isn't among them.
  const agents = useAgents();
  const update = useUpdateSystemStatusSettings();
  const [draft, setDraft] = useState<HealthCheckDraft>(() => toHealthCheckDraft(settings));
  const lastSynced = useRef(JSON.stringify(toHealthCheckDraft(settings)));

  useEffect(() => {
    const next = toHealthCheckDraft(settings);
    setDraft((current) => (JSON.stringify(current) === lastSynced.current ? next : current));
    lastSynced.current = JSON.stringify(next);
  }, [settings]);

  const { projectId, hostId } = draft;
  // Resolving refuses an unlaunchable default (a saved joint agent, a harness
  // that is not ready, …); the problems are shown before anyone clicks.
  const resolve = useQuery({
    queryKey: ["calling-defaults", "resolve", projectId, hostId],
    queryFn: () => resolveCallingDefaults({ projectId, hostId }),
    enabled: projectId !== "" && hostId !== "",
    staleTime: 30_000,
  });

  const projectOptions =
    allProjects.data ??
    (projects.data ?? []).flatMap((project) =>
      project.id === null ? [] : [{ id: project.id, name: project.name }],
    );
  const hostOptions = hosts.data ?? [];
  const resolvedAgentId = resolve.data?.agent_id ?? null;
  const resolvedAgentName =
    resolvedAgentId === null
      ? null
      : (agents.data?.find((agent) => agent.id === resolvedAgentId)?.display_name ??
        resolvedAgentId);
  const savedDraft = toHealthCheckDraft(settings);
  const unchanged = JSON.stringify(draft) === JSON.stringify(savedDraft);

  return (
    <form
      data-testid="health-check-form"
      className="mt-4 flex flex-col gap-4"
      onSubmit={(event) => {
        event.preventDefault();
        update.mutate({
          health_check: {
            project_id: projectId === "" ? null : projectId,
            host_id: hostId === "" ? null : hostId,
            prompt: healthCheckPromptPayload(draft.prompt, settings.default_health_check_prompt),
          },
        });
      }}
    >
      <div className="flex flex-col gap-1.5">
        <label htmlFor="health-check-project" className="text-ui font-normal md:font-medium">
          Ops project
        </label>
        <ProjectSinglePicker
          projects={projectOptions}
          value={projectId || null}
          onChange={(id) => setDraft((current) => ({ ...current, projectId: id ?? "" }))}
          label="Ops project"
          testId="health-check-project-trigger"
        />
        <p className="text-sm text-muted-foreground">
          Sessions opened by the check are filed in this project.
        </p>
      </div>

      <div className="flex flex-col gap-1.5">
        <label htmlFor="health-check-host" className="text-ui font-normal md:font-medium">
          Ops host
        </label>
        <Select
          value={hostId === "" ? HEALTH_CHECK_HOST_UNSET : hostId}
          onValueChange={(value) =>
            setDraft((current) => ({
              ...current,
              hostId: value === HEALTH_CHECK_HOST_UNSET ? "" : value,
            }))
          }
        >
          <SelectTrigger
            id="health-check-host"
            data-testid="health-check-host-trigger"
            className="w-full"
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent position="popper" align="start">
            <SelectItem value={HEALTH_CHECK_HOST_UNSET}>No host</SelectItem>
            {hostOptions.map((host) => (
              <SelectItem key={host.host_id} value={host.host_id}>
                {host.status === "offline" ? `${host.name} (offline)` : host.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <p className="text-sm text-muted-foreground">
          The check opens a session as you, in this project on this host; both must be yours.
        </p>
      </div>

      {projectId !== "" && hostId !== "" && (
        <div className="flex flex-col gap-1">
          <span className="text-ui font-normal md:font-medium">Agent</span>
          {resolve.data === undefined ? (
            resolve.isError ? (
              <span className="text-sm text-destructive">{resolve.error.message}</span>
            ) : (
              <span className="text-sm text-muted-foreground">Resolving…</span>
            )
          ) : (
            <>
              {resolvedAgentName !== null ? (
                <span className="text-ui">{resolvedAgentName}</span>
              ) : (
                <span className="text-sm text-amber-600 dark:text-amber-400">
                  No default agent is set for this project on this host. Set the project's default
                  agent for this host in the project's settings.
                </span>
              )}
              {resolve.data.problems.length > 0 && (
                <ul className="flex flex-col gap-1 text-sm text-amber-600 dark:text-amber-400">
                  {resolve.data.problems.map((problem) => (
                    <li key={`${problem.field}:${problem.setting}:${problem.message}`}>
                      {problem.message}
                    </li>
                  ))}
                </ul>
              )}
            </>
          )}
        </div>
      )}

      <div className="flex flex-col gap-1.5">
        <div className="flex items-center justify-between gap-3">
          <label htmlFor="health-check-prompt" className="text-ui font-normal md:font-medium">
            Prompt
          </label>
          <Button
            type="button"
            size="sm"
            variant="ghost"
            onClick={() =>
              setDraft((current) => ({
                ...current,
                prompt: settings.default_health_check_prompt,
              }))
            }
          >
            Reset to default
          </Button>
        </div>
        <Textarea
          id="health-check-prompt"
          value={draft.prompt}
          rows={10}
          onChange={(event) => setDraft((current) => ({ ...current, prompt: event.target.value }))}
        />
        <span className="text-sm text-muted-foreground">
          Sent as the first message, followed by the monitor snapshot.
        </span>
      </div>

      <div className="flex items-center gap-2">
        <Button type="submit" loading={update.isPending} disabled={unchanged || update.isPending}>
          Save
        </Button>
        <span className="text-sm text-muted-foreground">
          The project's default agent on this host runs the check.
        </span>
      </div>

      {update.isError && (
        <div
          role="alert"
          className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-ui text-destructive"
        >
          {update.error.message}
        </div>
      )}
    </form>
  );
}

/** The monitor's own cost: one row for the server, one per reporting host. */
function MonitorOverheadTable({
  overhead,
  hostNames,
}: {
  overhead: SystemMonitorOverhead;
  hostNames: Map<string, string>;
}) {
  return (
    <div className="mt-4 overflow-hidden rounded-lg border">
      <table className="w-full text-sm" data-testid="monitor-overhead-table">
        <thead>
          <tr className="border-b text-sm text-muted-foreground">
            <th className="px-3 py-1.5 text-left font-medium">Process</th>
            <th className="px-3 py-1.5 text-right font-medium">CPU</th>
            <th className="px-3 py-1.5 text-right font-medium">Memory</th>
          </tr>
        </thead>
        <tbody>
          <tr className="border-b last:border-b-0">
            <td className="px-3 py-1.5">Server</td>
            <td className="px-3 py-1.5 text-right tabular-nums">
              {formatPct(overhead.server.cpu_pct, 2)}
            </td>
            <td className="px-3 py-1.5 text-right tabular-nums">
              {formatBytes(overhead.server.mem_estimate_bytes)} (estimate)
            </td>
          </tr>
          {Object.entries(overhead.hosts).map(([hostId, cost]) => (
            <tr key={hostId} className="border-b last:border-b-0">
              <td className="px-3 py-1.5">{hostNames.get(hostId) ?? hostId}</td>
              <td className="px-3 py-1.5 text-right tabular-nums">{formatPct(cost.cpu_pct, 2)}</td>
              <td className="px-3 py-1.5 text-right tabular-nums">
                {formatBytes(cost.rss_delta)} below start
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function MonitorSettingsDialog({
  open,
  onOpenChange,
  overhead,
  hostNames,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  overhead: SystemMonitorOverhead;
  hostNames: Map<string, string>;
}) {
  const settings = useSystemStatusSettings({ enabled: open });
  const data = settings.data ?? undefined;
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[min(40rem,90vh)] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Monitor settings</DialogTitle>
          <DialogDescription>
            Shared by every host. Each host can set its own load lines on its card.
          </DialogDescription>
        </DialogHeader>
        {data === undefined ? (
          <p className="text-sm text-muted-foreground">
            {settings.isError ? settings.error.message : "Loading settings…"}
          </p>
        ) : (
          <div className="flex flex-col gap-6">
            {data.load_lines !== undefined && (
              <div>
                <h3 className="flex items-center gap-1.5 text-ui font-semibold">
                  Default load lines
                  <HelpTip label="About default load lines" className="size-11 md:size-4">
                    Load per core is the 1-minute load divided by logical cores. A host that sets
                    its own lines ignores these.
                  </HelpTip>
                </h3>
                <LoadLinesSettingsForm lines={data.load_lines} />
              </div>
            )}
            <div className="border-t pt-4">
              <h3 className="flex items-center gap-1.5 text-ui font-semibold">
                Monitor overhead
                <HelpTip label="About monitor overhead" className="size-11 md:size-4">
                  The monitor's own CPU and memory. A host's figure is how much its memory grew
                  since the monitor started, so it is an upper bound.
                </HelpTip>
              </h3>
              <MonitorOverheadTable overhead={overhead} hostNames={hostNames} />
            </div>
            <div className="border-t pt-4">
              <h3 className="flex items-center gap-1.5 text-ui font-semibold">
                Health check
                <HelpTip label="About the health check" className="size-11 md:size-4">
                  Opens a session on this project and host and sends the prompt below with a monitor
                  snapshot as its first message.
                </HelpTip>
              </h3>
              <HealthCheckSettingsForm settings={data} />
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
