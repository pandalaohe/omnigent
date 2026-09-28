// Tests for the manual create dialog: submit stays disabled until the required
// fields are filled, the workspace-without-host pairing rule surfaces inline,
// and a valid submit calls the create mutation with the RRULE built from the
// schedule fields (host/workspace omitted when unset).
//
// The agent/host hooks and the create mutation are mocked; WorkspacePicker is
// stubbed (its filesystem browsing is out of scope here).

import type * as NativeCodingAgentsModule from "@/lib/nativeCodingAgents";
import type * as ScheduledTasksApiModule from "@/lib/scheduledTasksApi";
import type * as UseConversationsModule from "@/hooks/useConversations";

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  CreateScheduledTaskDialog,
  isInsidePopper,
  shouldGuardDialogDismiss,
} from "./CreateScheduledTaskDialog";
import * as agentsHook from "@/hooks/useAvailableAgents";
import * as customAgentsApi from "@/lib/customAgentsApi";
import * as hostsHook from "@/hooks/useHosts";
import * as scheduledHooks from "@/hooks/useScheduledTasks";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { SERVER_INFO_OFFLINE_FALLBACK } from "@/lib/bootCapabilities";
import { useProjects } from "@/hooks/useConversations";

vi.mock("@/hooks/useConversations", async (importOriginal) => ({
  ...(await importOriginal<typeof UseConversationsModule>()),
  useProjects: vi.fn(),
}));

vi.mock("@/hooks/useAvailableAgents", () => ({ useAvailableAgents: vi.fn() }));
// Saved library Agents: keep the real `customAgentForPicker` row mapping and
// mock only the catalog hook, so the dialog's merge is exercised as shipped.
vi.mock("@/lib/customAgentsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof customAgentsApi>()),
  useCustomAgents: vi.fn(),
}));
// useHostModelOptions is consumed by the ModelEffortFields sub-form (model
// dropdown source). Returning no data leaves no-host Claude aliases available.
vi.mock("@/hooks/useHosts", () => ({
  useHosts: vi.fn(),
  useHostModelOptions: vi.fn(() => ({ data: undefined })),
}));
vi.mock("@/hooks/useScheduledTasks", () => ({
  useCreateScheduledTask: vi.fn(),
  useUpdateScheduledTask: vi.fn(),
}));
vi.mock("@/lib/agentLabels", () => ({ useBrainHarnessLabels: () => ({}) }));
vi.mock("@/shell/WorkspacePicker", () => ({
  isNavigablePath: (path: string) => path.startsWith("/"),
  HostWorkspacePicker: ({ onSelect }: { onSelect?: (p: string) => void }) => (
    <button type="button" onClick={() => onSelect?.("/home/me/repo")}>
      pick-workspace
    </button>
  ),
}));

// Stub the heavy shared picker: expose buttons that drive the exact callbacks
// the dialog wires (select an entry, set model/effort knobs, toggle open state)
// so we can test the dialog's selection→payload mapping + dismiss-guard without
// the real Radix menu / QueryClient. Two entries mirror the two mapping cases:
// a bare harness (claude-native-ui) and a plain agent (polly).
vi.mock("@/shell/NewChatDialog", () => ({
  AgentHarnessPicker: ({
    onSelectAgent,
    onOpenChange,
    effectiveAgentId,
    agentLabel,
    host,
    dropdownModal,
    contentClassName,
    harnessEntries,
    agentEntries,
  }: {
    onSelectAgent: (a: AvailableAgent) => void;
    onOpenChange?: (open: boolean) => void;
    effectiveAgentId: string | null;
    agentLabel: string;
    host?: { host_id: string } | null;
    dropdownModal?: boolean;
    contentClassName?: string;
    harnessEntries: AvailableAgent[];
    agentEntries: AvailableAgent[];
  }) => (
    <div
      data-testid="agent-picker-stub"
      data-effective={effectiveAgentId ?? ""}
      // Surface the host the dialog feeds the picker for badge computation, so
      // a test can assert it's populated even when no host is pinned.
      data-badge-host={host?.host_id ?? ""}
      data-dropdown-modal={dropdownModal === false ? "false" : "true"}
      data-content-class={contentClassName}
      data-harness-entries={harnessEntries.map((agent) => agent.name).join(",")}
      data-agent-entries={agentEntries.map((agent) => agent.name).join(",")}
    >
      <span>{agentLabel}</span>
      <button
        type="button"
        data-testid="pick-harness-claude"
        onClick={() =>
          onSelectAgent({
            id: "ag_claude_native",
            name: "claude-native-ui",
            display_name: "Claude Code",
            description: null,
            harness: "claude-native",
            skills: [],
          })
        }
      >
        pick claude harness
      </button>
      <button
        type="button"
        data-testid="pick-agent-polly"
        onClick={() =>
          onSelectAgent({
            id: "ag_1",
            name: "polly",
            display_name: "Polly",
            description: null,
            harness: "claude-sdk",
            skills: [],
          })
        }
      >
        pick polly
      </button>
      <button
        type="button"
        data-testid="pick-agent-codex"
        onClick={() =>
          onSelectAgent({
            id: "ag_codex",
            name: "codex-agent",
            display_name: "Codex agent",
            description: null,
            harness: "codex",
            skills: [],
          })
        }
      >
        pick codex agent
      </button>
      <button type="button" data-testid="picker-open" onClick={() => onOpenChange?.(true)}>
        open
      </button>
      <button type="button" data-testid="picker-close" onClick={() => onOpenChange?.(false)}>
        close
      </button>
      {/* Every entry the dialog feeds the picker is pickable — mirrors the real
          menu's onSelectAgent for rows the fixed buttons above don't cover. */}
      {agentEntries.map((agent) => (
        <button
          key={agent.id}
          type="button"
          data-testid={`pick-${agent.id}`}
          onClick={() => onSelectAgent(agent)}
        >
          {agent.display_name}
        </button>
      ))}
    </div>
  ),
}));

// nativeAgentHasCapability(claude-native, "permissionMode") must be true so the
// model/effort knobs map onto the payload; Polly has no native capability.
vi.mock("@/lib/nativeCodingAgents", async (orig) => {
  const actual = await orig<typeof NativeCodingAgentsModule>();
  return {
    ...actual,
    isNativeCodingAgent: (a: AvailableAgent) => a?.name === "claude-native-ui",
    nativeAgentHasCapability: (a: AvailableAgent | undefined | null, cap: string) =>
      a?.name === "claude-native-ui" && cap === "permissionMode",
  };
});

const AGENTS: AvailableAgent[] = [
  {
    id: "ag_1",
    name: "polly",
    display_name: "Polly",
    description: null,
    harness: "claude-sdk",
    skills: [],
  },
  {
    id: "ag_claude_native",
    name: "claude-native-ui",
    display_name: "Claude Code",
    description: null,
    harness: "claude-native",
    skills: [],
  },
  {
    id: "ag_codex",
    name: "codex-agent",
    display_name: "Codex agent",
    description: null,
    harness: "codex",
    skills: [],
  },
];

// A saved multi-member Agent. The roster is deliberately not lead-first so the
// dialog's own lead-first ordering is under test; the members carry models and
// efforts that must NOT surface anywhere in the scheduled dialog.
const CUSTOM_AGENTS: customAgentsApi.CustomAgent[] = [
  {
    id: "ca_release_crew",
    name: "Release crew",
    description: null,
    harness: null,
    model: null,
    members: [
      {
        name: "executor",
        description: null,
        harness: "codex",
        model: "gpt-5.5",
        reasoning_effort: "xhigh",
        lead: false,
      },
      {
        name: "architect",
        description: null,
        harness: "claude-sdk",
        model: "opus",
        reasoning_effort: "high",
        lead: true,
      },
      {
        name: "reviewer",
        description: null,
        harness: "codex",
        model: null,
        reasoning_effort: null,
        lead: false,
      },
    ],
    version: 1,
    created_at: 1,
    updated_at: null,
  },
];

