import type { Decorator, Meta, StoryObj } from "@storybook/react-vite";
import { userEvent } from "storybook/test";
import { childSessionsQueryKey, type ChildSessionInfo } from "@/hooks/useChildSessions";
import type { Session } from "@/lib/types";
import { StoryQueryRouter } from "@/storybook/StoryProviders";
import { SubagentsPanel } from "./SubagentsPanel";

function child(overrides: Partial<ChildSessionInfo> & { id: string }): ChildSessionInfo {
  return {
    title: null,
    task_summary: null,
    tool: null,
    session_name: null,
    current_task_status: null,
    busy: false,
    last_message_preview: null,
    pending_elicitations_count: 0,
    ...overrides,
  };
}

function rootSession(overrides: Partial<Session> = {}): Session {
  return {
    id: "conversation-root",
    agentId: "agent-root",
    agentName: "orchestrator",
    runnerId: null,
    status: "idle",
    createdAt: 0,
    title: null,
    labels: {},
    items: [],
    pendingElicitations: [],
    permissionLevel: 4,
    parentSessionId: null,
    subAgentName: null,
    kind: "default",
    ...overrides,
  };
}

function panelEnvironment({
  activeId,
  session,
  tree,
  hosts = [],
  past = [],
}: {
  activeId: string;
  session: Session;
  tree: Record<string, ChildSessionInfo[]>;
  hosts?: { host_id: string; name: string; owner: string; status: "online" | "offline" }[];
  past?: ChildSessionInfo[];
}): Decorator {
  const referencedIds = new Set(
    Object.values(tree)
      .flat()
      .map((entry) => entry.id),
  );
  return (Story) => (
    <StoryQueryRouter
      route={`/c/${activeId}`}
      seed={(queryClient) => {
        queryClient.setQueryData(["session", session.id], session);
        queryClient.setQueryData(["hosts", { includeSandbox: false }], hosts);
        for (const [id, children] of Object.entries(tree)) {
          queryClient.setQueryData(childSessionsQueryKey(id), children);
        }
        for (const id of referencedIds) {
          if (!(id in tree)) queryClient.setQueryData(childSessionsQueryKey(id), []);
        }
        queryClient.setQueryData([...childSessionsQueryKey(session.id), "past"], {
          pages: [{ data: past, has_more: false, last_id: past.at(-1)?.id ?? null }],
          pageParams: [null],
        });
      }}
    >
      <div className="h-[520px] w-[320px] overflow-hidden rounded-lg border bg-card">
        <Story />
      </div>
    </StoryQueryRouter>
  );
}

const meta = {
  title: "Components/Shell/SubagentsPanel",
  component: SubagentsPanel,
  tags: ["visual-snapshot"],
  args: {
    rootSessionId: "conversation-root",
  },
} satisfies Meta<typeof SubagentsPanel>;

export default meta;
type Story = StoryObj<typeof meta>;

const statusTree = {
  "conversation-root": [
    child({
      id: "child-launching",
      title: "researcher:spec-scan",
      tool: "researcher",
      session_name: "spec-scan",
      current_task_status: "launching",
    }),
    child({
      id: "child-working",
      title: "frontend_engineer:rail",
      tool: "frontend_engineer",
      session_name: "rail",
      busy: true,
      last_message_preview: "Inspecting rail layout and status indicators…",
    }),
    child({
      id: "child-awaiting",
      title: "researcher:auth",
      tool: "researcher",
      session_name: "auth",
      busy: true,
      pending_elicitations_count: 1,
    }),
    child({
      id: "child-done",
      title: "Explore:find-callers",
      tool: "Explore",
      session_name: "find-callers",
      current_task_status: "completed",
      last_message_preview: "Found 14 call sites of the legacy API.",
    }),
    child({
      id: "child-failed",
      title: "pr-test-analyzer:ci",
      tool: "pr-test-analyzer",
      session_name: "ci",
      current_task_status: "failed",
      last_task_error: { code: "tool_error", message: "Tool raised ValueError" },
    }),
    child({
      id: "child-idle",
      title: "technical-writer:docs",
      tool: "technical-writer",
      session_name: "docs",
    }),
  ],
};

export const StatusSpectrum: Story = {
  args: { conversationId: "conversation-root" },
  decorators: [
    panelEnvironment({
      activeId: "conversation-root",
      session: rootSession({ status: "running", agentName: "deep-researcher" }),
      tree: statusTree,
    }),
  ],
};

const deepTree = {
  "conversation-root": [
    child({
      id: "child-coder",
      title: "frontend_engineer:rail-polish",
      tool: "frontend_engineer",
      session_name: "rail-polish",
      busy: true,
      routed_model: "databricks-claude-sonnet-5",
      last_message_preview: "Applying the depth-stepped gutter to nested rows.",
    }),
    child({
      id: "child-leaf",
      title: "researcher:audit",
      tool: "researcher",
      session_name: "audit",
      current_task_status: "completed",
    }),
  ],
  "child-coder": [
    child({
      id: "grandchild-active",
      title: "Explore:find-usages",
      tool: "Explore",
      session_name: "find-usages",
      current_task_status: "completed",
    }),
    child({
      id: "grandchild-test",
      title: "pr-test-analyzer:unit",
      tool: "pr-test-analyzer",
      session_name: "unit",
      busy: true,
    }),
  ],
  "grandchild-active": [
    child({
      id: "great-grandchild",
      title: "Explore:deep",
      tool: "Explore",
      session_name: "deep",
      current_task_status: "completed",
    }),
  ],
};

