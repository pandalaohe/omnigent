import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CustomAgent, CustomAgentDetail } from "@/lib/customAgentsApi";
import { AgentEditor } from "./AgentEditor";

const mocks = vi.hoisted(() => ({
  getCustomAgent: vi.fn(),
  updateCustomAgent: vi.fn(),
  useHosts: vi.fn(),
  useHostModelOptions: vi.fn(),
  useNewChatHostId: vi.fn(),
}));

vi.mock("@/lib/customAgentsApi", () => ({
  getCustomAgent: mocks.getCustomAgent,
  updateCustomAgent: mocks.updateCustomAgent,
}));

vi.mock("@/lib/agentLabels", () => ({
  BRAIN_HARNESS_LABELS: { "claude-sdk": "Claude SDK" },
  useBrainHarnessLabels: () => ({ "claude-sdk": "Claude SDK" }),
}));

vi.mock("@/hooks/useHosts", () => ({
  useNewChatHostId: mocks.useNewChatHostId,
  useHosts: mocks.useHosts,
  useHostModelOptions: mocks.useHostModelOptions,
}));

const agent: CustomAgent = {
  id: "ca_release_crew",
  name: "release-crew",
  description: "Ships releases",
  harness: "claude-sdk",
  model: "opus",
  members: null,
  version: 5,
  created_at: 1,
  updated_at: null,
};

/** Legacy detail: no roster projection, so the editor builds one lead row. */
const legacyDetail: CustomAgentDetail = {
  ...agent,
  model: null,
  instructions: "Ship small changes.",
};

/** Two-member detail: lead plus one role. */
const crewDetail: CustomAgentDetail = {
  ...agent,
  members: [
    {
      name: "release-crew",
      description: "Ships releases",
      harness: "claude-sdk",
      model: "opus",
      reasoning_effort: "high",
      lead: true,
    },
    {
      name: "reviewer",
      description: "Reviews diffs",
      harness: "codex-native",
      model: null,
      reasoning_effort: null,
      lead: false,
    },
  ],
  instructions: "Ship small changes.",
};

/** One user host with a status, shaped like the `useHosts` rows. */
function host(hostId: string, name: string, status: "online" | "offline") {
  return { host_id: hostId, name, owner: "me", status };
}

/** Two-member detail whose members carry saved library hosts. */
const hostedDetail: CustomAgentDetail = {
  ...crewDetail,
  members: [
    { ...crewDetail.members![0], host_id: "host_a" },
    { ...crewDetail.members![1], host_id: "host_b" },
  ],
};

function renderEditor(detail: CustomAgentDetail, client?: QueryClient) {
  mocks.getCustomAgent.mockResolvedValue(detail);
  const queryClient =
    client ??
    new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: Infinity } },
    });
  const onClose = vi.fn();
  const onSaved = vi.fn().mockResolvedValue(undefined);
  render(
    <QueryClientProvider client={queryClient}>
      <AgentEditor agent={agent} onClose={onClose} onSaved={onSaved} />
    </QueryClientProvider>,
  );
  return { onClose, onSaved };
}

async function awaitLoaded(instructions = "Ship small changes.") {
  await waitFor(() =>
    expect(screen.getByTestId("agent-editor-instructions")).toHaveValue(instructions),
  );
}

function leadRow() {
  return screen.getAllByTestId("agent-member-row")[0];
}

function memberRows() {
  return screen.getAllByTestId("agent-member-row").slice(1);
}

function openTrigger(trigger: HTMLElement) {
  if (trigger.getAttribute("data-state") === "closed") {
    fireEvent.pointerDown(trigger, { button: 0, pointerType: "mouse" });
  }
}

function choose(trigger: HTMLElement, sectionTestId: string, optionTestId: string) {
  openTrigger(trigger);
  fireEvent.click(screen.getByTestId(sectionTestId));
  fireEvent.click(screen.getByTestId(optionTestId));
}

function saveButton() {
  return screen.getByTestId("agent-editor-save");
}