const mutateAsync = vi.fn();
const updateMutateAsync = vi.fn();

function mockCustomAgents(data: customAgentsApi.CustomAgent[] = []) {
  vi.mocked(customAgentsApi.useCustomAgents).mockReturnValue({
    data,
  } as unknown as ReturnType<typeof customAgentsApi.useCustomAgents>);
}

beforeEach(() => {
  mutateAsync.mockReset().mockResolvedValue({ id: "st_new" });
  updateMutateAsync.mockReset().mockResolvedValue({ id: "st_1" });
  mockCustomAgents();
  vi.mocked(useProjects).mockReturnValue({ data: [] } as unknown as ReturnType<typeof useProjects>);
  vi.mocked(hostsHook.useHostModelOptions).mockReturnValue({
    data: undefined,
  } as ReturnType<typeof hostsHook.useHostModelOptions>);
  vi.mocked(agentsHook.useAvailableAgents).mockReturnValue({
    data: AGENTS,
  } as unknown as ReturnType<typeof agentsHook.useAvailableAgents>);
  vi.mocked(hostsHook.useHosts).mockReturnValue({
    data: [{ host_id: "host_1", name: "laptop", owner: "me", status: "online" }],
  } as unknown as ReturnType<typeof hostsHook.useHosts>);
  vi.mocked(scheduledHooks.useCreateScheduledTask).mockReturnValue({
    mutateAsync,
    isPending: false,
  } as unknown as ReturnType<typeof scheduledHooks.useCreateScheduledTask>);
  vi.mocked(scheduledHooks.useUpdateScheduledTask).mockReturnValue({
    mutateAsync: updateMutateAsync,
    isPending: false,
  } as unknown as ReturnType<typeof scheduledHooks.useUpdateScheduledTask>);
});

afterEach(() => cleanup());

function renderDialog(onOpenChange: (open: boolean) => void = vi.fn()) {
  return render(<CreateScheduledTaskDialog open onOpenChange={onOpenChange} />);
}

/** Render inside a CapabilitiesProvider that advertises managed sandboxes. */
function renderWithSandboxes(onOpenChange: (open: boolean) => void = vi.fn()) {
  return render(
    <CapabilitiesProvider
      info={{ ...SERVER_INFO_OFFLINE_FALLBACK, managed_sandboxes_enabled: true }}
    >
      <CreateScheduledTaskDialog open onOpenChange={onOpenChange} />
    </CapabilitiesProvider>,
  );
}

function scheduledTask(overrides: Partial<ScheduledTasksApiModule.ScheduledTask> = {}) {
  return {
    id: "st_1",
    name: "Morning brief",
    prompt: "Summarize overnight activity",
    rrule: "FREQ=DAILY;BYHOUR=8;BYMINUTE=30",
    ownerUserId: null,
    agentId: "ag_1",
    timezone: "America/Los_Angeles",
    createdAt: 1,
    updatedAt: 2,
    modelOverride: null,
    reasoningEffort: null,
    permissionMode: null,
    workspace: null,
    hostId: null,
    executionTarget: "connected_host",
    state: "active",
    lastRunAt: null,
    lastRunStatus: null,
    lastRunConversationId: null,
    nextRunAt: null,
    ...overrides,
  } satisfies ScheduledTasksApiModule.ScheduledTask;
}

describe("agent picker readiness (needs-setup badges)", () => {
  it("shows the model + effort controls for the default (Claude-native) agent", () => {
    renderDialog();
    // The default effective agent is claude-native-ui (a capable native coding
    // agent), so the model/effort pickers render and the "uses defaults" hint —
    // shown only when the controls are hidden — is absent.
    expect(screen.getByTestId("task-model-effort-field")).toBeInTheDocument();
    expect(screen.getByTestId("task-model-trigger")).toBeInTheDocument();
    expect(screen.getByTestId("task-effort-trigger")).toBeInTheDocument();
    expect(
      screen.queryByText("Uses this agent's default model, effort, and permission settings"),
    ).not.toBeInTheDocument();
  });

  it("feeds the picker a fallback online host so 'needs setup' badges show with no host pinned", () => {
    renderDialog();
    // Fresh state: no host pinned, but the dialog still passes the first online
    // host to the picker for badge computation (badgeHost fallback) so the
    // "needs setup" affordance isn't invisible until the user picks a host.
    const picker = screen.getByTestId("agent-picker-stub");
    expect(picker.getAttribute("data-badge-host")).toBe("host_1");
  });

  it("embeds the agent dropdown in non-modal mode so inside-dialog clicks only close the menu", () => {
    renderDialog();
    expect(screen.getByTestId("agent-picker-stub")).toHaveAttribute("data-dropdown-modal", "false");
  });

  it("uses the shared viewport-aware menu height instead of a fixed scroll cap", () => {
    renderDialog();
    expect(screen.getByTestId("agent-picker-stub")).toHaveAttribute("data-content-class", "w-80");
  });

  it("groups generic ACP choices with harnesses like the main composer", () => {
    vi.mocked(agentsHook.useAvailableAgents).mockReturnValue({
      data: [
        ...AGENTS,
        {
          id: "ag_jcode",
          name: "jcode",
          display_name: "Jcode",
          description: null,
          harness: "jcode",
          skills: [],
          acpHarness: true,
        },
        {
          id: "ag_grok",
          name: "grok",
          display_name: "Grok Build",
          description: null,
          harness: "grok",
          skills: [],
          acpHarness: true,
        },
      ],
    } as unknown as ReturnType<typeof agentsHook.useAvailableAgents>);

    renderDialog();

    const picker = screen.getByTestId("agent-picker-stub");
    expect(picker).toHaveAttribute("data-harness-entries", "claude-native-ui,jcode,grok");
    expect(picker).toHaveAttribute("data-agent-entries", "polly,codex-agent");
  });
});

describe("CreateScheduledTaskDialog validation", () => {
  it("keeps submit disabled until name + prompt are set (agent defaults to the first)", () => {
    renderDialog();
    const submit = screen.getByTestId("create-scheduled-task-submit");
    // Agent is never blank — the picker resolves a default (first agent) — so
    // only name + prompt gate submit.
    expect(submit).toBeDisabled();

    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "Nightly" } });
    expect(submit).toBeDisabled();
    fireEvent.change(screen.getByTestId("task-prompt-input"), { target: { value: "Do it" } });
    expect(submit).toBeEnabled();
  });

  it("pins a workspace only after the full-screen browser confirms", () => {
    renderDialog();
    const hostTrigger = screen.getByTestId("task-host-trigger");
    fireEvent.pointerDown(hostTrigger, new MouseEvent("pointerdown", { bubbles: true, button: 0 }));
    fireEvent.click(hostTrigger);
    fireEvent.click(screen.getByRole("option", { name: /laptop/ }));

    fireEvent.click(screen.getByTestId("task-workspace-browse"));
    expect(screen.getByTestId("workspace-picker-dialog")).toBeInTheDocument();
    expect(screen.queryByText("/home/me/repo")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "pick-workspace" }));

    expect(screen.queryByTestId("workspace-picker-dialog")).toBeNull();
    expect(screen.getAllByText("/home/me/repo")).toHaveLength(2);
  });
});