export const DeepTreeActiveGrandchild: Story = {
  args: { conversationId: "grandchild-active" },
  decorators: [
    panelEnvironment({
      activeId: "grandchild-active",
      session: rootSession({
        agentName: "claude-native-ui",
        labels: { "omnigent.wrapper": "claude-code-native-ui" },
      }),
      tree: deepTree,
    }),
  ],
};

const iconTree = {
  "conversation-root": [
    child({
      id: "child-claude",
      title: "claude_code:review",
      tool: "claude_code",
      session_name: "review",
      labels: { "omnigent.wrapper": "claude-code-native-ui" },
      busy: true,
    }),
    child({
      id: "child-codex",
      title: "codex:port-fix",
      tool: "codex",
      session_name: "port-fix",
      labels: { "omnigent.wrapper": "codex-native-ui" },
      current_task_status: "completed",
    }),
    child({ id: "child-pi", title: "pi:review-auth", tool: "pi", session_name: "review-auth" }),
    child({
      id: "child-custom",
      title: "ui:claude-native-ui:jimmy",
      tool: "claude-native-ui",
      session_name: "jimmy",
      labels: { "omnigent.wrapper": "claude-code-native-ui" },
    }),
  ],
  "child-claude": [
    child({
      id: "child-claude-sub",
      title: "Explore:find-the-bug",
      tool: "Explore",
      busy: true,
    }),
  ],
};

export const BrandIconsCollapsed: Story = {
  args: { conversationId: "conversation-root" },
  decorators: [
    panelEnvironment({
      activeId: "conversation-root",
      session: rootSession(),
      tree: iconTree,
    }),
  ],
  play: async ({ canvasElement }) => {
    const row = canvasElement.querySelector('[data-child-session-id="child-claude"]');
    const toggle = row
      ?.closest("li")
      ?.querySelector<HTMLElement>('[data-testid="subagent-collapse-toggle"]');
    if (!toggle) throw new Error("Sub-agent collapse toggle not found");
    await userEvent.click(toggle);
  },
};

const groupedHosts = [
  { host_id: "host-tmb", name: "TMB", owner: "u", status: "online" as const },
  { host_id: "host-fn", name: "fn", owner: "u", status: "online" as const },
];

const groupedTree = {
  "conversation-root": [
    child({
      id: "group-rail",
      title: "researcher:rail-shots",
      tool: "researcher",
      session_name: "rail-shots",
      host_id: "host-tmb",
      cwd: "/opt/work/omnigent/fork/omnigent-scc18-agents-rail",
      created_at: 5,
      busy: true,
      last_message_preview: "Checking the grouped rail layout…",
    }),
    child({
      id: "group-api",
      title: "codex:api-notes",
      tool: "codex",
      session_name: "api-notes",
      host_id: "host-tmb",
      cwd: "/opt/work/omnigent/fork/omnigent-scc18-agents-rail",
      created_at: 4,
      current_task_status: "completed",
    }),
    child({
      id: "group-async",
      title: "researcher:async-card",
      tool: "researcher",
      session_name: "async-card",
      host_id: "host-tmb",
      cwd: "/opt/work/omnigent/fork/omnigent-scc17-async-card",
      created_at: 3,
      busy: true,
      pending_elicitations_count: 1,
    }),
    child({
      id: "group-tests",
      title: "pr-test-analyzer:suite",
      tool: "pr-test-analyzer",
      session_name: "suite",
      host_id: "host-tmb",
      cwd: "/opt/work/omnigent/fork/omnigent-scc17-async-card",
      created_at: 2,
      warm_state: "warm",
    }),
    child({
      id: "group-fn",
      title: "researcher:repro",
      tool: "researcher",
      session_name: "repro",
      host_id: "host-fn",
      cwd: "/root/omnigent-dev",
      created_at: 1,
      last_message_preview: "Reproduced on fn.",
    }),
  ],
};

const archivedAt = Math.floor(Date.now() / 1000);
const groupedPast = [
  child({
    id: "past-status",
    title: "researcher:status",
    tool: "researcher",
    session_name: "status",
    host_id: "host-tmb",
    cwd: "/opt/work/omnigent/fork/omnigent-scc13-status",
    archived: true,
    archived_at: archivedAt - 3600,
  }),
  child({
    id: "past-flow",
    title: "codex:flow-timer",
    tool: "codex",
    session_name: "flow-timer",
    host_id: "host-fn",
    cwd: "/root/omnigent-dev",
    archived: true,
    archived_at: archivedAt - 86_400,
  }),
];