beforeEach(() => {
  mocks.getCustomAgent.mockReset();
  mocks.updateCustomAgent.mockReset();
  mocks.updateCustomAgent.mockResolvedValue(legacyDetail);
  mocks.useHosts.mockReset();
  mocks.useHosts.mockReturnValue({ data: [] });
  mocks.useHostModelOptions.mockReset();
  mocks.useHostModelOptions.mockReturnValue({ data: undefined });
  mocks.useNewChatHostId.mockReset();
  mocks.useNewChatHostId.mockReturnValue(null);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("AgentEditor", () => {
  it("sends an edited lead model in the roster, lead first", async () => {
    renderEditor(crewDetail);
    await awaitLoaded();
    openTrigger(within(leadRow()).getByTestId("agent-member-trigger"));
    fireEvent.click(screen.getByTestId("agent-member-model"));
    fireEvent.click(await screen.findByTestId("agent-member-model-sonnet"));
    fireEvent.click(saveButton());

    await waitFor(() =>
      expect(mocks.updateCustomAgent).toHaveBeenCalledWith(agent.id, {
        name: "release-crew",
        description: "Ships releases",
        instructions: "Ship small changes.",
        version: 5,
        members: [
          {
            name: "release-crew",
            description: "Ships releases",
            harness: "claude-sdk",
            model: "sonnet",
            reasoning_effort: "high",
            lead: true,
            host_id: null,
          },
          {
            name: "reviewer",
            description: "Reviews diffs",
            harness: "codex-native",
            model: null,
            reasoning_effort: null,
            lead: false,
            host_id: null,
          },
        ],
      }),
    );
  });

  it("adds a member after the lead, focusing its role name", async () => {
    renderEditor(crewDetail);
    await awaitLoaded();
    fireEvent.click(screen.getByTestId("agent-editor-add-member"));

    const added = memberRows()[1];
    const nameInput = within(added).getByTestId("agent-member-name");
    expect(nameInput).toHaveFocus();
    fireEvent.change(nameInput, { target: { value: "executor" } });
    fireEvent.change(within(added).getByTestId("agent-member-description"), {
      target: { value: "Writes code" },
    });
    choose(
      within(added).getByTestId("agent-member-trigger"),
      "agent-member-harness",
      "agent-member-harness-codex-native",
    );
    fireEvent.click(saveButton());

    await waitFor(() => {
      const [, patch] = mocks.updateCustomAgent.mock.calls.at(-1) as [
        string,
        { members: unknown[] },
      ];
      expect(patch.members).toHaveLength(3);
      expect(patch.members[0]).toMatchObject({ name: "release-crew", lead: true });
      expect(patch.members[1]).toMatchObject({ name: "reviewer", lead: false });
      expect(patch.members[2]).toEqual({
        name: "executor",
        description: "Writes code",
        harness: "codex-native",
        model: null,
        reasoning_effort: null,
        lead: false,
        host_id: null,
      });
    });
  });

  it("removes a member from the roster", async () => {
    renderEditor(crewDetail);
    await awaitLoaded();
    fireEvent.click(within(memberRows()[0]).getByTestId("agent-member-remove"));
    fireEvent.click(saveButton());

    await waitFor(() =>
      expect(mocks.updateCustomAgent).toHaveBeenCalledWith(
        agent.id,
        expect.objectContaining({
          members: [expect.objectContaining({ name: "release-crew", lead: true })],
        }),
      ),
    );
  });

  it("sends a name-only edit without members", async () => {
    renderEditor(crewDetail);
    await awaitLoaded();
    fireEvent.change(screen.getByTestId("agent-editor-name"), {
      target: { value: "release-crew-2" },
    });
    fireEvent.click(saveButton());

    await waitFor(() =>
      expect(mocks.updateCustomAgent).toHaveBeenCalledWith(agent.id, {
        name: "release-crew-2",
        description: "Ships releases",
        instructions: "Ship small changes.",
        version: 5,
      }),
    );
  });

  it("blocks Save on a duplicate role name", async () => {
    renderEditor(crewDetail);
    await awaitLoaded();
    fireEvent.click(screen.getByTestId("agent-editor-add-member"));
    fireEvent.change(within(memberRows()[1]).getByTestId("agent-member-name"), {
      target: { value: "reviewer" },
    });

    expect(
      screen.getByText("Role names must be unique and differ from the Agent name."),
    ).toBeInTheDocument();
    expect(saveButton()).toBeDisabled();
  });

  it("blocks Save on a role name outside the agent-name pattern", async () => {
    renderEditor(crewDetail);
    await awaitLoaded();
    fireEvent.click(screen.getByTestId("agent-editor-add-member"));
    fireEvent.change(within(memberRows()[1]).getByTestId("agent-member-name"), {
      target: { value: "back end" },
    });

    expect(
      screen.getByText("Role name must match [a-zA-Z0-9_-]+ (no dots, slashes, or whitespace)."),
    ).toBeInTheDocument();
    expect(saveButton()).toBeDisabled();
  });

  it("requires a model for the lead", async () => {
    renderEditor(crewDetail);
    await awaitLoaded();
    openTrigger(within(leadRow()).getByTestId("agent-member-trigger"));
    fireEvent.click(screen.getByTestId("agent-member-model"));
    fireEvent.click(await screen.findByTestId("agent-member-model-default"));

    expect(screen.getByText(/Pick a model/)).toBeInTheDocument();
    expect(saveButton()).toBeDisabled();
  });

  it("saves a name-only edit when the lead loaded without a model", async () => {
    renderEditor(legacyDetail);
    await awaitLoaded();
    fireEvent.change(screen.getByTestId("agent-editor-name"), {
      target: { value: "release-crew-2" },
    });

    expect(saveButton()).not.toBeDisabled();
    fireEvent.click(saveButton());

    await waitFor(() =>
      expect(mocks.updateCustomAgent).toHaveBeenCalledWith(agent.id, {
        name: "release-crew-2",
        description: "Ships releases",
        instructions: "Ship small changes.",
        version: 5,
      }),
    );
  });

  it("shows Unsaved changes only while the form differs", async () => {
    renderEditor(crewDetail);
    await awaitLoaded();
    const name = screen.getByTestId("agent-editor-name");
    expect(screen.queryByTestId("agent-editor-dirty")).toBeNull();

    fireEvent.change(name, { target: { value: "release-crew-2" } });
    expect(screen.getByTestId("agent-editor-dirty")).toHaveTextContent("Unsaved changes");

    fireEvent.change(name, { target: { value: "release-crew" } });
    expect(screen.queryByTestId("agent-editor-dirty")).toBeNull();
  });

  it("reloads the whole form from the conflicting server copy and saves its version", async () => {
    renderEditor(crewDetail);
    await awaitLoaded();
    const reloaded: CustomAgentDetail = {
      ...crewDetail,
      version: 6,
      instructions: "Ship big changes.",
      members: (crewDetail.members ?? []).map((member) =>
        member.lead ? member : { ...member, description: "Reviews everything" },
      ),
    };
    mocks.getCustomAgent.mockResolvedValueOnce(reloaded);
    mocks.updateCustomAgent
      .mockRejectedValueOnce(
        Object.assign(new Error("Custom Agent changed; reload before saving"), { status: 409 }),
      )
      .mockResolvedValueOnce(reloaded);

    fireEvent.change(screen.getByTestId("agent-editor-name"), {
      target: { value: "release-crew-2" },
    });
    fireEvent.click(saveButton());

    expect(
      await screen.findByText(
        "This Agent changed elsewhere and was reloaded — reapply your edits.",
      ),
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByTestId("agent-editor-instructions")).toHaveValue("Ship big changes."),
    );
    expect(screen.getByTestId("agent-editor-name")).toHaveValue("release-crew");
    expect(within(memberRows()[0]).getByTestId("agent-member-description")).toHaveValue(
      "Reviews everything",
    );

    fireEvent.change(screen.getByTestId("agent-editor-name"), {
      target: { value: "release-crew-3" },
    });
    fireEvent.click(saveButton());
    await waitFor(() =>
      expect(mocks.updateCustomAgent).toHaveBeenLastCalledWith(
        agent.id,
        expect.objectContaining({
          name: "release-crew-3",
          instructions: "Ship big changes.",
          version: 6,
        }),
      ),
    );
  });

  it("keeps a saved member's name read-only and only names a member added here", async () => {
    renderEditor(crewDetail);
    await awaitLoaded();

    expect(within(memberRows()[0]).getByText("reviewer")).toBeInTheDocument();
    expect(within(memberRows()[0]).getByTestId("agent-member-remove")).toHaveAccessibleName(
      "Remove reviewer",
    );
    expect(screen.queryByLabelText("Role name")).toBeNull();

    fireEvent.click(screen.getByTestId("agent-editor-add-member"));

    expect(within(memberRows()[1]).getByLabelText("Role name")).toBeInTheDocument();
    expect(screen.getAllByLabelText("Role name")).toHaveLength(1);
    expect(within(memberRows()[0]).queryByLabelText("Role name")).toBeNull();
  });

  it("warns that a removed saved member loses its settings, and not for an added one", async () => {
    renderEditor(crewDetail);
    await awaitLoaded();
    const hint = "Removing a member deletes its saved settings when you save.";

    fireEvent.click(screen.getByTestId("agent-editor-add-member"));
    fireEvent.click(within(memberRows()[1]).getByTestId("agent-member-remove"));
    expect(screen.queryByText(hint)).toBeNull();

    fireEvent.click(within(memberRows()[0]).getByTestId("agent-member-remove"));
    expect(screen.getByText(hint)).toBeInTheDocument();
  });

  it("ignores cached detail until a fetch after mount succeeds", async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: Infinity } },
    });
    client.setQueryData(["custom-agent", agent.id], crewDetail);
    const fresh: CustomAgentDetail = {
      ...crewDetail,
      version: 6,
      instructions: "Ship big changes.",
    };
    mocks.getCustomAgent.mockRejectedValueOnce(new Error("network down"));
    renderEditor(crewDetail, client);
    mocks.getCustomAgent.mockResolvedValue(fresh);

    expect(await screen.findByText("network down")).toBeInTheDocument();
    expect(screen.getByTestId("agent-editor-instructions")).toBeDisabled();
    expect(screen.getByTestId("agent-editor-name")).toBeDisabled();
    expect(saveButton()).toBeDisabled();

    await act(async () => {
      await client.refetchQueries({ queryKey: ["custom-agent", agent.id] });
    });

    await waitFor(() =>
      expect(screen.getByTestId("agent-editor-instructions")).toHaveValue("Ship big changes."),
    );
    expect(screen.getByTestId("agent-editor-name")).not.toBeDisabled();
    expect(saveButton()).not.toBeDisabled();
  });

  it("blocks a new member from reusing a removed saved member's name", async () => {
    renderEditor(crewDetail);
    await awaitLoaded();
    fireEvent.click(within(memberRows()[0]).getByTestId("agent-member-remove"));
    fireEvent.click(screen.getByTestId("agent-editor-add-member"));
    fireEvent.change(within(memberRows()[0]).getByTestId("agent-member-name"), {
      target: { value: "reviewer" },
    });

    expect(
      screen.getByText("reviewer was a saved member — undo its removal or save first"),
    ).toBeInTheDocument();
    expect(saveButton()).toBeDisabled();
  });

  it("lists the user's hosts in Host › and sends a picked host", async () => {
    mocks.useHosts.mockReturnValue({
      data: [host("host_a", "machine-a", "online"), host("host_b", "machine-b", "offline")],
    });
    renderEditor(hostedDetail);
    await awaitLoaded();

    openTrigger(within(leadRow()).getByTestId("agent-member-trigger"));
    fireEvent.click(screen.getByTestId("agent-member-host"));
    expect(await screen.findByTestId("agent-member-host-session")).toBeVisible();
    expect(screen.getByTestId("agent-member-host-host_a")).toBeVisible();
    expect(screen.getByTestId("agent-member-host-host_b")).toHaveTextContent("machine-b · offline");
    fireEvent.click(screen.getByTestId("agent-member-host-session"));
    fireEvent.click(saveButton());

    await waitFor(() => {
      const [, patch] = mocks.updateCustomAgent.mock.calls.at(-1) as [
        string,
        { members: { host_id: string | null }[] },
      ];
      expect(patch.members[0].host_id).toBeNull();
      expect(patch.members[1].host_id).toBe("host_b");
    });
  });

  it("warns on an offline member host and still saves", async () => {
    mocks.useHosts.mockReturnValue({
      data: [host("host_a", "machine-a", "online"), host("host_b", "machine-b", "offline")],
    });
    renderEditor(hostedDetail);
    await awaitLoaded();

    const warnings = screen.getAllByTestId("agent-member-host-warning");
    expect(warnings).toHaveLength(1);
    expect(warnings[0]).toHaveTextContent("machine-b is offline");
    expect(saveButton()).not.toBeDisabled();

    fireEvent.change(screen.getByTestId("agent-editor-name"), {
      target: { value: "release-crew-2" },
    });
    fireEvent.click(saveButton());
    await waitFor(() => expect(mocks.updateCustomAgent).toHaveBeenCalledTimes(1));
  });

  it("resolves each member's catalog against its own host", async () => {
    mocks.useHosts.mockReturnValue({
      data: [host("host_a", "machine-a", "online"), host("host_b", "machine-b", "online")],
    });
    renderEditor(hostedDetail);
    await awaitLoaded();

    expect(mocks.useHostModelOptions.mock.calls).toContainEqual(["host_a", "claude-sdk", true]);
    expect(mocks.useHostModelOptions.mock.calls).toContainEqual(["host_b", "codex-native", true]);
  });

  it("falls back to the New Chat host for a member without one", async () => {
    mocks.useNewChatHostId.mockReturnValue("host_session");
    renderEditor(crewDetail);
    await awaitLoaded();

    expect(mocks.useHostModelOptions.mock.calls).toContainEqual([
      "host_session",
      "claude-sdk",
      true,
    ]);
  });
});