describe("CreateScheduledTaskDialog prefill (seed-on-open + reset)", () => {
  it("seeds Name + Prompt from initialName/initialPrompt when opened", () => {
    render(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        initialName="Daily morning brief"
        initialPrompt="Summarize overnight activity."
      />,
    );
    expect((screen.getByTestId("task-name-input") as HTMLInputElement).value).toBe(
      "Daily morning brief",
    );
    expect((screen.getByTestId("task-prompt-input") as HTMLTextAreaElement).value).toBe(
      "Summarize overnight activity.",
    );
  });

  it("starts EMPTY when opened with no initial values (manual path)", () => {
    render(<CreateScheduledTaskDialog open onOpenChange={vi.fn()} />);
    expect((screen.getByTestId("task-name-input") as HTMLInputElement).value).toBe("");
    expect((screen.getByTestId("task-prompt-input") as HTMLTextAreaElement).value).toBe("");
  });

  it("does not clobber user edits while the dialog stays open", () => {
    const { rerender } = render(
      <CreateScheduledTaskDialog open onOpenChange={vi.fn()} initialName="Seed" />,
    );
    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "Edited" } });
    // A re-render with the SAME open+props must not re-seed over the edit.
    rerender(<CreateScheduledTaskDialog open onOpenChange={vi.fn()} initialName="Seed" />);
    expect((screen.getByTestId("task-name-input") as HTMLInputElement).value).toBe("Edited");
  });

  it("reseeds on a fresh open, and a no-prefill reopen starts empty (no stale leak)", () => {
    const { rerender } = render(
      <CreateScheduledTaskDialog open={false} onOpenChange={vi.fn()} initialName="First" />,
    );
    // Open with "First".
    rerender(<CreateScheduledTaskDialog open onOpenChange={vi.fn()} initialName="First" />);
    expect((screen.getByTestId("task-name-input") as HTMLInputElement).value).toBe("First");

    // Close (resetForm clears), then reopen with NO prefill → empty.
    rerender(<CreateScheduledTaskDialog open={false} onOpenChange={vi.fn()} />);
    rerender(<CreateScheduledTaskDialog open onOpenChange={vi.fn()} />);
    expect((screen.getByTestId("task-name-input") as HTMLInputElement).value).toBe("");
  });
});

describe("CreateScheduledTaskDialog edit mode", () => {
  it("seeds fields from the scheduled task and uses edit copy", () => {
    render(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        editingTask={scheduledTask({ agentId: "ag_1" })}
      />,
    );
    expect(screen.getByText("Edit automation")).toBeInTheDocument();
    expect(screen.getByText(/Update this recurring agent session/i)).toBeInTheDocument();
    expect(screen.getByTestId("create-scheduled-task-submit")).toHaveTextContent("Save changes");
    expect((screen.getByTestId("task-name-input") as HTMLInputElement).value).toBe("Morning brief");
    expect((screen.getByTestId("task-prompt-input") as HTMLTextAreaElement).value).toBe(
      "Summarize overnight activity",
    );
    // Edit mode offers the same picker create does, seeded with the task's own
    // agent — the harness of an existing automation is changeable.
    expect(screen.getByTestId("agent-picker-stub")).toHaveAttribute("data-effective", "ag_1");
    expect(screen.getByTestId("agent-picker-stub")).toHaveTextContent("Polly");
    expect(screen.getByTestId("schedule-time")).toHaveValue("08:30 AM");
  });

  it("keeps a task bound to an agent the picker hides instead of retargeting it", () => {
    render(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        editingTask={scheduledTask({ agentId: "ag_gone" })}
      />,
    );
    // Without the edit-mode fallback this would silently resolve to the first
    // listed agent and switch the harness on the next save.
    expect(screen.getByTestId("agent-picker-stub")).toHaveAttribute("data-effective", "ag_gone");
  });

  it("sends agentId when the harness is switched, clearing the old agent's settings", async () => {
    render(<CreateScheduledTaskDialog open onOpenChange={vi.fn()} editingTask={scheduledTask()} />);
    fireEvent.click(screen.getByTestId("pick-harness-claude"));
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));

    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    const { input } = updateMutateAsync.mock.calls[0][0];
    expect(input.agentId).toBe("ag_claude_native");
    // The task had no overrides (all at Default) and the user changed none, so
    // the PATCH omits them; the server clears stale settings on the rebind.
    expect(input).not.toHaveProperty("modelOverride");
    expect(input).not.toHaveProperty("reasoningEffort");
    expect(input).not.toHaveProperty("permissionMode");
  });

  it("round-trips non-quarter-hour edit times through the update payload", async () => {
    render(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        editingTask={scheduledTask({ rrule: "FREQ=DAILY;BYHOUR=17;BYMINUTE=7" })}
      />,
    );
    expect(screen.getByTestId("schedule-time")).toHaveValue("05:07 PM");
    fireEvent.click(screen.getByTestId("schedule-time-picker-trigger"));
    expect(screen.getByTestId("schedule-hour-05")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("schedule-minute-07")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("schedule-period-PM")).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    expect(updateMutateAsync.mock.calls[0][0].input.rrule).toBe("FREQ=DAILY;BYHOUR=17;BYMINUTE=7");
  });

  it("submits supported edits through the update mutation without changing agent defaults", async () => {
    render(<CreateScheduledTaskDialog open onOpenChange={vi.fn()} editingTask={scheduledTask()} />);
    fireEvent.change(screen.getByTestId("task-name-input"), {
      target: { value: "Updated brief" },
    });
    fireEvent.change(screen.getByTestId("task-prompt-input"), {
      target: { value: "Updated prompt" },
    });
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));

    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    expect(mutateAsync).not.toHaveBeenCalled();
    // Untouched override controls are omitted, not resent as nulls: the
    // stored values (here: none) stay whatever the task carries.
    expect(updateMutateAsync.mock.calls[0][0]).toEqual({
      id: "st_1",
      input: {
        name: "Updated brief",
        prompt: "Updated prompt",
        rrule: "FREQ=DAILY;BYHOUR=8;BYMINUTE=30",
        timezone: "America/Los_Angeles",
      },
    });
  });

  it("keeps the task's settings when the pick lands back on its own agent", async () => {
    // Switching away and back is not a rebind, so the stored model/effort/
    // permission must survive it — the clear is only justified by a real switch.
    render(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        editingTask={scheduledTask({
          agentId: "ag_claude_native",
          modelOverride: "opus",
          reasoningEffort: "high",
          permissionMode: "acceptEdits",
        })}
      />,
    );
    fireEvent.click(screen.getByTestId("pick-agent-polly"));
    fireEvent.click(screen.getByTestId("pick-harness-claude"));
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));

    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    const { input } = updateMutateAsync.mock.calls[0][0];
    expect(input).not.toHaveProperty("agentId");
    // Unchanged controls are omitted, so the stored settings survive the PATCH.
    expect(input).not.toHaveProperty("modelOverride");
    expect(input).not.toHaveProperty("reasoningEffort");
    expect(input).not.toHaveProperty("permissionMode");
  });

  it("keeps the task's settings when the current agent is re-picked", async () => {
    render(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        editingTask={scheduledTask({
          agentId: "ag_claude_native",
          permissionMode: "bypassPermissions",
        })}
      />,
    );
    fireEvent.click(screen.getByTestId("pick-harness-claude"));
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));

    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    const { input } = updateMutateAsync.mock.calls[0][0];
    expect(input).not.toHaveProperty("agentId");
    expect(input).not.toHaveProperty("permissionMode");
  });

  it("blocks update when the existing RRULE cannot be represented by the form", () => {
    render(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        editingTask={scheduledTask({ rrule: "FREQ=DAILY;INTERVAL=2;BYHOUR=9;BYMINUTE=0" })}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("This schedule can't be edited");
    expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
  });
});

/** Open the Host <Select> the Radix-in-jsdom way (see ForkSessionDialog.test). */
function openHostSelect() {
  const trigger = screen.getByTestId("task-host-trigger");
  fireEvent.pointerDown(trigger, new MouseEvent("pointerdown", { bubbles: true, button: 0 }));
  fireEvent.click(trigger);
}

