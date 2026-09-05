import type { Meta, StoryObj } from "@storybook/react-vite";
import type { Agent } from "@/hooks/useAgents";
import { StoryQueryRouter } from "@/storybook/StoryProviders";
import { useChatStore } from "@/store/chatStore";
import { ChatHeader } from "./ChatHeader";

const mobileMenu = {
  fileViewerOpen: false,
  panelOpen: false,
  terminalFirst: false,
  executionLogsOpen: false,
  filesPanelOpen: false,
  archivePanelOpen: false,
  subagentsPanelOpen: false,
  shellsPanelOpen: false,
  hideTerminalsTab: false,
  showShellsTab: false,
  terminalsLength: 0,
  debugMode: false,
  changedCount: 0,
  subagentsWorking: 0,
  agentCount: 1,
  onOpenFiles: () => undefined,
  onOpenChanges: () => undefined,
  onOpenArchive: () => undefined,
  onOpenShells: () => undefined,
  onOpenSubagents: () => undefined,
  onOpenMainExecutionLog: () => undefined,
};

const nativeAgent: Agent = {
  id: "agent-claude",
  name: "claude-native-ui",
  mcp_servers: [],
  policies: [],
};

const meta = {
  title: "Components/Shell/ChatHeader",
  component: ChatHeader,
  tags: ["visual-snapshot"],
  args: {
    onOpenSidebar: () => undefined,
    actionConversation: null,
    boundAgent: undefined,
    wrapperLabel: null,
    canShare: false,
    onShare: () => undefined,
    hasAgentInfo: false,
    onAgentInfo: () => undefined,
    hasHeaderMenu: false,
    showFilesPanel: false,
    hasRailContent: false,
    rightPanelOpen: false,
    onToggleRightPanel: () => undefined,
    mobileMenu,
  },
  decorators: [
    (Story, context) => {
      const previewWidth = (context.parameters.previewWidth as number | undefined) ?? 720;
      const nativeHeader = context.parameters.nativeMobileHeader as
        "server" | "conversation-title" | undefined;
      return (
        <StoryQueryRouter route="/c/conversation-story">
          <div
            className="app-shell relative h-20 max-w-[calc(100vw-3rem)] overflow-visible rounded-xl border bg-background"
            style={{ width: previewWidth }}
            data-ios-native={nativeHeader ? "true" : undefined}
            data-native-mobile-header={nativeHeader}
          >
            <Story />
          </div>
        </StoryQueryRouter>
      );
    },
  ],
} satisfies Meta<typeof ChatHeader>;

export default meta;
type Story = StoryObj<typeof meta>;

export const FiledSessionWithPresence: Story = {
  args: {
    sidebarOpen: false,
    isChildSession: false,
    conversationId: "conversation-story",
    conversationTitle: "Fix flaky login retries",
    projectName: "Payments",
    canShare: true,
    hasRailContent: true,
    showFilesPanel: true,
  },
  decorators: [
    (Story) => {
      useChatStore.setState({
        viewers: [
          { userId: "alice@example.com", idle: false },
          { userId: "bob@example.com", idle: true },
        ],
      });
      return <Story />;
    },
  ],
};

export const ChildSessionShareDisabled: Story = {
  decorators: [
    (Story) => {
      useChatStore.setState({ viewers: [] });
      return <Story />;
    },
  ],
  args: {
    sidebarOpen: true,
    isChildSession: true,
    conversationId: "conversation-child",
    conversationTitle: "Fix flaky login retries",
    projectName: null,
    titleLinkTo: "/c/conversation-parent",
    boundAgent: nativeAgent,
    wrapperLabel: "claude-code-native-ui-subagent",
    canShare: true,
    shareDisabled: true,
    shareDisabledReason: "Sharing requires a deployed server",
    hasRailContent: true,
    rightPanelOpen: true,
  },
};

const LONG_MOBILE_TITLE =
  "Investigate why unread session polling skips the latest production conversation";

export const MobileWebLongTitle: Story = {
  parameters: { previewWidth: 342 },
  args: {
    sidebarOpen: false,
    isChildSession: false,
    conversationId: "conversation-mobile-web",
    conversationTitle: LONG_MOBILE_TITLE,
    projectName: null,
  },
};

export const NativeIOSDefaultServerHeader: Story = {
  parameters: { previewWidth: 342, nativeMobileHeader: "server" },
  args: {
    sidebarOpen: false,
    isChildSession: false,
    conversationId: "conversation-native-server",
    conversationTitle: LONG_MOBILE_TITLE,
    projectName: null,
  },
};

export const NativeIOSTitleHeader: Story = {
  parameters: { previewWidth: 342, nativeMobileHeader: "conversation-title" },
  args: {
    sidebarOpen: false,
    isChildSession: false,
    conversationId: "conversation-native-title",
    conversationTitle: LONG_MOBILE_TITLE,
    // Exercises the mobile title-mode rule without letting the desktop-only
    // folder segment consume space in the native header.
    projectName: "Omnigent",
  },
};
