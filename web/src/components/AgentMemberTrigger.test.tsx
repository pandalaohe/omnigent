import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
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

  it("opens a section as a page at phone width instead of a flyout", async () => {
    const originalMatchMedia = window.matchMedia;
    window.matchMedia = ((query: string) => ({
      ...originalMatchMedia(query),
      matches: query.includes("max-width"),
    })) as typeof window.matchMedia;
    try {
      renderTrigger("claude-native");
      fireEvent.keyDown(screen.getByTestId("agent-member-trigger"), { key: "ArrowDown" });
      fireEvent.click(screen.getByTestId("agent-member-effort"));

      const menu = await screen.findByTestId("agent-member-menu");
      expect(within(menu).getByTestId("agent-member-efforts")).toBeVisible();
      expect(screen.getAllByRole("menu")).toHaveLength(1);
      expect(menu).toHaveClass("w-[calc(100vw-1.5rem)]");

      fireEvent.click(screen.getByTestId("agent-member-config-back"));
      expect(screen.queryByTestId("agent-member-efforts")).toBeNull();
      expect(screen.getByTestId("agent-member-effort")).toBeVisible();
    } finally {
      window.matchMedia = originalMatchMedia;
    }
  });

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

  it("groups registry harness choices by their declared section", () => {
    renderTrigger("plugin-sdk", {
      harnessOptions: [
        { id: "native-x", label: "Native X", section: "native" },
        { id: "plugin-sdk", label: "Plugin SDK", section: "sdk" },
      ],
    });
    openTrigger();
    fireEvent.click(screen.getByTestId("agent-member-harness"));
    expect(screen.getByTestId("agent-member-harness-options-native")).toHaveTextContent("Native X");
    expect(screen.getByTestId("agent-member-harness-options-sdk")).toHaveTextContent("Plugin SDK");
  });

  it("hides undeployed choices until revealed while keeping the saved choice visible", () => {
    const onChange = vi.fn();
    renderTrigger("saved-sdk", {
      harnessOptions: [
        { id: "ready-sdk", label: "Ready SDK", section: "sdk", deployed: true },
        { id: "saved-sdk", label: "Saved SDK", section: "sdk", deployed: false },
        { id: "other-sdk", label: "Other SDK", section: "sdk", deployed: false },
      ],
      onChange,
    });
    openTrigger();
    fireEvent.click(screen.getByTestId("agent-member-harness"));
    const saved = screen.getByTestId("agent-member-harness-saved-sdk");
    expect(within(saved).getByText("Saved SDK", { exact: true })).toBeInTheDocument();
    expect(within(saved).getByText("Not installed here")).toBeInTheDocument();
    expect(screen.queryByTestId("agent-member-harness-other-sdk")).toBeNull();
    expect(screen.getByTestId("agent-member-show-undeployed")).toHaveTextContent(
      "Show 1 not installed here",
    );
    fireEvent.click(screen.getByTestId("agent-member-show-undeployed"));
    const other = screen.getByTestId("agent-member-harness-other-sdk");
    expect(within(other).getByText("Other SDK", { exact: true })).toBeInTheDocument();
    expect(within(other).getByText("Not installed here")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("agent-member-harness-other-sdk"));
    expect(onChange).toHaveBeenCalledWith({
      harness: "other-sdk",
      model: null,
      effort: null,
      hostId: null,
    });
  });
});