describe("CreateScheduledTaskDialog sandbox mode", () => {
  it("offers no sandbox host option when the server does not advertise managed sandboxes", () => {
    renderDialog();
    openHostSelect();
    expect(screen.queryByTestId("task-host-sandbox-option")).not.toBeInTheDocument();
  });

  it("offers a sandbox option in the host picker when enabled; choosing it enters sandbox mode", () => {
    renderWithSandboxes();
    openHostSelect();
    fireEvent.click(screen.getByTestId("task-host-sandbox-option"));
    // Sandbox mode swaps the helper text to the fresh-sandbox copy (no separate
    // checkbox), and the connected-host workspace picker never appears.
    expect(
      screen.getByText(
        "Provisions a fresh sandbox for each run. Shutdown follows the server’s sandbox configuration.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText("pick-workspace")).not.toBeInTheDocument();
  });

  it("submits execution_target=managed_sandbox with no host/workspace when the sandbox option is chosen", async () => {
    renderWithSandboxes();
    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "Nightly" } });
    fireEvent.change(screen.getByTestId("task-prompt-input"), { target: { value: "Do it" } });
    openHostSelect();
    fireEvent.click(screen.getByTestId("task-host-sandbox-option"));

    const submit = screen.getByTestId("create-scheduled-task-submit");
    await waitFor(() => expect(submit).toBeEnabled());
    fireEvent.click(submit);

    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    const arg = mutateAsync.mock.calls[0][0];
    expect(arg).toMatchObject({ executionTarget: "managed_sandbox" });
    expect(arg).not.toHaveProperty("hostId");
    expect(arg).not.toHaveProperty("workspace");
  });
});

describe("CreateScheduledTaskDialog saved Agents", () => {
  it("offers saved library Agents and creates against the ca_ id", async () => {
    mockCustomAgents(CUSTOM_AGENTS);
    renderDialog();
    const picker = screen.getByTestId("agent-picker-stub");
    expect(picker.getAttribute("data-agent-entries")?.split(",")).toContain("Release crew");

    fireEvent.click(screen.getByTestId("pick-ca_release_crew"));
    expect(picker).toHaveTextContent("Release crew");
    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "Nightly" } });
    fireEvent.change(screen.getByTestId("task-prompt-input"), { target: { value: "Do it" } });
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));

    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    const arg = mutateAsync.mock.calls[0][0];
    expect(arg.agentId).toBe("ca_release_crew");
    expect(arg).not.toHaveProperty("modelOverride");
    expect(arg).not.toHaveProperty("reasoningEffort");
    expect(arg).not.toHaveProperty("permissionMode");
  });

  it("keeps a template-backed session pick when the catalog resolves after the pick", async () => {
    vi.mocked(agentsHook.useAvailableAgents).mockReturnValue({
      data: [
        ...AGENTS,
        {
          id: "ag_session_release_crew",
          name: "release-crew",
          display_name: "Release crew",
          description: null,
          harness: "claude-sdk",
          skills: [],
          templateId: "ca_release_crew",
        },
      ],
    } as unknown as ReturnType<typeof agentsHook.useAvailableAgents>);
    // Catalog still loading: the session-derived row is still offered and picked.
    vi.mocked(customAgentsApi.useCustomAgents).mockReturnValue({
      data: undefined,
    } as unknown as ReturnType<typeof customAgentsApi.useCustomAgents>);

    const { rerender } = renderDialog();
    fireEvent.click(screen.getByTestId("pick-ag_session_release_crew"));
    expect(screen.getByTestId("agent-picker-stub")).toHaveAttribute(
      "data-effective",
      "ag_session_release_crew",
    );

    // The library row arrives and supersedes the session row — the pick must
    // survive instead of falling back to the first agent.
    mockCustomAgents(CUSTOM_AGENTS);
    rerender(<CreateScheduledTaskDialog open onOpenChange={vi.fn()} />);
    expect(screen.getByTestId("agent-picker-stub")).toHaveAttribute(
      "data-effective",
      "ag_session_release_crew",
    );
    // Exactly ONE picker entry for the Agent: the retained session row stands in
    // for its library twin — offering both would let a pick of the twin silently
    // rebind the task to `ca_release_crew` and clear its overrides.
    expect(screen.getAllByRole("button", { name: "Release crew" })).toHaveLength(1);

    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "Nightly" } });
    fireEvent.change(screen.getByTestId("task-prompt-input"), { target: { value: "Do it" } });
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));

    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    expect(mutateAsync.mock.calls[0][0].agentId).toBe("ag_session_release_crew");
  });

  it("keeps exactly one picker entry for an edited task's template-backed session agent", () => {
    vi.mocked(agentsHook.useAvailableAgents).mockReturnValue({
      data: [
        ...AGENTS,
        {
          id: "ag_session_release_crew",
          name: "release-crew",
          display_name: "Release crew",
          description: null,
          harness: "claude-sdk",
          skills: [],
          templateId: "ca_release_crew",
        },
      ],
    } as unknown as ReturnType<typeof agentsHook.useAvailableAgents>);
    mockCustomAgents(CUSTOM_AGENTS);
    render(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        editingTask={scheduledTask({ agentId: "ag_session_release_crew" })}
      />,
    );

    const picker = screen.getByTestId("agent-picker-stub");
    // The task's own agent is retained for its prefill...
    expect(picker).toHaveAttribute("data-effective", "ag_session_release_crew");
    // ...and its library twin is suppressed instead of sitting beside it as a
    // second, identically named entry that would silently rebind the task.
    expect(screen.getAllByRole("button", { name: "Release crew" })).toHaveLength(1);
  });

  it("lists the members lead-first and keeps the model/effort controls hidden", () => {
    mockCustomAgents(CUSTOM_AGENTS);
    renderDialog();
    fireEvent.click(screen.getByTestId("pick-ca_release_crew"));

    expect(screen.getByTestId("task-agent-members")).toHaveTextContent(
      "architect (Lead), executor, reviewer",
    );
    // The members' own model / effort never render in the scheduled dialog.
    expect(screen.getByTestId("task-agent-members")).not.toHaveTextContent(/opus|high/i);
    expect(screen.queryByTestId("task-model-effort-field")).not.toBeInTheDocument();
    expect(screen.queryByTestId("task-permission-control")).not.toBeInTheDocument();
    expect(
      screen.getByText("Uses this agent's default model, effort, and permission settings"),
    ).toBeInTheDocument();
  });

  it("shows no member line for an Agent whose roster could not be loaded", () => {
    mockCustomAgents([{ ...CUSTOM_AGENTS[0], members: null }]);
    renderDialog();
    fireEvent.click(screen.getByTestId("pick-ca_release_crew"));
    expect(screen.queryByTestId("task-agent-members")).not.toBeInTheDocument();
  });

  it("renders an edited task's saved Agent by name", () => {
    mockCustomAgents(CUSTOM_AGENTS);
    render(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        editingTask={scheduledTask({ agentId: "ca_release_crew" })}
      />,
    );
    const picker = screen.getByTestId("agent-picker-stub");
    expect(picker).toHaveAttribute("data-effective", "ca_release_crew");
    expect(picker).toHaveTextContent("Release crew");
    expect(screen.getByTestId("task-agent-members")).toHaveTextContent("architect (Lead)");
  });

  it("disables the sandbox target while a saved Agent is picked, with the reason", () => {
    mockCustomAgents(CUSTOM_AGENTS);
    renderWithSandboxes();
    fireEvent.click(screen.getByTestId("pick-ca_release_crew"));
    openHostSelect();

    expect(screen.getByTestId("task-host-sandbox-option")).toHaveAttribute("data-disabled");
    expect(screen.getByTestId("task-host-sandbox-note")).toHaveTextContent(
      "Custom agents require a connected computer",
    );
  });

  it("blocks create when a saved Agent lands on the sandbox target until a host is chosen", async () => {
    mockCustomAgents(CUSTOM_AGENTS);
    renderWithSandboxes();
    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "Nightly" } });
    fireEvent.change(screen.getByTestId("task-prompt-input"), { target: { value: "Do it" } });
    openHostSelect();
    fireEvent.click(screen.getByTestId("task-host-sandbox-option"));

    const submit = screen.getByTestId("create-scheduled-task-submit");
    await waitFor(() => expect(submit).toBeEnabled());

    // Picking a saved Agent does not silently flip the target off; the reason
    // shows and create stays blocked until a connected host is chosen.
    fireEvent.click(screen.getByTestId("pick-ca_release_crew"));
    expect(screen.getByTestId("task-host-sandbox-note")).toHaveTextContent(
      "Custom agents require a connected computer",
    );
    expect(submit).toBeDisabled();

    openHostSelect();
    fireEvent.click(screen.getByRole("option", { name: "laptop" }));
    await waitFor(() => expect(submit).toBeEnabled());
  });
});

