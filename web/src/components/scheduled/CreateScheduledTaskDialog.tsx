// Dialog for creating or editing a scheduled task. Reuses the existing agent,
// host, and workspace pickers where the backend can persist those fields.

import { useEffect, useMemo, useRef, useState } from "react";
import { FolderOpenIcon, TriangleAlertIcon } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Label } from "@/components/scheduled/Label";
import { ScheduleFields } from "@/components/scheduled/ScheduleFields";
import { ModelEffortFields } from "@/components/scheduled/ModelEffortFields";
import { WorkspacePickerDialog } from "@/shell/WorkspacePickerDialog";
import { AgentHarnessPicker } from "@/shell/NewChatDialog";
import { useAvailableAgents, type AvailableAgent } from "@/hooks/useAvailableAgents";
import { useHosts } from "@/hooks/useHosts";
import { useProjects } from "@/hooks/useConversations";
import { customAgentForPicker, useCustomAgents } from "@/lib/customAgentsApi";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { sandboxOptionLabel } from "@/lib/capabilities";
import { useCreateScheduledTask, useUpdateScheduledTask } from "@/hooks/useScheduledTasks";
import { isNativeCodingAgent, nativeAgentHasCapability } from "@/lib/nativeCodingAgents";
import { isAcpHarnessAgent, selectableSessionAgents } from "@/lib/agentGrouping";
import {
  isBackdropOverlay,
  isInsidePopper,
  shouldGuardDialogDismiss,
} from "@/lib/dialogDismissGuard";
import {
  buildRRule,
  DEFAULT_SCHEDULE_MODEL,
  parseRRuleToScheduleModel,
  validateSchedule,
  type ScheduleModel,
} from "@/lib/scheduleBuilder";
import {
  ScheduledTaskApiError,
  type ScheduledTask,
  type ScheduledTaskExecutionTarget,
} from "@/lib/scheduledTasksApi";
import { localTimezone } from "@/lib/timezones";

