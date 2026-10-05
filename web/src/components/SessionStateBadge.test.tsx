import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { KeepWarmStatus } from "@/hooks/useConversations";
import type { SessionState } from "@/hooks/useSessionState";
import {
  BackgroundActivityBadge,
  ColdIdleDot,
  GoalActivityBadge,
  SessionStateBadge,
} from "./SessionStateBadge";

function renderBadge(
  state: SessionState,
  props: { cold?: boolean; keepWarm?: KeepWarmStatus | null } = {},
) {
  return render(
    <TooltipProvider>
      <SessionStateBadge state={state} {...props} />
    </TooltipProvider>,
  );
}

const STOPPED_KEEP_WARM: KeepWarmStatus = {
  state: "stopped",
  stop_reason: "cap",
  episode: { pings: 4, cost_usd: 0.038, estimated: true, started_at: null },
  total: { pings: 9, cost_usd: 0.1, estimated: true },
  last_return: null,
  last_reason: null,
};

afterEach(cleanup);

describe("SessionStateBadge — per-state rendering", () => {
  it("renders awaiting as a 'Needs response' tag with a count-aware accessible label", () => {
    renderBadge({ kind: "awaiting", count: 3 });
    const badge = screen.getByTestId("session-state-badge");
    expect(badge).toHaveAttribute("data-state", "awaiting");
    expect(badge).toHaveAttribute("aria-label", "3 approval prompts waiting");
    // The approval indicator is a visible text tag (not an icon-only dot), so
    // it reads as "Needs response" at a glance in the row.
    expect(badge).toHaveTextContent("Needs response");
  });

  it("uses singular wording when only one prompt is pending", () => {
    renderBadge({ kind: "awaiting", count: 1 });
    expect(screen.getByTestId("session-state-badge")).toHaveAttribute(
      "aria-label",
      "1 approval prompt waiting",
    );
  });

  it("renders running with a spinning grey spinner", () => {
    const { container } = renderBadge({ kind: "running" });
    const badge = screen.getByTestId("session-state-badge");
    expect(badge).toHaveAttribute("data-state", "running");
    // The running indicator is a grey spinner; a missing spinner
    // (or the old success-tone dot grid) means it regressed.
    const spinner = container.querySelector('[data-testid="running-dot"]');
    expect(spinner).not.toBeNull();
    expect(spinner?.getAttribute("class")).toContain("animate-spin");
    expect(spinner?.getAttribute("class")).toContain("text-muted-foreground");
    expect(spinner?.getAttribute("class")).toContain("size-3");
    expect(container.querySelector(".bg-success")).toBeNull();
  });

  it("renders starting with the same spinner as running and its own label", () => {
    const { container } = renderBadge({ kind: "starting" });
    const badge = screen.getByTestId("session-state-badge");
    expect(badge).toHaveAttribute("data-state", "starting");
    expect(badge).toHaveAttribute("aria-label", "Session starting up");
    const spinner = container.querySelector('[data-testid="running-dot"]');
    expect(spinner).not.toBeNull();
    expect(spinner?.getAttribute("class")).toContain("animate-spin");
  });

  it("renders unseen messages as a solid (non-pulsing) brand-pink dot", () => {
    const { container } = renderBadge({ kind: "unseen" });
    const badge = screen.getByTestId("session-state-badge");
    expect(badge).toHaveAttribute("aria-label", "New messages");
    expect(badge).toHaveAttribute("data-state", "unseen");
    // Unread reuses the brand-pink token but stays static; the pulsing
    // variant (running-pulse-dot) is reserved for the running state.
    const dot = container.querySelector(".bg-brand-accent");
    expect(dot).not.toBeNull();
    expect(dot?.getAttribute("class")).toContain("size-1.5");
    expect(dot?.getAttribute("class")).not.toContain("running-pulse-dot");
    expect(container.querySelector(".bg-info")).toBeNull();
  });

  it("renders a static CircleAlert in the same destructive color as error text", () => {
    const { container } = renderBadge({ kind: "error" });
    const badge = screen.getByRole("img", { name: "Latest message is an error" });
    expect(badge).toHaveAttribute("data-state", "error");
    const icon = container.querySelector("svg.lucide-circle-alert");
    expect(icon).toHaveClass("size-3.5", "shrink-0", "text-destructive");
    expect(icon).toHaveAttribute("aria-hidden", "true");
    expect(icon).not.toHaveClass("animate-spin", "animate-pulse", "text-brand-accent");
  });

  it("renders a host disconnect as a neutral outline dot", () => {
    const { container } = renderBadge({ kind: "disconnected" });
    const badge = screen.getByRole("img", { name: "Host disconnected" });
    expect(badge).toHaveAttribute("data-state", "disconnected");
    expect(container.querySelector("svg")).toBeNull();
    expect(badge.firstElementChild).toHaveClass(
      "size-2",
      "rounded-full",
      "border",
      "border-muted-foreground",
    );
  });
});