describe("CreateScheduledTaskDialog submit", () => {
  it("submits required fields with the built RRULE and omits host/workspace when unset", async () => {
    renderDialog();
    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "Nightly" } });
    fireEvent.change(screen.getByTestId("task-prompt-input"), { target: { value: "Do it" } });

    const submit = screen.getByTestId("create-scheduled-task-submit");
    await waitFor(() => expect(submit).toBeEnabled());
    fireEvent.click(submit);

    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    const arg = mutateAsync.mock.calls[0][0];
    expect(arg).toMatchObject({
      name: "Nightly",
      prompt: "Do it",
      // Default agent = first after sortAgentsForDisplay (harness rows rank
      // first, so the Claude Code harness is the default) — matches NewChatDialog.
      agentId: "ag_claude_native",
      // Default schedule model is daily at 09:00.
      rrule: "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
    });
    expect(arg).not.toHaveProperty("hostId");
    expect(arg).not.toHaveProperty("workspace");
    // Timezone has no visible control but is still inferred + sent: a non-empty
    // IANA-ish string (whatever the test env's local zone resolves to).
    expect(typeof arg.timezone).toBe("string");
    expect(arg.timezone.length).toBeGreaterThan(0);
  });

  // With the model/effort controls left at their Default (unselected) state, the
  // create body carries agent_id and NO model_override / reasoning_effort,
  // whether the pick is a bare harness (claude-native) or a plain agent (polly).
  it("maps a bare-harness pick to its agent_id, never sends model/effort", async () => {
    renderDialog();
    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "N" } });
    fireEvent.change(screen.getByTestId("task-prompt-input"), { target: { value: "P" } });
    fireEvent.click(screen.getByTestId("pick-harness-claude"));
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    const arg = mutateAsync.mock.calls[0][0];
    expect(arg.agentId).toBe("ag_claude_native");
    expect(arg).not.toHaveProperty("modelOverride");
    expect(arg).not.toHaveProperty("reasoningEffort");
  });

  it("maps an agent pick to its agent_id, never sends model/effort", async () => {
    renderDialog();
    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "N" } });
    fireEvent.change(screen.getByTestId("task-prompt-input"), { target: { value: "P" } });
    fireEvent.click(screen.getByTestId("pick-agent-polly"));
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    const arg = mutateAsync.mock.calls[0][0];
    expect(arg.agentId).toBe("ag_1");
    expect(arg).not.toHaveProperty("modelOverride");
    expect(arg).not.toHaveProperty("reasoningEffort");
  });

  it("does not render a visible timezone picker (inferred silently)", () => {
    renderDialog();
    expect(screen.queryByTestId("task-timezone-trigger")).toBeNull();
  });

  it("renders the Time field as an input with a compact picker trigger", () => {
    renderDialog();
    const timeField = screen.getByTestId("schedule-time");
    expect(timeField.tagName).toBe("INPUT");
    expect(timeField).toHaveAttribute("placeholder", "5:00 PM");
    expect(timeField).toHaveClass("text-ui");
    expect(screen.getByTestId("schedule-time-picker-trigger")).toBeInTheDocument();
  });

  it("lays out Frequency and Time in one compact row", () => {
    renderDialog();
    const row = screen.getByTestId("schedule-frequency-time-row");
    expect(row).toContainElement(screen.getByText("Frequency"));
    expect(row).toContainElement(screen.getByText("Time"));
    expect(row).toContainElement(screen.getByTestId("schedule-preset-trigger"));
    expect(row).toContainElement(screen.getByTestId("schedule-time"));
    expect(row).toHaveClass("sm:grid-cols-2", "sm:gap-6");
    expect(screen.getByTestId("schedule-frequency-control")).toHaveClass("w-full");
    expect(screen.getByTestId("schedule-time-control")).toHaveClass("w-full");
    expect(screen.getByTestId("schedule-preset-trigger")).toHaveClass("w-full");
  });

  it("lays out Host full-width like the other top-level fields", () => {
    renderDialog();
    const hostField = screen.getByTestId("task-host-field");
    const hostTrigger = screen.getByTestId("task-host-trigger");
    expect(hostField).not.toHaveClass("sm:w-64");
    expect(hostField).toContainElement(hostTrigger);
    expect(hostTrigger).toHaveClass("w-full");
  });

  it("keeps the footer visible by letting only the dialog body scroll", () => {
    renderDialog();
    expect(screen.getByTestId("create-scheduled-task-dialog")).toHaveClass("flex", "flex-col");
    expect(screen.getByTestId("scheduled-task-dialog-body")).toHaveClass(
      "min-h-0",
      "flex-1",
      "overflow-y-auto",
    );
    expect(document.querySelector('[data-slot="dialog-footer"]')).toHaveClass("shrink-0");
  });

  it("chooses a non-quarter-hour time from the compact picker", async () => {
    renderDialog();
    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "T" } });
    fireEvent.change(screen.getByTestId("task-prompt-input"), { target: { value: "P" } });
    fireEvent.click(screen.getByTestId("schedule-time-picker-trigger"));
    fireEvent.click(await screen.findByTestId("schedule-hour-05"));
    fireEvent.click(screen.getByTestId("schedule-minute-07"));
    fireEvent.click(screen.getByTestId("schedule-period-PM"));
    expect(screen.getByTestId("schedule-time")).toHaveValue("05:07 PM");

    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    expect(mutateAsync.mock.calls[0][0].rrule).toBe("FREQ=DAILY;BYHOUR=17;BYMINUTE=7");
  });

  it("shows all minute choices in the compact picker", async () => {
    renderDialog();
    fireEvent.click(screen.getByTestId("schedule-time-picker-trigger"));
    const minuteColumn = await screen.findByTestId("schedule-minute-column");
    expect(minuteColumn.querySelectorAll('[data-testid^="schedule-minute-"]')).toHaveLength(60);
    expect(screen.getByTestId("schedule-minute-00")).toBeInTheDocument();
    expect(screen.getByTestId("schedule-minute-01")).toBeInTheDocument();
    expect(screen.getByTestId("schedule-minute-15")).toBeInTheDocument();
    expect(screen.getByTestId("schedule-minute-30")).toBeInTheDocument();
    expect(screen.getByTestId("schedule-minute-45")).toBeInTheDocument();
    expect(screen.getByTestId("schedule-minute-59")).toBeInTheDocument();
  });

  it("makes overflowing picker columns scrollable without closing the picker", async () => {
    renderDialog();
    fireEvent.click(screen.getByTestId("schedule-time-picker-trigger"));
    const hourColumn = await screen.findByTestId("schedule-hour-column");
    expect(hourColumn).toHaveClass("overflow-y-auto", "overscroll-contain");
    expect(screen.getByTestId("schedule-minute-column")).toHaveClass("overflow-y-auto");

    fireEvent.wheel(hourColumn, { deltaY: 120 });
    expect(screen.getByTestId("schedule-time-picker")).toBeInTheDocument();
  });

  it("renders all minute values and selects a non-quarter-hour minute", async () => {
    renderDialog();
    fireEvent.click(screen.getByTestId("schedule-time-picker-trigger"));
    const minuteColumn = screen.getByTestId("schedule-minute-column");
    expect(minuteColumn.querySelectorAll('[data-testid^="schedule-minute-"]')).toHaveLength(60);
    expect(screen.getByTestId("schedule-minute-00")).toBeInTheDocument();
    expect(screen.getByTestId("schedule-minute-37")).toBeInTheDocument();
    expect(screen.getByTestId("schedule-minute-59")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("schedule-minute-37"));
    expect(screen.getByTestId("schedule-time")).toHaveValue("09:37 AM");

    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "T" } });
    fireEvent.change(screen.getByTestId("task-prompt-input"), { target: { value: "P" } });
    const submit = screen.getByTestId("create-scheduled-task-submit");
    await waitFor(() => expect(submit).toBeEnabled());
    fireEvent.click(submit);
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    expect(mutateAsync.mock.calls[0][0].rrule).toBe("FREQ=DAILY;BYHOUR=9;BYMINUTE=37");
  });

  it("round-trips the current non-quarter-hour minute as the selected picker value", () => {
    render(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        editingTask={scheduledTask({ rrule: "FREQ=DAILY;BYHOUR=9;BYMINUTE=7" })}
      />,
    );
    fireEvent.click(screen.getByTestId("schedule-time-picker-trigger"));
    expect(screen.getByTestId("schedule-minute-00")).toBeInTheDocument();
    expect(screen.getByTestId("schedule-minute-07")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("schedule-minute-15")).toBeInTheDocument();
    expect(screen.getByTestId("schedule-minute-30")).toBeInTheDocument();
    expect(screen.getByTestId("schedule-minute-45")).toBeInTheDocument();
    expect(screen.getByTestId("schedule-minute-08")).toBeInTheDocument();
  });

  it("typing a non-quarter-hour time flows into the submitted RRULE", async () => {
    renderDialog();
    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "T" } });
    fireEvent.change(screen.getByTestId("task-prompt-input"), { target: { value: "P" } });
    fireEvent.change(screen.getByTestId("schedule-time"), { target: { value: "5:07 PM" } });

    const submit = screen.getByTestId("create-scheduled-task-submit");
    await waitFor(() => expect(submit).toBeEnabled());
    fireEvent.click(submit);
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    // Default preset is Daily -> 17:07.
    expect(mutateAsync.mock.calls[0][0].rrule).toBe("FREQ=DAILY;BYHOUR=17;BYMINUTE=7");
  });

  it("does not canonicalize partial time input while the field is focused", () => {
    renderDialog();
    const timeField = screen.getByTestId("schedule-time");

    timeField.focus();
    fireEvent.change(timeField, { target: { value: "" } });
    fireEvent.change(timeField, { target: { value: "1" } });
    expect(timeField).toHaveValue("1");
    fireEvent.change(timeField, { target: { value: "1:" } });
    expect(timeField).toHaveValue("1:");
    fireEvent.change(timeField, { target: { value: "1:15" } });
    expect(timeField).toHaveValue("1:15");
    expect(screen.queryByTestId("schedule-error")).toBeNull();

    fireEvent.blur(timeField);
    expect(timeField).toHaveValue("01:15 AM");
  });

  it("canonicalizes typed time on blur", () => {
    renderDialog();
    const timeField = screen.getByTestId("schedule-time");
    fireEvent.change(timeField, { target: { value: "17:07" } });
    fireEvent.blur(timeField);
    expect(timeField).toHaveValue("05:07 PM");
  });

  it("uses text-ui for dialog text fields that wrap shared primitives", () => {
    renderDialog();
    expect(screen.getByTestId("task-name-input")).toHaveClass("text-ui");
    expect(screen.getByTestId("task-prompt-input")).toHaveClass("text-ui");
  });

  it("blocks submit while the typed time is invalid", async () => {
    renderDialog();
    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "T" } });
    fireEvent.change(screen.getByTestId("task-prompt-input"), { target: { value: "P" } });
    fireEvent.change(screen.getByTestId("schedule-time"), { target: { value: "25:99" } });
    expect(screen.getByTestId("schedule-error")).toHaveTextContent("Enter a valid time");
    expect(screen.getByTestId("create-scheduled-task-submit")).toBeDisabled();
  });

  it("Hourly preset shows a minute-only text input", async () => {
    renderDialog();
    fireEvent.keyDown(screen.getByTestId("schedule-preset-trigger"), { key: "Enter" });
    fireEvent.click(await screen.findByRole("option", { name: "Hourly" }));
    const minuteField = screen.getByTestId("schedule-minute");
    expect(minuteField.tagName).toBe("INPUT");
    expect(minuteField).toHaveClass("text-ui");
    expect(minuteField).toHaveAttribute("placeholder", "0");
    expect(screen.queryByTestId("schedule-time-picker-trigger")).toBeNull();
    fireEvent.change(minuteField, { target: { value: "7" } });
    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "T" } });
    fireEvent.change(screen.getByTestId("task-prompt-input"), { target: { value: "P" } });
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    expect(mutateAsync.mock.calls[0][0].rrule).toBe("FREQ=HOURLY;BYMINUTE=7");
  });

  it("Hourly preset strips non-digits, caps to two digits, and clamps above 59", async () => {
    renderDialog();
    fireEvent.keyDown(screen.getByTestId("schedule-preset-trigger"), { key: "Enter" });
    fireEvent.click(await screen.findByRole("option", { name: "Hourly" }));
    const minuteField = screen.getByTestId("schedule-minute");

    fireEvent.change(minuteField, { target: { value: "a:-" } });
    expect(minuteField).toHaveValue("");
    fireEvent.change(minuteField, { target: { value: "3a" } });
    expect(minuteField).toHaveValue("3");
    fireEvent.blur(minuteField);
    expect(minuteField).toHaveValue("3");
    fireEvent.change(minuteField, { target: { value: "75" } });
    expect(minuteField).toHaveValue("59");
    fireEvent.change(minuteField, { target: { value: "123" } });
    expect(minuteField).toHaveValue("12");
  });

  it("offers exactly the four frequency presets with no Custom entry point", async () => {
    renderDialog();
    // Open the frequency Select (keyboard is the reliable jsdom path).
    fireEvent.keyDown(screen.getByTestId("schedule-preset-trigger"), { key: "Enter" });
    const options = (await screen.findAllByRole("option")).map((o) => o.textContent);
    expect(options).toEqual(["Hourly", "Daily", "Weekdays", "Weekly"]);
    expect(options).not.toContain("Custom");
    // The Custom-only sub-controls are not reachable from this form.
    expect(screen.queryByTestId("custom-freq-trigger")).toBeNull();
    expect(screen.queryByTestId("custom-interval")).toBeNull();
    expect(screen.queryByTestId("schedule-month-trigger")).toBeNull();
  });

  it("does not render the 'Reads as' schedule preview", () => {
    renderDialog();
    expect(screen.queryByTestId("schedule-preview")).toBeNull();
    expect(screen.queryByText(/Reads as:/i)).toBeNull();
  });
});

