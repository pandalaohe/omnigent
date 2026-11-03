import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { UsagePage } from "./UsagePage";

const { trackClickMock } = vi.hoisted(() => ({ trackClickMock: vi.fn() }));

vi.mock("@/hooks/useUsageReport", () => ({
  useUsageReport: () => ({ data: null, isLoading: false, isError: false, refetch: vi.fn() }),
}));

vi.mock("@/lib/analytics", () => ({
  useOmnigentAnalytics: () => ({ trackClick: trackClickMock }),
}));

vi.mock("@/components/usage/CostTimelineChart", () => ({
  CostTimelineChart: () => <div data-testid="cost-timeline" />,
}));
vi.mock("@/components/usage/UsageBreakdownCharts", () => ({
  UsageBreakdownCharts: () => <div data-testid="usage-breakdown" />,
}));
vi.mock("@/components/usage/UsageSessionTable", () => ({
  UsageSessionTable: () => <div data-testid="usage-sessions" />,
}));

afterEach(() => {
  cleanup();
  trackClickMock.mockReset();
});

it("uses a segmented range with a clear selected state and working custom dates", () => {
  const { container } = render(<UsagePage />);
  const group = screen.getByRole("radiogroup", { name: "Usage range" });
  const thirty = within(group).getByRole("radio", { name: "30 days" });
  const seven = within(group).getByRole("radio", { name: "7 days" });
  const custom = within(group).getByRole("radio", { name: "Custom" });

  expect(thirty).toHaveAttribute("aria-checked", "true");
  expect(thirty).toHaveClass("bg-primary-cta", "text-primary-cta-foreground");
  expect(seven).not.toHaveClass("bg-primary-cta");
  expect(container.querySelectorAll('input[type="date"]')).toHaveLength(0);

  fireEvent.click(seven);
  expect(trackClickMock).toHaveBeenCalledWith("usage.range_7d", "button");
  expect(seven).toHaveAttribute("aria-checked", "true");
  expect(seven).toHaveClass("bg-primary-cta", "text-primary-cta-foreground");
  expect(thirty).toHaveAttribute("aria-checked", "false");
  expect(thirty).not.toHaveClass("bg-primary-cta");

  fireEvent.click(custom);
  expect(trackClickMock).toHaveBeenCalledWith("usage.range_custom", "button");
  expect(custom).toHaveAttribute("aria-checked", "true");
  expect(container.querySelectorAll('input[type="date"]')).toHaveLength(2);
});
