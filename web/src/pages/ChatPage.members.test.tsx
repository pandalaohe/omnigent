import type * as UseWorkspaceChangedFilesModule from "@/hooks/useWorkspaceChangedFiles";
import type * as UseSessionModule from "@/hooks/useSession";
import type * as UseHostsModule from "@/hooks/useHosts";
import type * as RunnerHealthProviderModule from "@/hooks/RunnerHealthProvider";
import type * as AgentLabelsModule from "@/lib/agentLabels";
import type * as UseChildSessionsModule from "@/hooks/useChildSessions";
import type { ChildSessionInfo } from "@/hooks/useChildSessions";

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useChatStore } from "@/store/chatStore";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { WorkspaceFile } from "@/hooks/useWorkspaceChangedFiles";

// The "@" menu's file source: one file at the workspace root, enough to prove
// file rows still render beside a Members section.
vi.mock("@/hooks/useWorkspaceChangedFiles", async (importOriginal) => {
  const actual = await importOriginal<typeof UseWorkspaceChangedFilesModule>();
  return {
    ...actual,
    useWorkspaceAllFiles: () => ({
      data: {
        available: true,
        data: [
          { path: "src", name: "src", type: "directory", bytes: null, modified_at: null },
        ] satisfies WorkspaceFile[],
      },
      isLoading: false,
    }),
    useWorkspaceDirectory: () => ({ data: undefined, isLoading: false }),
  };
});

vi.mock("@/hooks/useGithub", () => ({
  useGithubInfo: () => ({ data: undefined }),
}));
vi.mock("@/hooks/useComposerGitStatus", () => ({
  useComposerGitStatus: () => ({ branchState: "unknown", prCount: 0 }),
}));
vi.mock("@/hooks/useSkills", () => ({
  useSkills: () => ({ skills: [], skillsStatus: "ready", refetch: vi.fn() }),
}));
vi.mock("@/hooks/useHosts", async (importOriginal) => ({
  ...(await importOriginal<typeof UseHostsModule>()),
  useHosts: () => ({ data: [] }),
}));
vi.mock("@/hooks/RunnerHealthProvider", async (importOriginal) => ({
  ...(await importOriginal<typeof RunnerHealthProviderModule>()),
  useSessionHostOnline: () => undefined,
}));
vi.mock("@/lib/agentLabels", async (importOriginal) => ({
  ...(await importOriginal<typeof AgentLabelsModule>()),
  useBrainHarnessLabels: () => ({
    "claude-sdk": "Claude SDK",
    codex: "Codex",
    "claude-native": "Claude Code",
  }),
}));

const { sessionSnapshot } = vi.hoisted(() => ({
  sessionSnapshot: {
    hostId: null as string | null,
    workspace: null as string | null,
    gitBranch: null as string | null,
    agentTemplateId: null as string | null,
    labels: {} as Record<string, string>,
  },
}));
vi.mock("@/hooks/useSession", async (importOriginal) => ({
  ...(await importOriginal<typeof UseSessionModule>()),
  useSession: () => ({ session: sessionSnapshot, isLoading: false, error: null }),
}));

const { childSessionsMock } = vi.hoisted(() => ({
  childSessionsMock: { children: [] as ChildSessionInfo[] },
}));
vi.mock("@/hooks/useChildSessions", async (importOriginal) => ({
  ...(await importOriginal<typeof UseChildSessionsModule>()),
  useChildSessions: () => ({
    children: childSessionsMock.children,
    isLoading: false,
    error: null,
  }),
}));

import { Composer } from "./ChatPage";

const MEMBER_KEY = "omnigent.member.";

function memberLabel(overrides: Record<string, unknown> = {}): string {
  return JSON.stringify({
    host: "Mac",
    harness: "codex",
    model: "gpt-6-sol",
    effort: "xhigh",
    lead: false,
    ...overrides,
  });
}