describe("CreateScheduledTaskDialog project field", () => {
  function openProjectSelect() {
    const trigger = screen.getByTestId("task-project-trigger");
    fireEvent.pointerDown(trigger, new MouseEvent("pointerdown", { bubbles: true, button: 0 }));
    fireEvent.click(trigger);
  }

  it("pre-fills the project from the surrounding context and sends project_id", async () => {
    vi.mocked(useProjects).mockReturnValue({
      data: [{ id: "proj_alpha", name: "Alpha" }],
    } as ReturnType<typeof useProjects>);
    render(<CreateScheduledTaskDialog open onOpenChange={vi.fn()} currentProjectName="Alpha" />);
    expect(screen.getByTestId("task-project-trigger")).toHaveTextContent("Alpha");

    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "Nightly" } });
    fireEvent.change(screen.getByTestId("task-prompt-input"), { target: { value: "Do it" } });
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    expect(mutateAsync.mock.calls[0][0]).toMatchObject({ projectId: "proj_alpha" });
  });

  it("omits project_id for 'No project'", async () => {
    vi.mocked(useProjects).mockReturnValue({
      data: [{ id: "proj_alpha", name: "Alpha" }],
    } as ReturnType<typeof useProjects>);
    renderDialog();
    expect(screen.getByTestId("task-project-trigger")).toHaveTextContent("No project");

    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "Nightly" } });
    fireEvent.change(screen.getByTestId("task-prompt-input"), { target: { value: "Do it" } });
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    expect(mutateAsync.mock.calls[0][0]).not.toHaveProperty("projectId");
  });

  it("pre-fills an edited task's project and sends a cleared project as null", async () => {
    vi.mocked(useProjects).mockReturnValue({
      data: [{ id: "proj_alpha", name: "Alpha" }],
    } as ReturnType<typeof useProjects>);
    render(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        editingTask={scheduledTask({ projectId: "proj_alpha" })}
      />,
    );
    expect(screen.getByTestId("task-project-trigger")).toHaveTextContent("Alpha");

    openProjectSelect();
    fireEvent.click(await screen.findByRole("option", { name: "No project" }));
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    expect(updateMutateAsync.mock.calls[0][0].input).toMatchObject({ projectId: null });
  });
});

