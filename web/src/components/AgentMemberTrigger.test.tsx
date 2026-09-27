import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AgentMemberTrigger } from "./AgentMemberTrigger";

vi.mock("@/hooks/useHosts", () => ({
  useHostModelOptions: () => ({ data: undefined, isLoading: false, error: null }),
}));

const HARNESS_OPTIONS = [
  { id: "claude-native", label: "Claude Code" },
  { id: "claude-sdk", label: "Claude SDK" },
  { id: "codex", label: "Codex" },
];

function renderTrigger(
  harness: string,
  extra: Partial<Parameters<typeof AgentMemberTrigger>[0]> = {},
) {
  return render(
    <AgentMemberTrigger
      harness={harness}
      model={null}
      effort={null}
      harnessOptions={HARNESS_OPTIONS}
      hostId={null}
      sessionHostId={null}
      onChange={vi.fn()}
      {...extra}
    />,
  );
}

function openTrigger() {
  const trigger = screen.getByTestId("agent-member-trigger");
  if (trigger.getAttribute("data-state") === "closed") {
    fireEvent.pointerDown(trigger, { button: 0, pointerType: "mouse" });
  }
}

afterEach(cleanup);

describe("AgentMemberTrigger", () => {
  it.each(["claude-sdk", "codex"])(
    "draws the 15px SDK mark before the vendor logo for the %s harness",
    (harness) => {
      renderTrigger(harness);
      const mark = screen.getByTestId("sdk-mark");
      expect(mark).toHaveClass("size-[15px]");
      const img = screen.getByTestId("agent-member-trigger").querySelector("img");
      expect(img).toBeTruthy();
      expect(mark.compareDocumentPosition(img!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    },
  );

  it("leaves a native harness without the SDK mark", () => {
    renderTrigger("claude-native");
    expect(screen.queryByTestId("sdk-mark")).toBeNull();
    expect(screen.getByTestId("agent-member-trigger").querySelector("img")).toBeTruthy();
  });

  it("lists the user's hosts with their online state and reports the pick", () => {
    const onChange = vi.fn();
    renderTrigger("claude-sdk", {
      hosts: [
        { host_id: "host_a", name: "machine-a", owner: "me", status: "online" },
        { host_id: "host_b", name: "machine-b", owner: "me", status: "offline" },
      ],
      onChange,
    });

    openTrigger();
    fireEvent.click(screen.getByTestId("agent-member-host"));
    expect(screen.getByTestId("agent-member-host-session")).toBeVisible();
    expect(screen.getByTestId("agent-member-host-host_a")).toBeVisible();
    expect(screen.getByTestId("agent-member-host-host_b")).toHaveTextContent("machine-b · offline");

    fireEvent.click(screen.getByTestId("agent-member-host-host_a"));
    expect(onChange).toHaveBeenCalledWith({
      harness: "claude-sdk",
      model: null,
      effort: null,
      hostId: "host_a",
    });
  });

  it("omits the Host section when the caller provides no hosts", () => {
    renderTrigger("claude-sdk");
    openTrigger();
    expect(screen.queryByTestId("agent-member-host")).toBeNull();
  });
});
