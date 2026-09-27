import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ReactElement } from "react";

import { DropdownMenu, DropdownMenuContent } from "@/components/ui/dropdown-menu";
import { AgentMembersBanner, AgentMembersMenu } from "./AgentMembersMenu";
import type { ChildSessionInfo } from "@/hooks/useChildSessions";
import type { SessionMember } from "@/lib/sessionMembers";

afterEach(cleanup);

function member(overrides: Partial<SessionMember> = {}): SessionMember {
  return {
    role: "executor",
    host: "Mac",
    harness: "codex",
    model: "gpt-6-sol",
    effort: "xhigh",
    lead: false,
    unavailable: null,
    ...overrides,
  };
}

const MEMBERS: SessionMember[] = [
  member({
    role: "architect",
    host: "Mac",
    harness: "claude-sdk",
    model: "opus-1m",
    effort: "high",
    lead: true,
  }),
  member({ role: "executor" }),
  member({ role: "reviewer", host: "Desktop-HRF", harness: "codex", model: "gpt-6-astra" }),
];

function child(overrides: Partial<ChildSessionInfo> = {}): ChildSessionInfo {
  return {
    id: "conv_child",
    title: "executor:upload",
    task_summary: null,
    tool: "executor",
    session_name: "upload",
    labels: {},
    current_task_status: "completed",
    busy: false,
    last_message_preview: null,
    pending_elicitations_count: 0,
    ...overrides,
  };
}

function renderInMenu(ui: ReactElement) {
  return render(
    <MemoryRouter>
      <DropdownMenu open>
        <DropdownMenuContent>{ui}</DropdownMenuContent>
      </DropdownMenu>
    </MemoryRouter>,
  );
}

describe("AgentMembersMenu", () => {
  it("lists one row per member with the lead marked", () => {
    renderInMenu(<AgentMembersMenu members={MEMBERS} agentName="release-crew" />);
    expect(screen.getByTestId("agent-members-title")).toHaveTextContent("release-crew");
    expect(screen.getByTestId("agent-member-row-architect")).toBeInTheDocument();
    expect(screen.getByTestId("agent-member-row-executor")).toBeInTheDocument();
    expect(screen.getByTestId("agent-member-row-reviewer")).toBeInTheDocument();
    expect(screen.getByTestId("agent-member-lead-architect")).toHaveTextContent("Lead");
    expect(screen.queryByTestId("agent-member-lead-executor")).toBeNull();
    expect(screen.getByTestId("agent-member-row-executor")).toHaveTextContent(
      "Mac · gpt-6-sol · xhigh",
    );
  });

  it("explains that members are locked for this session", () => {
    renderInMenu(<AgentMembersMenu members={MEMBERS} />);
    expect(screen.getByTestId("agent-members-lock-note")).toHaveTextContent(
      "Members are fixed for this session; edits apply to new sessions",
    );
  });

  it("marks an unavailable member", () => {
    renderInMenu(
      <AgentMembersMenu
        members={[member({ role: "reviewer", unavailable: "host_offline" })]}
        agentName="release-crew"
      />,
    );
    expect(screen.getByTestId("agent-member-unavailable-reviewer")).toHaveTextContent(
      "unavailable",
    );
    expect(screen.getByTestId("agent-member-row-reviewer")).toHaveAttribute(
      "data-unavailable",
      "true",
    );
  });

  it("offers Open to the member's matching child session", () => {
    renderInMenu(
      <AgentMembersMenu members={MEMBERS} childSessions={[child({ id: "conv_exec" })]} />,
    );
    expect(screen.getByTestId("agent-member-open-executor")).toHaveAttribute(
      "href",
      "/c/conv_exec",
    );
    expect(screen.queryByTestId("agent-member-open-architect")).toBeNull();
  });

  it("matches a child by session name when the tool label differs", () => {
    renderInMenu(
      <AgentMembersMenu
        members={MEMBERS}
        childSessions={[child({ id: "conv_by_name", tool: null, session_name: "reviewer" })]}
      />,
    );
    expect(screen.getByTestId("agent-member-open-reviewer")).toHaveAttribute(
      "href",
      "/c/conv_by_name",
    );
  });

  it("shows Edit agent only when a saved-Agent callback is provided", () => {
    const onEditAgent = vi.fn();
    renderInMenu(<AgentMembersMenu members={MEMBERS} onEditAgent={onEditAgent} />);
    expect(screen.getByTestId("composer-agent-edit-agent")).toHaveTextContent("Edit agent");
    expect(onEditAgent).not.toHaveBeenCalled();
    cleanup();
    renderInMenu(<AgentMembersMenu members={MEMBERS} />);
    expect(screen.queryByTestId("composer-agent-edit-agent")).toBeNull();
  });
});

describe("AgentMembersBanner", () => {
  it("renders nothing when every member can run", () => {
    render(<AgentMembersBanner members={MEMBERS} />);
    expect(screen.queryByTestId("agent-members-banner")).toBeNull();
  });

  it.each([
    ["host_offline", "host offline"],
    ["harness_not_configured", "harness not set up on the host"],
    ["binary-missing", "CLI missing"],
    ["needs-auth", "sign-in needed"],
    ["version-too-low", "CLI too old"],
    ["model_missing", "model not offered by the host"],
  ])("names an unavailable role with the reason for %s", (code, reason) => {
    render(<AgentMembersBanner members={[member({ role: "reviewer", unavailable: code })]} />);
    const banner = screen.getByTestId("agent-members-banner");
    expect(banner).toHaveTextContent("reviewer can't run");
    expect(banner).toHaveTextContent(reason);
    expect(banner).toHaveTextContent("nothing was handed to another member");
  });

  it("says coordination is paused when the lead is unavailable", () => {
    render(
      <AgentMembersBanner
        members={[member({ role: "architect", lead: true, unavailable: "host_offline" })]}
      />,
    );
    expect(screen.getByTestId("agent-members-banner-lead")).toHaveTextContent(
      "Coordination is paused",
    );
    expect(screen.getByTestId("agent-members-banner-lead")).toHaveTextContent("host offline");
  });

  it("lists the lead and the other unavailable members together", () => {
    render(
      <AgentMembersBanner
        members={[
          member({ role: "architect", lead: true, unavailable: "binary-missing" }),
          member({ role: "reviewer", unavailable: "needs-auth" }),
        ]}
      />,
    );
    expect(screen.getByTestId("agent-members-banner-lead")).toHaveTextContent("CLI missing");
    expect(screen.getByTestId("agent-members-banner-reviewer")).toHaveTextContent("sign-in needed");
  });
});