describe("CreateScheduledTaskDialog model + effort controls", () => {
  it("renders model, effort, and permission for Claude native and SDK agents", async () => {
    renderDialog();
    expect(screen.getByTestId("task-model-effort-field")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("pick-agent-polly"));
    expect(screen.getByTestId("task-model-trigger")).toBeInTheDocument();
    expect(screen.getByTestId("task-effort-trigger")).toBeInTheDocument();
    fireEvent.keyDown(screen.getByTestId("task-model-trigger"), { key: "Enter" });
    fireEvent.click(await screen.findByRole("option", { name: "Opus" }));
    fireEvent.keyDown(screen.getByTestId("task-permission-trigger"), { key: "Enter" });
    expect(await screen.findByRole("option", { name: "Bypass permissions" })).toBeInTheDocument();
  });

  it("shows Codex SDK presets and only default model and effort without a host", async () => {
    renderDialog();
    fireEvent.click(screen.getByTestId("pick-agent-codex"));
    expect(screen.getByTestId("task-model-effort-field")).toBeInTheDocument();
    fireEvent.keyDown(screen.getByTestId("task-model-trigger"), { key: "Enter" });
    expect(await screen.findAllByRole("option")).toHaveLength(1);
    fireEvent.click(await screen.findByRole("option", { name: "Default" }));
    fireEvent.keyDown(screen.getByTestId("task-effort-trigger"), { key: "Enter" });
    expect(await screen.findAllByRole("option")).toHaveLength(1);
    fireEvent.click(await screen.findByRole("option", { name: "Default" }));
    fireEvent.keyDown(screen.getByTestId("task-permission-trigger"), { key: "Enter" });
    expect(await screen.findByRole("option", { name: "Agent default" })).toBeInTheDocument();
    expect(await screen.findByRole("option", { name: "Default" })).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("option", { name: "Read only" }));
    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "N" } });
    fireEvent.change(screen.getByTestId("task-prompt-input"), { target: { value: "P" } });
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    expect(mutateAsync.mock.calls[0][0].permissionMode).toBe("read-only");
  });

  it("shows saved Codex model and effort when no host catalog is available", () => {
    render(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        editingTask={scheduledTask({
          agentId: "ag_codex",
          modelOverride: "gpt-5.5",
          reasoningEffort: "xhigh",
        })}
      />,
    );
    expect(screen.getByTestId("task-model-trigger")).toHaveTextContent("gpt-5.5");
    expect(screen.getByTestId("task-effort-trigger")).toHaveTextContent("xHigh");
  });

  it("uses pinned host Codex SDK model rows and their effort levels", async () => {
    vi.mocked(hostsHook.useHostModelOptions).mockReturnValue({
      data: [
        {
          id: "model-one",
          displayName: "Model One",
          supportedReasoningEfforts: [{ reasoningEffort: "high" }],
        },
        {
          id: "model-two",
          displayName: "Model Two",
          supportedReasoningEfforts: [{ reasoningEffort: "low" }],
        },
      ],
    } as ReturnType<typeof hostsHook.useHostModelOptions>);
    render(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        editingTask={scheduledTask({
          agentId: "ag_codex",
          hostId: "host_1",
          modelOverride: "model-one",
          reasoningEffort: "high",
        })}
      />,
    );
    expect(hostsHook.useHostModelOptions).toHaveBeenCalledWith("host_1", "codex", true);
    expect(screen.getByTestId("task-model-trigger")).toHaveTextContent("Model One");
    fireEvent.keyDown(screen.getByTestId("task-effort-trigger"), { key: "Enter" });
    expect(await screen.findByRole("option", { name: "High" })).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("option", { name: "High" }));
    fireEvent.keyDown(screen.getByTestId("task-model-trigger"), { key: "Enter" });
    fireEvent.click(await screen.findByRole("option", { name: "Model Two" }));
    expect(screen.getByTestId("task-effort-trigger")).toHaveTextContent("Default");
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    expect(updateMutateAsync.mock.calls[0][0].input).toMatchObject({
      modelOverride: "model-two",
      reasoningEffort: null,
    });
  });

  it("sends the selected model + effort on create for a capable agent", async () => {
    renderDialog();
    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "N" } });
    fireEvent.change(screen.getByTestId("task-prompt-input"), { target: { value: "P" } });

    // Pick a model (keyboard is the reliable jsdom path for Radix Select).
    fireEvent.keyDown(screen.getByTestId("task-model-trigger"), { key: "Enter" });
    fireEvent.click(await screen.findByRole("option", { name: "Opus" }));
    // Pick an effort.
    fireEvent.keyDown(screen.getByTestId("task-effort-trigger"), { key: "Enter" });
    fireEvent.click(await screen.findByRole("option", { name: "High" }));
    // Pick a permission mode.
    fireEvent.keyDown(screen.getByTestId("task-permission-trigger"), { key: "Enter" });
    fireEvent.click(await screen.findByRole("option", { name: "Accept edits" }));

    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    const arg = mutateAsync.mock.calls[0][0];
    expect(arg.modelOverride).toBe("opus");
    expect(arg.reasoningEffort).toBe("high");
    expect(arg.permissionMode).toBe("acceptEdits");
  });

  it("sends Claude SDK model, effort, and permission on create", async () => {
    renderDialog();
    fireEvent.click(screen.getByTestId("pick-agent-polly"));
    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "N" } });
    fireEvent.change(screen.getByTestId("task-prompt-input"), { target: { value: "P" } });
    fireEvent.keyDown(screen.getByTestId("task-model-trigger"), { key: "Enter" });
    fireEvent.click(await screen.findByRole("option", { name: "Opus" }));
    fireEvent.keyDown(screen.getByTestId("task-effort-trigger"), { key: "Enter" });
    fireEvent.click(await screen.findByRole("option", { name: "High" }));
    fireEvent.keyDown(screen.getByTestId("task-permission-trigger"), { key: "Enter" });
    fireEvent.click(await screen.findByRole("option", { name: "Bypass permissions" }));
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    expect(mutateAsync.mock.calls[0][0]).toMatchObject({
      modelOverride: "opus",
      reasoningEffort: "high",
      permissionMode: "bypassPermissions",
    });
  });

  it("sends changed Claude SDK model, effort, and permission on update", async () => {
    render(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        editingTask={scheduledTask({
          agentId: "ag_1",
          modelOverride: "opus",
          reasoningEffort: "high",
          permissionMode: "bypassPermissions",
        })}
      />,
    );
    expect(screen.getByTestId("task-model-trigger")).toHaveTextContent("Opus");
    // Pick a different value in each control: the PATCH states exactly those.
    fireEvent.keyDown(screen.getByTestId("task-model-trigger"), { key: "Enter" });
    fireEvent.click(await screen.findByRole("option", { name: "Sonnet" }));
    fireEvent.keyDown(screen.getByTestId("task-effort-trigger"), { key: "Enter" });
    fireEvent.click(await screen.findByRole("option", { name: "Medium" }));
    fireEvent.keyDown(screen.getByTestId("task-permission-trigger"), { key: "Enter" });
    fireEvent.click(await screen.findByRole("option", { name: "Plan" }));
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    expect(updateMutateAsync.mock.calls[0][0].input).toMatchObject({
      modelOverride: "sonnet",
      reasoningEffort: "medium",
      permissionMode: "plan",
    });
  });

  it("omits model + effort + permission on create when left at Default", async () => {
    renderDialog();
    fireEvent.change(screen.getByTestId("task-name-input"), { target: { value: "N" } });
    fireEvent.change(screen.getByTestId("task-prompt-input"), { target: { value: "P" } });
    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    const arg = mutateAsync.mock.calls[0][0];
    expect(arg).not.toHaveProperty("modelOverride");
    expect(arg).not.toHaveProperty("reasoningEffort");
    expect(arg).not.toHaveProperty("permissionMode");
  });

  it("prefills model + effort + permission in edit mode from the loaded task", async () => {
    render(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        editingTask={scheduledTask({
          agentId: "ag_claude_native",
          modelOverride: "sonnet",
          reasoningEffort: "medium",
          permissionMode: "acceptEdits",
        })}
      />,
    );
    // Prefilled selections surface as the trigger's shown value.
    expect(screen.getByTestId("task-model-trigger")).toHaveTextContent("Sonnet");
    expect(screen.getByTestId("task-effort-trigger")).toHaveTextContent("Medium");
    expect(screen.getByTestId("task-permission-trigger")).toHaveTextContent("Accept edits");
  });

  it("sends only the changed overrides on update, nulling a cleared one", async () => {
    render(
      <CreateScheduledTaskDialog
        open
        onOpenChange={vi.fn()}
        editingTask={scheduledTask({
          agentId: "ag_claude_native",
          modelOverride: "opus",
          reasoningEffort: "high",
          permissionMode: "plan",
        })}
      />,
    );
    // Reset the model back to Default (a cleared previously-set override) and
    // change the effort (a new value); leave permission untouched.
    fireEvent.keyDown(screen.getByTestId("task-model-trigger"), { key: "Enter" });
    fireEvent.click(await screen.findByRole("option", { name: "Default" }));
    fireEvent.keyDown(screen.getByTestId("task-effort-trigger"), { key: "Enter" });
    fireEvent.click(await screen.findByRole("option", { name: "Medium" }));

    fireEvent.click(screen.getByTestId("create-scheduled-task-submit"));
    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1));
    const { input } = updateMutateAsync.mock.calls[0][0];
    // Cleared model → null (back to inherit); changed effort → the value;
    // untouched permission → omitted so the stored mode survives.
    expect(input.modelOverride).toBeNull();
    expect(input.reasoningEffort).toBe("medium");
    expect(input).not.toHaveProperty("permissionMode");
  });
});