/** The 2+ member session every test starts from. */
function multiMemberLabels(): Record<string, string> {
  return {
    [`${MEMBER_KEY}architect`]: memberLabel({
      harness: "claude-sdk",
      model: "opus-1m",
      effort: "high",
      lead: true,
    }),
    [`${MEMBER_KEY}executor`]: memberLabel(),
    [`${MEMBER_KEY}reviewer`]: memberLabel({
      host: "Desktop-HRF",
      model: "gpt-6-astra",
      effort: "medium",
    }),
  };
}

function composerProps(overrides: Partial<Parameters<typeof Composer>[0]> = {}) {
  return {
    status: "idle" as const,
    isWorking: false,
    disabled: false,
    onSend: vi.fn(),
    onStop: vi.fn(),
    agents: undefined,
    agentsLoading: false,
    selectedAgentId: null,
    onSelectAgent: vi.fn(),
    permissionLevel: null,
    readOnlyReason: null,
    replyQuotes: [],
    onRemoveQuote: vi.fn(),
    onClearAllQuotes: vi.fn(),
    effortLevels: ["low", "medium", "high"] as const,
    showEffort: true,
    showModels: false,
    modelPickerKind: null,
    codexModelOptions: [],
    showCodexPlanMode: false,
    ...overrides,
  };
}

function textarea() {
  return screen.getByLabelText("Message the agent") as HTMLTextAreaElement;
}

/** Type `text` into the composer with the caret at the end. */
function type(text: string) {
  fireEvent.change(textarea(), { target: { value: text, selectionStart: text.length } });
}

function renderComposer(overrides: Partial<Parameters<typeof Composer>[0]> = {}) {
  const ui: ReactElement = <Composer {...composerProps(overrides)} />;
  return render(
    <MemoryRouter>
      <TooltipProvider>{ui}</TooltipProvider>
    </MemoryRouter>,
  );
}

function openSessionPicker(testId: string) {
  fireEvent.keyDown(screen.getByTestId(testId), { key: "ArrowDown" });
}