describe("SessionStateBadge — cold keep-warm", () => {
  it("renders a cold unseen as a blue dot and drops the brand pink", () => {
    const { container } = renderBadge(
      { kind: "unseen" },
      { cold: true, keepWarm: STOPPED_KEEP_WARM },
    );
    const badge = screen.getByTestId("session-state-badge");
    expect(badge).toHaveAttribute("data-state", "unseen");
    expect(badge).toHaveAttribute("data-cold", "true");
    expect(container.querySelector(".bg-keep-cold")).not.toBeNull();
    expect(container.querySelector(".bg-brand-accent")).toBeNull();
  });

  it("keeps the unseen dot brand-pink on a warm render", () => {
    const { container } = renderBadge({ kind: "unseen" });
    expect(screen.getByTestId("session-state-badge")).not.toHaveAttribute("data-cold");
    expect(container.querySelector(".bg-brand-accent")).not.toBeNull();
    expect(container.querySelector(".bg-keep-cold")).toBeNull();
  });

  it("renders a cold awaiting as a blue framed tag", () => {
    renderBadge({ kind: "awaiting", count: 2 }, { cold: true, keepWarm: STOPPED_KEEP_WARM });
    const badge = screen.getByTestId("session-state-badge");
    expect(badge).toHaveAttribute("data-state", "awaiting");
    expect(badge).toHaveAttribute("data-cold", "true");
    expect(badge).toHaveTextContent("Needs response");
    const tag = badge.firstElementChild;
    expect(tag).toHaveClass("border-keep-cold", "bg-keep-cold/15", "text-keep-cold");
    expect(tag).not.toHaveClass("bg-brand-accent/15");
  });

  it("leaves a cold running render exactly as before", () => {
    const { container } = renderBadge(
      { kind: "running" },
      { cold: true, keepWarm: STOPPED_KEEP_WARM },
    );
    const badge = screen.getByTestId("session-state-badge");
    expect(badge).not.toHaveAttribute("data-cold");
    expect(container.querySelector('[data-testid="running-dot"]')).not.toBeNull();
    expect(container.querySelector(".bg-keep-cold")).toBeNull();
  });

  it("appends the keep-warm line to the cold unseen tooltip", async () => {
    const user = userEvent.setup();
    renderBadge({ kind: "unseen" }, { cold: true, keepWarm: STOPPED_KEEP_WARM });
    await user.hover(screen.getByTestId("session-state-badge"));
    const tooltip = await screen.findByRole("tooltip");
    expect(tooltip).toHaveTextContent("New messages");
    expect(tooltip).toHaveTextContent("Keep-warm stopped: cap reached · 4 pings ≈$0.04");
  });

  it("appends the likely-cold line when no keep-warm status is known", async () => {
    const user = userEvent.setup();
    renderBadge({ kind: "awaiting", count: 2 }, { cold: true, keepWarm: null });
    await user.hover(screen.getByTestId("session-state-badge"));
    const tooltip = await screen.findByRole("tooltip");
    expect(tooltip).toHaveTextContent("2 approval prompts waiting");
    expect(tooltip).toHaveTextContent("Prompt cache likely cold");
  });
});

describe("ColdIdleDot", () => {
  it("renders a blue idle dot with the keep-warm tooltip", async () => {
    const user = userEvent.setup();
    const { container } = render(
      <TooltipProvider>
        <ColdIdleDot keepWarm={null} />
      </TooltipProvider>,
    );
    const dot = screen.getByTestId("session-state-badge");
    expect(dot).toHaveAttribute("data-state", "cold");
    expect(dot).toHaveAttribute("data-cold", "true");
    expect(container.querySelector(".bg-keep-cold")).not.toBeNull();
    await user.hover(dot);
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Prompt cache likely cold");
  });
});

describe("BackgroundActivityBadge", () => {
  it("renders a compact B with a count-aware accessible label", () => {
    render(
      <TooltipProvider>
        <BackgroundActivityBadge count={2} />
      </TooltipProvider>,
    );
    const badge = screen.getByTestId("background-activity-badge");
    expect(badge).toHaveTextContent("B");
    expect(badge).toHaveAttribute("aria-label", "2 background activities running");
  });
});

describe("GoalActivityBadge", () => {
  it.each([
    ["active", "Goal active", "text-status-green"],
    ["paused", "Goal paused", "text-status-yellow"],
  ] as const)("renders %s as a compact G", (state, label, tone) => {
    render(
      <TooltipProvider>
        <GoalActivityBadge state={state} />
      </TooltipProvider>,
    );
    const badge = screen.getByTestId("goal-activity-badge");
    expect(badge).toHaveTextContent("G");
    expect(badge).toHaveAttribute("aria-label", label);
    expect(badge).toHaveClass(tone);
  });
});