export function CreateScheduledTaskDialog({
  open,
  onOpenChange,
  initialName,
  initialPrompt,
  editingTask = null,
  currentProjectName = null,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Seed values applied to Name/Prompt when the dialog opens (e.g. from a
   *  "Suggestions" suggestion chip). Omitted → the fields start empty. */
  initialName?: string;
  initialPrompt?: string;
  editingTask?: ScheduledTask | null;
  /** Project NAME of the context the dialog was opened in (e.g. the
   *  `?project=` route param); resolved against the projects list to pre-fill
   *  the Project select on create. */
  currentProjectName?: string | null;
}) {
  const { data: agents } = useAvailableAgents({ enabled: open });
  const { data: customAgents } = useCustomAgents(open);
  const { data: hosts } = useHosts({ enabled: open });
  const { data: projects } = useProjects();
  const info = useServerInfo();
  // Gates the "new sandbox each run" option: only servers that can actually
  // serve a managed launch advertise it (same flag New Chat's sandbox option
  // gates on). "loading" → treat as disabled until /v1/info resolves.
  const managedSandboxesEnabled = info !== "loading" && info.managed_sandboxes_enabled;
  // Provider-named label for the sandbox host option (e.g. "Modal Sandbox"),
  // falling back to the generic "New Sandbox" — same helper the New Chat picker uses.
  const sandboxLabel = sandboxOptionLabel(info !== "loading" ? info.sandbox_provider : null);
  const createMutation = useCreateScheduledTask();
  const updateMutation = useUpdateScheduledTask();
  const isEdit = editingTask !== null;

  const [name, setName] = useState("");
  const [prompt, setPrompt] = useState("");
  const [schedule, setSchedule] = useState<ScheduleModel>(DEFAULT_SCHEDULE_MODEL);

  // ── Agent / harness picker (shared with NewChatDialog) ─────────────────────
  // The picker's "Harnesses" (Claude Code / Codex / Pi …) and "Agents"
  // (Polly / Debby / custom) rows are all real AvailableAgents with ids, so a
  // single `pickedAgentId` covers both cases — for a bare-harness pick it's the
  // `*-native-ui` agent's id, exactly what the interactive dialog sends.
  //
  // Scheduled tasks create sessions from the selected agent. Model + effort are
  // offered for native coding agents and SDK agents that support them (see
  // `showModelEffort` below), reusing lightweight scheduled-local pickers rather
  // than the interactive dialog's 26-prop HarnessConfigModal (bound to
  // smart-routing / cost-control / per-turn model loading — disproportionate
  // for a saved task).
  // "" = unselected → `model_override` / `reasoning_effort` / `permission_mode`
  // are omitted so the fire path uses the agent's configured defaults. Permission
  // mode is offered for Claude Code, Claude SDK, and Codex SDK agents; each fire
  // launches a fresh session, so the whole Claude launch vocabulary is valid —
  // including the launch-only `dontAsk` / `bypassPermissions` — while Codex SDK
  // uses its approval presets.
  const [pickedAgentId, setPickedAgentId] = useState<string | null>(null);
  const [pickedModel, setPickedModel] = useState<string>("");
  const [pickedEffort, setPickedEffort] = useState<string>("");
  const [pickedPermission, setPickedPermission] = useState<string>("");

  // Saved library Agents join the picker as in New Chat. A session row backed by
  // one gives way to it unless it is the edited task's agent or the current pick
  // (a late catalog must not swap the pick); a kept row hides its library twin.
  const agentList = useMemo(() => {
    const libraryAgents = customAgents ?? [];
    const liveTemplateIds = new Set(libraryAgents.map((agent) => agent.id));
    const sourceAgents = (agents ?? []).filter(
      (agent) =>
        !agent.templateId ||
        !liveTemplateIds.has(agent.templateId) ||
        agent.id === editingTask?.agentId ||
        agent.id === pickedAgentId,
    );
    const retainedTemplateIds = new Set(
      sourceAgents
        .map((agent) => agent.templateId)
        .filter((id): id is string => id !== undefined && liveTemplateIds.has(id)),
    );
    return selectableSessionAgents([
      ...sourceAgents,
      ...libraryAgents
        .filter((agent) => !retainedTemplateIds.has(agent.id))
        .map(customAgentForPicker),
    ]);
  }, [agents, customAgents, editingTask?.agentId, pickedAgentId]);
  const harnessEntries = useMemo(
    () => agentList.filter((a) => isNativeCodingAgent(a) || isAcpHarnessAgent(a)),
    [agentList],
  );
  const agentEntries = useMemo(
    () => agentList.filter((a) => !isNativeCodingAgent(a) && !isAcpHarnessAgent(a)),
    [agentList],
  );
  // Resolve the effective selection: the explicit pick if it's still in the
  // list, else the edited task's own agent (which may be hidden from the picker
  // — never silently retarget it), else the first agent (so a fresh picker
  // always has a concrete value).
  const effectiveAgentId =
    (agentList.some((a) => a.id === pickedAgentId)
      ? pickedAgentId
      : isEdit
        ? editingTask?.agentId
        : agentList[0]?.id) ?? null;
  const selectedAgent = agentList.find((a) => a.id === effectiveAgentId);
  const agentLabel = selectedAgent
    ? selectedAgent.display_name
    : isEdit && editingTask
      ? editingTask.agentId
      : "Select agent";
  // Editing a task only rebinds the agent when the user actually picks a
  // different one — the prefill starts equal to the task's own agent.
  const agentChanged =
    isEdit && effectiveAgentId !== null && effectiveAgentId !== editingTask?.agentId;

  // The selected saved Agent's roster, lead first. Read from the catalog row —
  // the picker's `AvailableAgent` carries no members; null → no line.
  const selectedCustomAgent = (customAgents ?? []).find((a) => a.id === effectiveAgentId);
  const customAgentMembers =
    selectedCustomAgent?.members && selectedCustomAgent.members.length > 0
      ? [...selectedCustomAgent.members].sort((a, b) => Number(b.lead) - Number(a.lead))
      : null;

  function handleSelectAgent(agent: AvailableAgent) {
    setPickedAgentId(agent.id);
    // Keep the per-agent settings in step with the pick: they don't transfer
    // across harnesses (model ids and permission vocabularies are harness-bound),
    // so a switch drops them, mirroring the server's clear on a
    // rebind. Landing back on the task's own agent is NOT a switch, so restore
    // the values the dialog opened with — otherwise re-picking the current agent
    // (or switching away and back) would wipe them with no rebind to justify it.
    const backToOriginal = isEdit && agent.id === editingTask?.agentId;
    setPickedModel(backToOriginal ? (editingTask?.modelOverride ?? "") : "");
    setPickedEffort(backToOriginal ? (editingTask?.reasoningEffort ?? "") : "");
    setPickedPermission(backToOriginal ? (editingTask?.permissionMode ?? "") : "");
  }

  // Model + effort are surfaced only for native coding agents that carry the
  // model/effort surface — the same `permissionMode` capability the interactive
  // dialog gates its Model/Effort/Permissions block on (Claude Code) — and for
  // Claude SDK and Codex SDK agents. Agents without it (native harnesses with no
  // model-picker surface) show no model/effort controls, exactly like
  // interactive. Resolved from the full agent list so a task bound to an agent
  // the picker hides still gates on its real capabilities.
  const modelEffortAgent = agents?.find((a) => a.id === effectiveAgentId);
  const nativePermissionMode = nativeAgentHasCapability(modelEffortAgent, "permissionMode");
  const modelEffortHarness: "claude-native" | "claude-sdk" | "codex" | null = nativePermissionMode
    ? "claude-native"
    : modelEffortAgent?.harness === "claude-sdk" || modelEffortAgent?.harness === "codex"
      ? modelEffortAgent.harness
      : null;
  const showModelEffort = modelEffortHarness !== null;

  // ── Nested dropdown dismiss guard ─────────────────────────────────────────
  // The agent picker and host/schedule Selects portal dropdowns OUTSIDE DialogContent.
  // Two dismiss paths leak through to the Dialog and close the whole modal:
  //   (a) picking an option — the closing pointerdown lands in the popper;
  //   (b) clicking empty modal body (or the trigger) while a dropdown is open —
  //       the target is the dialog body, and the portal ALSO emits a
  //       focus-outside as it unmounts.
  // Target-sniffing (isInsidePopper) only covers (a). To cover (b) too, track
  // whether ANY dropdown is open, and keep the guard armed for a short grace
  // window after it closes so the trailing pointerup/focus transition that
  // Radix reports as "interact outside" is absorbed. See `guardDialogDismiss`.
  const selectOpenCountRef = useRef(0);
  const selectClosedAtRef = useRef(0);
  const [workspaceBrowserOpen, setWorkspaceBrowserOpen] = useState(false);
  function handleSelectOpenChange(isOpen: boolean) {
    if (isOpen) {
      selectOpenCountRef.current += 1;
    } else {
      selectOpenCountRef.current = Math.max(0, selectOpenCountRef.current - 1);
      selectClosedAtRef.current = Date.now();
    }
  }
  /** preventDefault the Dialog's outside-dismiss ONLY for the narrow nested-Select
   * cases — a click inside portalled dropdown content (path a) or while a dropdown
   * is open (path b),
   * plus a short grace window for the trailing focus-outside a dropdown emits as
   * it unmounts. A genuine click on the backdrop OVERLAY always dismisses: its target
   * is the overlay itself (never a popper), so we let it through even inside the
   * grace window — this is the fix for backdrop-click-to-close being swallowed.
   * Escape + Cancel are unaffected (they don't route through this guard). */
  function guardDialogDismiss(event: { target: EventTarget | null; preventDefault: () => void }) {
    if (workspaceBrowserOpen) {
      event.preventDefault();
      return;
    }
    if (
      shouldGuardDialogDismiss(event.target, {
        selectOpen: selectOpenCountRef.current > 0,
        msSinceSelectClose: Date.now() - selectClosedAtRef.current,
      })
    ) {
      event.preventDefault();
    }
  }
  // Optional pinned host/workspace. "" = unset (server resolves at fire time).
  const [hostId, setHostId] = useState<string>("");
  const [workspace, setWorkspace] = useState<string>("");
  // Project whose calling defaults apply at fire time. Until the user touches
  // the select, the value follows the surrounding context (or the edited
  // task), so a late projects-list load still pre-fills it.
  const [projectId, setProjectId] = useState<string>("");
  const [projectTouched, setProjectTouched] = useState(false);
  const defaultProjectId =
    editingTask !== null
      ? (editingTask.projectId ?? "")
      : ((projects ?? []).find((project) => project.name === currentProjectName)?.id ?? "");
  const effectiveProjectId = projectTouched ? projectId : defaultProjectId;
  // When on, each fire runs in a FRESH managed sandbox (execution_target =
  // "managed_sandbox") instead of a connected host; hides the host/workspace
  // pickers. Only offered when the server advertises managed sandboxes.
  const [sandboxMode, setSandboxMode] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [scheduleUnsupported, setScheduleUnsupported] = useState(false);

  // Seed Name/Prompt on the closed→open transition ONLY. Keying off the
  // transition (not `open` being true) means we never clobber the user's edits
  // while the dialog stays open. Each fresh open is AUTHORITATIVE — the fields
  // are set to the initial values, or cleared to "" when none are supplied — so
  // a stale prefill can never leak into a subsequent manual open regardless of
  // how the prior instance was closed, and switching chips reseeds.
  const wasOpen = useRef(false);
  useEffect(() => {
    if (open && !wasOpen.current) {
      if (editingTask) {
        const parsedSchedule = parseRRuleToScheduleModel(editingTask.rrule);
        setName(editingTask.name);
        setPrompt(editingTask.prompt);
        setPickedAgentId(editingTask.agentId);
        // Prefill the model/effort/permission controls from the loaded task; null → "".
        setPickedModel(editingTask.modelOverride ?? "");
        setPickedEffort(editingTask.reasoningEffort ?? "");
        setPickedPermission(editingTask.permissionMode ?? "");
        setSchedule(parsedSchedule ?? DEFAULT_SCHEDULE_MODEL);
        setScheduleUnsupported(parsedSchedule === null);
        setHostId(editingTask.hostId ?? "");
        setWorkspace(editingTask.workspace ?? "");
        setSandboxMode(editingTask.executionTarget === "managed_sandbox");
        setProjectId("");
        setProjectTouched(false);
      } else {
        setName(initialName ?? "");
        setPrompt(initialPrompt ?? "");
        setPickedAgentId(null);
        setPickedModel("");
        setPickedEffort("");
        setPickedPermission("");
        setSchedule(DEFAULT_SCHEDULE_MODEL);
        setScheduleUnsupported(false);
        setHostId("");
        setWorkspace("");
        setSandboxMode(false);
        setProjectId("");
        setProjectTouched(false);
      }
      setError(null);
    }
    wasOpen.current = open;
  }, [open, initialName, initialPrompt, editingTask]);

  const hostOptions = hosts ?? [];
  // Label-only folders have no first-class id, so they cannot key a task's
  // project (the server default-fills those from the creating session).
  const projectOptions = (projects ?? []).flatMap((project) =>
    project.id === null ? [] : [{ id: project.id, name: project.name }],
  );
  const preservePinnedHost = isEdit && editingTask?.hostId != null;
  // The resolved Host for the pinned id, or undefined when none is pinned.
  const selectedHost = hostId === "" ? undefined : hostOptions.find((h) => h.host_id === hostId);
  // Host whose `configured_harnesses` drives the picker's "needs setup" badges.
  // Host is optional on scheduled tasks; unset means resolve the connected host
  // at fire time, so we must not require the user to pin one before the
  // readiness affordance appears. Fall back to the first ONLINE host for badge
  // computation only — this does NOT change the form's `hostId` value (which
  // stays "" = resolve-at-fire); it just gives the picker a readiness map so
  // unconfigured agents show "needs setup" immediately, matching how the
  // interactive New Chat dialog auto-selects the first online host on mount.
  const badgeHost =
    selectedHost ?? hostOptions.find((h) => h.status === "online") ?? hostOptions[0];

  // A workspace is only valid with a host — mirror the server's pairing rule so
  // the user gets inline feedback instead of a 400. In sandbox mode there is no
  // host/workspace pairing at all, so the rule doesn't apply.
  const workspaceWithoutHost = !sandboxMode && workspace.trim() !== "" && hostId === "";
  // A saved Agent runs from its bundle on a connected computer (a server 400 on
  // sandbox targets): the sandbox option is disabled while one is picked, and a
  // pick beside an already-selected sandbox blocks submit until a host is chosen.
  const libraryAgentSelected = effectiveAgentId?.startsWith("ca_") ?? false;
  const sandboxAgentConflict = sandboxMode && libraryAgentSelected;
  // Block submit on an invalid schedule (bad interval, empty multi-select) so
  // the form never posts an RRULE the server's validate_rrule would 400.
  const scheduleInvalid = scheduleUnsupported || validateSchedule(schedule) !== null;
  const mutationPending = createMutation.isPending || updateMutation.isPending;
  const canSubmit =
    name.trim() !== "" &&
    prompt.trim() !== "" &&
    (isEdit || effectiveAgentId !== null) &&
    !workspaceWithoutHost &&
    !sandboxAgentConflict &&
    !scheduleInvalid &&
    !mutationPending;

  function resetForm() {
    setName("");
    setPrompt("");
    setPickedAgentId(null);
    setPickedModel("");
    setPickedEffort("");
    setPickedPermission("");
    setSchedule(DEFAULT_SCHEDULE_MODEL);
    setHostId("");
    setWorkspace("");
    setWorkspaceBrowserOpen(false);
    setSandboxMode(false);
    setProjectId("");
    setProjectTouched(false);
    setError(null);
    setScheduleUnsupported(false);
  }

  function handleOpenChange(next: boolean) {
    if (!next) resetForm();
    onOpenChange(next);
  }

  async function handleSubmit() {
    setError(null);
    try {
      // A sandbox task runs hostless (fresh sandbox per fire), so host/workspace
      // are omitted. Only send executionTarget when it's meaningful: on create
      // when sandbox is chosen (connected_host is the server default), and on
      // edit only when it actually changed — mirroring how agentId is threaded.
      const executionTarget: ScheduledTaskExecutionTarget = sandboxMode
        ? "managed_sandbox"
        : "connected_host";
      const executionTargetChanged = editingTask
        ? editingTask.executionTarget !== executionTarget
        : sandboxMode;
      const input = {
        name: name.trim(),
        prompt: prompt.trim(),
        rrule: buildRRule(schedule),
        timezone: editingTask?.timezone ?? localTimezone(),
        ...(executionTargetChanged ? { executionTarget } : {}),
        ...(!sandboxMode && hostId !== "" ? { hostId } : {}),
        ...(!sandboxMode && hostId !== "" && workspace.trim() !== ""
          ? { workspace: workspace.trim() }
          : {}),
      };
      // The task's project is threaded only when it changed (a create with no
      // project omits it, so the server can pre-fill from the creating
      // session); a cleared select sends an explicit null.
      const projectChanged = effectiveProjectId !== (editingTask?.projectId ?? "");
      const projectPatch = projectChanged
        ? { projectId: effectiveProjectId === "" ? null : effectiveProjectId }
        : {};
      if (editingTask) {
        // When the agent supports them: a switch resends every non-empty selection
        // (server clears what the PATCH omits); otherwise only changes, `null` when cleared.
        const overrides: {
          modelOverride?: string | null;
          reasoningEffort?: string | null;
          permissionMode?: string | null;
        } = {};
        if (showModelEffort) {
          if (agentChanged) {
            if (pickedModel !== "") overrides.modelOverride = pickedModel;
            if (pickedEffort !== "") overrides.reasoningEffort = pickedEffort;
            if (pickedPermission !== "") overrides.permissionMode = pickedPermission;
          } else {
            if (pickedModel !== (editingTask.modelOverride ?? "")) {
              overrides.modelOverride = pickedModel === "" ? null : pickedModel;
            }
            if (pickedEffort !== (editingTask.reasoningEffort ?? "")) {
              overrides.reasoningEffort = pickedEffort === "" ? null : pickedEffort;
            }
            if (pickedPermission !== (editingTask.permissionMode ?? "")) {
              overrides.permissionMode = pickedPermission === "" ? null : pickedPermission;
            }
          }
        }
        await updateMutation.mutateAsync({
          id: editingTask.id,
          input: {
            ...input,
            ...overrides,
            ...projectPatch,
            // Only on a real switch: sending the unchanged agent is a server-side
            // no-op, but omitting it keeps the PATCH honest about what changed.
            ...(agentChanged && effectiveAgentId !== null ? { agentId: effectiveAgentId } : {}),
          },
        });
      } else {
        if (effectiveAgentId === null) return;
        await createMutation.mutateAsync({
          ...input,
          agentId: effectiveAgentId,
          ...projectPatch,
          // Include an override only when the agent supports model/effort AND the
          // user picked a non-default value; an unselected control is omitted so
          // the create uses the agent's configured defaults.
          ...(showModelEffort && pickedModel !== "" ? { modelOverride: pickedModel } : {}),
          ...(showModelEffort && pickedEffort !== "" ? { reasoningEffort: pickedEffort } : {}),
          ...(showModelEffort && pickedPermission !== ""
            ? { permissionMode: pickedPermission }
            : {}),
        });
      }
      handleOpenChange(false);
    } catch (err) {
      setError(
        err instanceof ScheduledTaskApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : isEdit
              ? "Couldn't update the automation."
              : "Couldn't create the automation.",
      );
    }
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        className="flex max-h-[90vh] flex-col overflow-hidden p-0 sm:max-w-[560px]"
        data-testid="create-scheduled-task-dialog"
        // Keep a nested Select's dismiss (pick an option, OR click empty modal
        // body / trigger while it's open) from closing the whole Dialog. See
        // `guardDialogDismiss` — it covers both the popper-target path and the
        // Select-open + focus-outside path, while leaving real backdrop clicks
        // and Escape to close as normal.
        onPointerDownOutside={guardDialogDismiss}
        onInteractOutside={guardDialogDismiss}
      >
        <DialogHeader className="shrink-0 px-6 pt-6 pb-0">
          <DialogTitle>{isEdit ? "Edit automation" : "New automation"}</DialogTitle>
          <DialogDescription>
            {isEdit
              ? "Update this recurring agent session."
              : "Runs an agent session on a recurring schedule."}
          </DialogDescription>
        </DialogHeader>

        <div
          className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto px-6 py-4"
          data-testid="scheduled-task-dialog-body"
        >
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="task-name">Name</Label>
            <Input
              id="task-name"
              value={name}
              placeholder="daily-brief"
              data-testid="task-name-input"
              className="text-ui"
              onChange={(e) => setName(e.target.value)}
            />
          </div>

          <div className="flex flex-col gap-1.5">
            <Label htmlFor="task-prompt">Prompt</Label>
            <Textarea
              id="task-prompt"
              value={prompt}
              rows={3}
              placeholder="What should the agent do each run?"
              data-testid="task-prompt-input"
              componentId="tasks.scheduled.prompt"
              // No native resize grip — match the clean styling of the other fields.
              className="resize-none text-ui"
              onChange={(e) => setPrompt(e.target.value)}
            />
          </div>

          <div className="flex flex-col gap-1.5">
            {/* "Runs with" — the unified picker offers BOTH harnesses (Claude
                Code / Codex / Pi …) and agents (Polly / Debby), so "Agent" would
                be misleading. */}
            <Label>Runs with</Label>
            <div data-testid="task-agent-picker">
              <AgentHarnessPicker
                agentEntries={agentEntries}
                harnessEntries={harnessEntries}
                effectiveAgentId={effectiveAgentId}
                agentLabel={agentLabel}
                hasAgents={agentList.length > 0}
                // Drives the per-row "needs setup" badges from
                // host.configured_harnesses. Uses the pinned host if any, else
                // falls back to the first online host so the badges show in the
                // fresh/default state (host is optional here — see `badgeHost`).
                host={badgeHost}
                onSelectAgent={handleSelectAgent}
                pendingAgent={null}
                pendingAgentId="__unused_pending_agent__"
                onSelectPending={() => {}}
                // Custom-agent creation is inert until there is a way to
                // persist a new agent independently of creating a session.
                onCreateCustomAgent={() => {}}
                sandboxSelected={false}
                // Forward the dropdown open/close into the dialog's outside-click
                // dismiss guard so opening the picker doesn't close the modal.
                onOpenChange={handleSelectOpenChange}
                // This picker is nested inside a Dialog. Radix DropdownMenu's
                // default modal mode can turn an inside-dialog click into a
                // parent Dialog outside interaction while the menu dismisses.
                dropdownModal={false}
                // The shared menu caps against available viewport height, so
                // constrained screens scroll without clipping taller screens.
                contentClassName="w-80"
                // Full-width trigger → left-align the menu's edge to it.
                contentAlign="start"
                // Match the sibling <Select> fields (Frequency / host): full
                // width, bordered, h-8, normal foreground text — not the compact
                // muted ghost styling the composer footer uses.
                triggerClassName="h-8 w-full justify-between rounded-lg border border-input bg-transparent px-2.5 text-foreground hover:bg-transparent hover:text-foreground dark:bg-input/30"
                triggerLabelClassName="max-w-none text-ui"
              />
            </div>
            {customAgentMembers && (
              // Saved Agent roster: names only, lead first (Model / Effort are per-member).
              <p className="text-sm text-muted-foreground" data-testid="task-agent-members">
                {customAgentMembers
                  .map((member) => (member.lead ? `${member.name} (Lead)` : member.name))
                  .join(", ")}
              </p>
            )}
            {agentChanged && (
              <p className="text-sm text-muted-foreground">
                Future runs use {agentLabel}; past runs keep the agent they ran with
              </p>
            )}
            {!showModelEffort && (
              <p className="text-sm text-muted-foreground">
                Uses this agent&apos;s default model, effort, and permission settings
              </p>
            )}
          </div>

          {/* Model + reasoning effort + permission mode — for native coding
              agents that carry the model/effort surface (Claude Code) and
              Claude SDK / Codex SDK agents. Unselected controls fall back to
              the agent's configured defaults. */}
          {showModelEffort && (
            <div data-testid="task-model-effort-field">
              <ModelEffortFields
                harness={modelEffortHarness}
                model={pickedModel}
                effort={pickedEffort}
                permissionMode={pickedPermission}
                hostId={hostId}
                onModelChange={setPickedModel}
                onEffortChange={setPickedEffort}
                onPermissionModeChange={setPickedPermission}
                onSelectOpenChange={handleSelectOpenChange}
              />
              <p className="mt-1.5 text-sm text-muted-foreground">
                Leave these controls on their defaults to use the agent&apos;s configured model,
                effort, and permission mode. Automations run unattended, so prompting modes (Manual
                or Plan for Claude; Default or Read only for Codex SDK) will wait for approval that
                never comes.
              </p>
            </div>
          )}

          <ScheduleFields
            model={schedule}
            onChange={(next) => {
              setScheduleUnsupported(false);
              setSchedule(next);
            }}
            onSelectOpenChange={handleSelectOpenChange}
          />
          {scheduleUnsupported && (
            <p className="text-sm text-destructive" role="alert">
              This schedule can&apos;t be edited in this form yet.
            </p>
          )}

          {/* Timezone is inferred from the browser (localTimezone via Intl) and
              intentionally has no visible control. It is still sent in the create
              payload so the schedule evaluates in the user's local zone. */}

          {/* Project whose calling defaults apply at each fire. Pre-filled
              from the surrounding context (or the edited task) until the user
              changes it; "No project" leaves the master table in charge. */}
          <div className="flex flex-col gap-1.5" data-testid="task-project-field">
            <Label htmlFor="task-project">Project (optional)</Label>
            <Select
              value={effectiveProjectId === "" ? UNSET_PROJECT : effectiveProjectId}
              componentId="tasks.scheduled.project"
              onValueChange={(v) => {
                setProjectTouched(true);
                setProjectId(v === UNSET_PROJECT ? "" : v);
              }}
              onOpenChange={handleSelectOpenChange}
            >
              <SelectTrigger
                id="task-project"
                data-testid="task-project-trigger"
                className="w-full"
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent position="popper" align="start">
                <SelectItem value={UNSET_PROJECT}>No project</SelectItem>
                {projectOptions.map((project) => (
                  <SelectItem key={project.id} value={project.id}>
                    {project.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-sm text-muted-foreground">
              Runs on this project&apos;s calling defaults for the host; no project uses the master
              table.
            </p>
          </div>

          {/* Host / execution target. When the server offers managed sandboxes,
              a "New Sandbox" entry sits in the same picker (mirroring the New Chat
              composer): choosing it provisions a FRESH sandbox per fire
              (execution_target=managed_sandbox, hostless) instead of a connected
              host. Otherwise the picker is the optional connected-host pin. */}
          <div className="flex flex-col gap-1.5" data-testid="task-host-field">
            <Label htmlFor="task-host">Host (optional)</Label>
            <Select
              value={sandboxMode ? SANDBOX_HOST : hostId === "" ? UNSET_HOST : hostId}
              componentId="tasks.scheduled.host"
              onValueChange={(v) => {
                if (v === SANDBOX_HOST) {
                  // A sandbox run is hostless; drop any pinned host/workspace.
                  setSandboxMode(true);
                  setHostId("");
                  setWorkspace("");
                  setWorkspaceBrowserOpen(false);
                  return;
                }
                if (preservePinnedHost && v === UNSET_HOST) return;
                setSandboxMode(false);
                const next = v === UNSET_HOST ? "" : v;
                setHostId(next);
                // Clearing the host invalidates any pinned workspace.
                if (next === "") setWorkspace("");
                setWorkspaceBrowserOpen(false);
              }}
              onOpenChange={handleSelectOpenChange}
            >
              <SelectTrigger id="task-host" data-testid="task-host-trigger" className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent position="popper" align="start">
                <SelectItem value={UNSET_HOST} disabled={preservePinnedHost}>
                  Resolve at fire time
                </SelectItem>
                {(managedSandboxesEnabled || sandboxMode) && (
                  <SelectItem
                    value={SANDBOX_HOST}
                    disabled={libraryAgentSelected}
                    data-testid="task-host-sandbox-option"
                  >
                    {sandboxLabel}
                  </SelectItem>
                )}
                {hostOptions.map((host) => (
                  <SelectItem key={host.host_id} value={host.host_id}>
                    {host.name} {host.status === "offline" ? "(offline)" : ""}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-sm text-muted-foreground">
              {sandboxMode
                ? "Provisions a fresh sandbox for each run. Shutdown follows the server’s sandbox configuration."
                : "Leave unset to run on your connected host when the task fires."}
            </p>
            {libraryAgentSelected && (managedSandboxesEnabled || sandboxMode) && (
              <p className="text-sm text-muted-foreground" data-testid="task-host-sandbox-note">
                Custom agents require a connected computer
              </p>
            )}
          </div>

          {!sandboxMode && hostId !== "" && (
            <div className="flex flex-col gap-1.5">
              <Label>Workspace (optional)</Label>
              <p className="text-sm text-muted-foreground">
                Starts at this Host&apos;s pinned folder, then its home directory. Pick a directory
                for this task.
              </p>
              <Button
                type="button"
                variant="outline"
                className="w-full justify-start gap-2"
                onClick={() => setWorkspaceBrowserOpen(true)}
                data-testid="task-workspace-browse"
              >
                <FolderOpenIcon className="size-4 text-muted-foreground" />
                <span className="truncate">{workspace || "Browse folders…"}</span>
              </Button>
              <WorkspacePickerDialog
                open={workspaceBrowserOpen}
                onOpenChange={setWorkspaceBrowserOpen}
                hostId={hostId}
                initialPath={workspace}
                onConfirm={setWorkspace}
              />
              {workspace && (
                <p className="truncate font-mono text-sm text-muted-foreground">{workspace}</p>
              )}
            </div>
          )}

          {workspaceWithoutHost && (
            <p
              className="flex items-center gap-1.5 text-sm text-destructive"
              data-testid="workspace-without-host-error"
            >
              <TriangleAlertIcon className="size-3.5 shrink-0" />
              Pick a host before pinning a workspace.
            </p>
          )}

          {error && (
            <div
              role="alert"
              data-testid="create-error"
              className="flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive"
            >
              <TriangleAlertIcon className="mt-0.5 size-3.5 shrink-0" />
              <span>{error}</span>
            </div>
          )}
        </div>

        <DialogFooter className="mx-0 mb-0 shrink-0 rounded-none border-t-0 bg-transparent px-6 py-4 sm:justify-end">
          <Button
            variant="outline"
            onClick={() => handleOpenChange(false)}
            componentId="tasks.scheduled.cancel"
          >
            Cancel
          </Button>
          <Button
            onClick={handleSubmit}
            loading={mutationPending}
            disabled={!canSubmit}
            data-testid="create-scheduled-task-submit"
            componentId="tasks.scheduled.save"
          >
            {isEdit ? "Save changes" : "Create task"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** Sentinel Select value for "no pinned host" — Radix Select disallows "". */
const UNSET_HOST = "__unset_host__";

/** Sentinel Select value for "no project" — Radix Select disallows "". */
const UNSET_PROJECT = "__unset_project__";

/** Sentinel Select value for the "New Sandbox" option (execution_target=managed_sandbox). */
const SANDBOX_HOST = "__sandbox__";

// The nested-dropdown dismiss guard now lives in a dependency-free module so
// other dialogs (project settings) can reuse it without pulling this file's
// module graph. Re-exported here to keep existing imports and tests stable.
export { isBackdropOverlay, isInsidePopper, shouldGuardDialogDismiss };