let convSeq = 0;
beforeEach(() => {
  localStorage.clear();
  sessionSnapshot.labels = {};
  sessionSnapshot.agentTemplateId = null;
  childSessionsMock.children = [];
  useChatStore.setState({
    conversationId: `conv_members_${++convSeq}`,
    sessionHarness: "claude-sdk",
    pendingComposerAttachments: [],
  });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("2+ member session composer", () => {
  beforeEach(() => {
    sessionSnapshot.labels = multiMemberLabels();
  });

  it("replaces the model/effort trigger with the members menu trigger", () => {
    renderComposer({ showModels: true, showEffort: true, modelPickerKind: "claude" });
    expect(screen.getByTestId("composer-agent-members-trigger")).toBeInTheDocument();
    expect(screen.queryByTestId("composer-config-gear")).toBeNull();
  });

  it("lists each member, the lock note, Open and Edit agent in the menu", () => {
    sessionSnapshot.labels["omnigent:agent-template-id"] = "ca_release_crew";
    childSessionsMock.children = [
      {
        id: "conv_exec",
        title: "executor:upload",
        task_summary: null,
        tool: "executor",
        session_name: "upload",
        labels: {},
        current_task_status: "completed",
        busy: false,
        last_message_preview: null,
        pending_elicitations_count: 0,
      },
    ];
    renderComposer();
    openSessionPicker("composer-agent-members-trigger");

    expect(screen.getByTestId("agent-member-row-architect")).toHaveTextContent("architect");
    expect(screen.getByTestId("agent-member-row-executor")).toHaveTextContent("executor");
    expect(screen.getByTestId("agent-member-row-reviewer")).toHaveTextContent("reviewer");
    expect(screen.getByTestId("agent-member-lead-architect")).toHaveTextContent("Lead");
    expect(screen.getByTestId("agent-members-lock-note")).toHaveTextContent(
      "Members are fixed for this session; edits apply to new sessions",
    );
    expect(screen.getByTestId("agent-member-open-executor")).toHaveAttribute(
      "href",
      "/c/conv_exec",
    );
    expect(screen.queryByTestId("agent-member-open-architect")).toBeNull();
    expect(screen.getByTestId("composer-agent-edit-agent")).toHaveTextContent("Edit agent");
    // No model / effort sections in the members menu.
    expect(screen.queryByTestId("composer-agent-models")).toBeNull();
    expect(screen.queryByTestId("composer-agent-efforts")).toBeNull();
  });

  it("omits Edit agent without the saved-Agent label", () => {
    renderComposer();
    openSessionPicker("composer-agent-members-trigger");
    expect(screen.queryByTestId("composer-agent-edit-agent")).toBeNull();
  });

  it("shows the routing hint in the placeholder", () => {
    renderComposer();
    expect(textarea().placeholder).toContain("@role hands a part to a member");
  });

  it("offers the Members section to `@` on a non-native harness", () => {
    // claude-sdk is not a native file-mention harness; the Members section must
    // still open because routing names are not on-disk files.
    renderComposer();
    type("@");
    expect(screen.getByText("@architect")).toBeInTheDocument();
    expect(screen.getByText("@executor")).toBeInTheDocument();
    expect(screen.getByText("@reviewer")).toBeInTheDocument();
    expect(screen.queryByTitle("Open src")).toBeNull();
  });

  it("filters member roles by the typed query", () => {
    renderComposer();
    type("@exe");
    expect(screen.getByText("@executor")).toBeInTheDocument();
    expect(screen.queryByText("@architect")).toBeNull();
  });

  it("inserts `@role ` when a member row is picked with Enter", () => {
    renderComposer();
    type("@");
    fireEvent.keyDown(textarea(), { key: "Enter" });
    expect(textarea().value).toBe("@architect ");
  });

  it("inserts `@role ` when a member row is clicked", () => {
    renderComposer();
    type("@rev");
    fireEvent.click(screen.getByTitle("Mention @reviewer"));
    expect(textarea().value).toBe("@reviewer ");
  });

  it("keeps file mentions working beside the Members section on native sessions", () => {
    useChatStore.setState({ sessionHarness: "claude-native" });
    renderComposer();
    type("@");
    expect(screen.getByText("@architect")).toBeInTheDocument();
    expect(screen.getByTitle("Open src")).toBeInTheDocument();
  });

  it("keeps no-member sessions on their normal model / effort controls", () => {
    sessionSnapshot.labels = { [`${MEMBER_KEY}architect`]: memberLabel({ lead: true }) };
    renderComposer({ showModels: true, showEffort: true, modelPickerKind: "claude" });
    expect(screen.getByTestId("composer-config-gear")).toBeInTheDocument();
    expect(screen.queryByTestId("composer-agent-members-trigger")).toBeNull();
    expect(textarea().placeholder).toBe("Send a message…");
  });
});

describe("unavailable members", () => {
  it.each([
    ["host_offline", "host offline"],
    ["harness_not_configured", "harness not set up on the host"],
    ["binary-missing", "CLI missing"],
    ["needs-auth", "sign-in needed"],
    ["version-too-low", "CLI too old"],
    ["model_missing", "model not offered by the host"],
  ])("shows the dot and banner reason for %s", (code) => {
    sessionSnapshot.labels = {
      [`${MEMBER_KEY}architect`]: memberLabel({ lead: true }),
      [`${MEMBER_KEY}reviewer`]: memberLabel({ unavailable: code }),
    };
    renderComposer();
    expect(screen.getByTestId("composer-members-alert")).toBeInTheDocument();
    expect(screen.getByTestId("agent-members-banner-reviewer")).toHaveTextContent("reviewer");
  });

  it("says coordination is paused when the lead is unavailable", () => {
    sessionSnapshot.labels = {
      [`${MEMBER_KEY}architect`]: memberLabel({ lead: true, unavailable: "host_offline" }),
      [`${MEMBER_KEY}executor`]: memberLabel(),
    };
    renderComposer();
    expect(screen.getByTestId("agent-members-banner-lead")).toHaveTextContent(
      "Coordination is paused",
    );
  });

  it("renders no dot or banner when every member can run", () => {
    sessionSnapshot.labels = multiMemberLabels();
    renderComposer();
    expect(screen.queryByTestId("composer-members-alert")).toBeNull();
    expect(screen.queryByTestId("agent-members-banner")).toBeNull();
  });
});