export const GroupedZones: Story = {
  args: { conversationId: "conversation-root" },
  decorators: [
    panelEnvironment({
      activeId: "conversation-root",
      session: rootSession({ hostId: "host-tmb" }),
      hosts: groupedHosts,
      tree: groupedTree,
      past: groupedPast,
    }),
  ],
  play: async ({ canvasElement }) => {
    const pastHeader = canvasElement.querySelector<HTMLElement>(
      '[data-testid="subagent-past-zone"]',
    );
    if (!pastHeader) throw new Error("Past zone header not found");
    await userEvent.click(pastHeader);
  },
};

// A real child session, Claude and Codex harness sub-agent mirrors, two hosts,
// running / idle / done rows. Same data in both trees.
const mixedWrapper = "omnigent.wrapper";
const MIXED_MAC = "host-mac";
const MIXED_FN = "host-fn";
const MIXED_CWD = "/opt/work/omnigent/fork/omnigent-feature";

const mixedHosts = [
  { host_id: MIXED_MAC, name: "TerrenceMBP.local", owner: "u", status: "online" as const },
  { host_id: MIXED_FN, name: "fn", owner: "u", status: "online" as const },
];

const mixedTree = {
  "conversation-root": [
    child({
      id: "real-child",
      title: "feature-rail",
      session_name: "feature-rail",
      agent_name: "claude-native-ui",
      harness: "claude-native",
      labels: { [mixedWrapper]: "claude-code-native-ui" },
      host_id: MIXED_MAC,
      cwd: MIXED_CWD,
      busy: true,
      warm_state: "warm",
      created_at: 9,
      last_message_preview: "Running the rail tests…",
    }),
    child({
      id: "mirror-explore",
      title: "Explore:a1b2c3",
      tool: "Explore",
      sub_agent_name: "Explore",
      labels: {
        [mixedWrapper]: "claude-code-native-ui-subagent",
        "omnigent.claude_native.description": "find rail callers",
      },
      host_id: MIXED_MAC,
      cwd: MIXED_CWD,
      busy: true,
      created_at: 8,
      last_message_preview: "Searching web/src/shell…",
    }),
    child({
      id: "mirror-review",
      title: "reviewer:d4e5f6",
      tool: "reviewer",
      sub_agent_name: "reviewer",
      labels: {
        [mixedWrapper]: "claude-code-native-ui-subagent",
        "omnigent.claude_native.description": "review rail diff",
      },
      host_id: MIXED_MAC,
      cwd: MIXED_CWD,
      current_task_status: "completed",
      created_at: 7,
      last_message_preview: "No blocking findings.",
    }),
    child({
      id: "mirror-codex",
      title: "worker:019a",
      tool: "worker",
      labels: { [mixedWrapper]: "codex-native-ui-subagent" },
      host_id: MIXED_MAC,
      cwd: MIXED_CWD,
      created_at: 6,
      last_message_preview: "Waiting for the next task.",
    }),
    child({
      id: "real-child-fn",
      title: "fn-repro",
      session_name: "fn-repro",
      agent_name: "codex-native-ui",
      harness: "codex-native",
      labels: { [mixedWrapper]: "codex-native-ui" },
      host_id: MIXED_FN,
      cwd: "/root/omnigent-dev",
      warm_state: "cold",
      created_at: 5,
      last_message_preview: "Reproduced on fn.",
    }),
  ],
  "real-child": [
    child({
      id: "grand-mirror",
      title: "Plan:77aa",
      tool: "Plan",
      sub_agent_name: "Plan",
      labels: {
        [mixedWrapper]: "claude-code-native-ui-subagent",
        "omnigent.claude_native.description": "plan rail fold",
      },
      host_id: MIXED_MAC,
      cwd: MIXED_CWD,
      current_task_status: "completed",
      created_at: 4,
    }),
  ],
};

const mixedPast = [
  child({
    id: "past-child",
    title: "status",
    session_name: "status",
    agent_name: "claude-native-ui",
    harness: "claude-native",
    labels: { [mixedWrapper]: "claude-code-native-ui" },
    host_id: MIXED_MAC,
    cwd: MIXED_CWD,
    archived: true,
    archived_at: archivedAt - 3600,
  }),
];

export const MixedKinds: Story = {
  args: { conversationId: "conversation-root" },
  decorators: [
    panelEnvironment({
      activeId: "conversation-root",
      session: rootSession({
        agentName: "claude-native-ui",
        labels: { [mixedWrapper]: "claude-code-native-ui" },
        hostId: MIXED_MAC,
        harness: "claude-native",
        status: "running",
      }),
      hosts: mixedHosts,
      tree: mixedTree,
      past: mixedPast,
    }),
  ],
  play: async ({ canvasElement }) => {
    const subagentsHeader = canvasElement.querySelector<HTMLElement>(
      '[data-testid="subagent-subagents-zone"]',
    );
    if (!subagentsHeader) throw new Error("Subagents zone header not found");
    await userEvent.click(subagentsHeader);
    const pastHeader = canvasElement.querySelector<HTMLElement>(
      '[data-testid="subagent-past-zone"]',
    );
    if (!pastHeader) throw new Error("Past zone header not found");
    await userEvent.click(pastHeader);
  },
};