describe("nested dropdowns do not dismiss the Dialog (isInsidePopper guard)", () => {
  // The guard's decision is pure DOM: is the interaction target inside a Radix
  // popper / Select portal? Unit-test that directly — jsdom can't faithfully
  // reproduce Radix's pointer-capture portal outside-click, so the full
  // "click an option → dialog stays open" path is covered by the live pane
  // verification (see the task report), not here.
  it("treats a click inside a Select portal as inside-popper", () => {
    const content = document.createElement("div");
    content.setAttribute("data-slot", "select-content");
    const option = document.createElement("div");
    option.setAttribute("role", "option");
    content.appendChild(option);
    document.body.appendChild(content);
    expect(isInsidePopper(option)).toBe(true);

    const wrapper = document.createElement("div");
    wrapper.setAttribute("data-radix-popper-content-wrapper", "");
    const inner = document.createElement("span");
    wrapper.appendChild(inner);
    document.body.appendChild(wrapper);
    expect(isInsidePopper(inner)).toBe(true);

    const listbox = document.createElement("div");
    listbox.setAttribute("role", "listbox");
    document.body.appendChild(listbox);
    expect(isInsidePopper(listbox)).toBe(true);
  });

  it("treats a click inside the agent DropdownMenu portal as inside-popper", () => {
    const content = document.createElement("div");
    content.setAttribute("data-slot", "dropdown-menu-content");
    const item = document.createElement("div");
    content.appendChild(item);
    document.body.appendChild(content);
    expect(isInsidePopper(item)).toBe(true);
  });

  it("treats the real backdrop (outside any popper) as NOT inside-popper", () => {
    const backdrop = document.createElement("div");
    document.body.appendChild(backdrop);
    expect(isInsidePopper(backdrop)).toBe(false);
    expect(isInsidePopper(null)).toBe(false);
    expect(isInsidePopper(document.body)).toBe(false);
  });
});

describe("shouldGuardDialogDismiss (backdrop click closes; dropdown-dismiss guarded)", () => {
  function overlayTarget(): Element {
    const overlay = document.createElement("div");
    overlay.setAttribute("data-slot", "dialog-overlay");
    document.body.appendChild(overlay);
    return overlay;
  }
  function popperTarget(): Element {
    const content = document.createElement("div");
    content.setAttribute("data-slot", "select-content");
    const inner = document.createElement("div");
    content.appendChild(inner);
    document.body.appendChild(content);
    return inner;
  }
  function dialogContentTarget(): Element {
    const content = document.createElement("div");
    content.setAttribute("data-slot", "dialog-content");
    document.body.appendChild(content);
    return content;
  }

  it("does NOT guard a genuine backdrop-overlay click → dialog dismisses", () => {
    const target = overlayTarget();
    // Even while a Select is open / just closed / inside grace, a backdrop click
    // must dismiss (the bug was this being swallowed).
    expect(shouldGuardDialogDismiss(target, { selectOpen: true, msSinceSelectClose: 0 })).toBe(
      false,
    );
    expect(shouldGuardDialogDismiss(target, { selectOpen: false, msSinceSelectClose: 10 })).toBe(
      false,
    );
  });

  it("guards a click INSIDE a popper (option pick) → dialog stays open", () => {
    expect(
      shouldGuardDialogDismiss(popperTarget(), { selectOpen: false, msSinceSelectClose: 9999 }),
    ).toBe(true);
  });

  it("guards while a dropdown is open, including clicks inside dialog content", () => {
    expect(
      shouldGuardDialogDismiss(dialogContentTarget(), {
        selectOpen: true,
        msSinceSelectClose: 9999,
      }),
    ).toBe(true);
  });

  it("guards while a Select is open, and within the grace window after it closes", () => {
    const plain = document.createElement("div");
    document.body.appendChild(plain);
    // Select currently open → guarded.
    expect(shouldGuardDialogDismiss(plain, { selectOpen: true, msSinceSelectClose: 9999 })).toBe(
      true,
    );
    // Trailing focus-outside within grace → guarded.
    expect(shouldGuardDialogDismiss(plain, { selectOpen: false, msSinceSelectClose: 50 })).toBe(
      true,
    );
    // Well after grace, not in a popper, no select → NOT guarded (would dismiss).
    expect(shouldGuardDialogDismiss(plain, { selectOpen: false, msSinceSelectClose: 500 })).toBe(
      false,
    );
  });
});
