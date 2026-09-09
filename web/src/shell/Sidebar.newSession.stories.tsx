import type { Meta, StoryObj } from "@storybook/react-vite";
import { onlineManager } from "@tanstack/react-query";
import { expect, userEvent, within } from "storybook/test";

import type { Conversation } from "@/hooks/useConversations";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { ExtensionCatalogProvider } from "@/extensions/ExtensionProvider";
import { writeNewSessionTarget } from "@/lib/newSessionTarget";
import { StoryQueryRouter } from "@/storybook/StoryProviders";
import { Sidebar } from "./Sidebar";

const sessions: Conversation[] = [
  {
    id: "session-alpha",
    object: "conversation",
    title: "Review project changes",
    agent_name: "Example agent",
    project_id: "project-alpha",
    created_at: 1_780_000_000,
    updated_at: 1_780_000_200,
    labels: { omni_project: "Alpha" },
    permission_level: 3,
  },
  {
    id: "session-unfiled",
    object: "conversation",
    title: "Inspect an unfiled working folder",
    agent_name: "Example agent",
    workspace: "/workspace/example-project/maintenance/long-folder-name",
    created_at: 1_780_000_000,
    updated_at: 1_780_000_100,
    labels: {},
    permission_level: 3,
  },
];

const meta = {
  title: "Components/Shell/NewSessionTarget",
  component: Sidebar,
  tags: ["visual-snapshot"],
  beforeEach: () => {
    // This interaction uses synthetic cached data; pause background polling.
    const wasOnline = onlineManager.isOnline();
    onlineManager.setOnline(false);
    return () => onlineManager.setOnline(wasOnline);
  },
  args: { open: true, onClose: () => undefined },
  render: (args) => <Sidebar {...args} onClose={() => undefined} />,
  decorators: [
    (Story) => (
      <StoryQueryRouter
        route="/c/session-unfiled"
        seed={(client) => {
          writeNewSessionTarget({ kind: "none" });
          client.setQueryData(
            ["projects"],
            [
              { id: "project-alpha", name: "Alpha" },
              { id: "project-beta", name: "Beta" },
            ],
          );
          const data = {
            pages: [
              {
                data: sessions,
                first_id: sessions[0].id,
                last_id: sessions[1].id,
                has_more: false,
              },
            ],
            pageParams: [undefined],
          };
          for (const includeArchived of [true, false]) {
            client.setQueryData(["conversations", "", includeArchived], data);
            for (const visibility of ["mine", "shared", "archived"]) {
              client.setQueryData(["conversations", "", includeArchived, null, visibility], data);
            }
          }
          client.setQueryData(["pinned-conversations"], {
            conversations: [],
            filterHonored: false,
          });
          client.setQueryData(["project-sessions", "Alpha"], {
            ...data,
            pages: [{ ...data.pages[0], data: [sessions[0]] }],
          });
          client.setQueryData(["project-sessions", "Beta"], {
            ...data,
            pages: [{ ...data.pages[0], data: [] }],
          });
          client.setQueryData(["hosts", { includeSandbox: false }], []);
          client.setQueryData(["hosts", { includeSandbox: true }], []);
        }}
      >
        <CapabilitiesProvider info={{ ...FALLBACK_SERVER_INFO, single_user: true }}>
          <ExtensionCatalogProvider extensions={[]}>
            <div className="app-shell fixed inset-0 overflow-hidden bg-background">
              <Story />
            </div>
          </ExtensionCatalogProvider>
        </CapabilitiesProvider>
      </StoryQueryRouter>
    ),
  ],
} satisfies Meta<typeof Sidebar>;

export default meta;
type Story = StoryObj<typeof meta>;

export const SelectProjectThenNoProject: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    const alpha = await canvas.findByRole("button", { name: "Use Alpha for new sessions" });
    await userEvent.click(alpha);
    expect(alpha).toHaveAttribute("aria-pressed", "true");
    expect(canvas.getByTestId("new-chat-button")).toHaveAttribute("href", "/?project=Alpha");
    await userEvent.click(canvas.getByRole("button", { name: "Use No Project for new sessions" }));
    expect(canvas.getByTestId("new-chat-button")).toHaveAttribute("href", "/");
  },
};
