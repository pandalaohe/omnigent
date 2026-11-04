// The latest-point dot must sit on the last polyline vertex: a single-point
// series draws its point at x = 50, longer series end at x = 100. The alert
// line's label sits in its own right gutter, so it can never cover the chart,
// and it is display only.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { Sparkline } from "./Sparkline";

afterEach(cleanup);

describe("Sparkline", () => {
  it("centers the latest dot on a single-point series", () => {
    render(<Sparkline points={[42]} latest="live" />);

    expect(screen.getByTestId("sparkline-latest").style.left).toBe("50%");
  });

  it("puts the latest dot at the right edge on a multi-point series", () => {
    render(<Sparkline points={[10, 20]} latest="live" />);

    expect(screen.getByTestId("sparkline-latest").style.left).toBe("100%");
  });

  it("names the alert line in the chart's accessible label", () => {
    render(<Sparkline points={[10, 20]} threshold={15} thresholdLabel="85%" label="Laptop CPU" />);

    const chart = screen.getByRole("img");
    expect(chart).toHaveAttribute("aria-label", "Laptop CPU. Alert line 85%");
    // The gutter is a sibling of the plot, outside the scaled SVG.
    const label = screen.getByTestId("sparkline-threshold-label");
    expect(chart).not.toContainElement(label);
    expect(label).toHaveTextContent("85%");
  });

  it("opens nothing when the label is clicked", () => {
    render(<Sparkline points={[10, 20]} threshold={15} thresholdLabel="85%" label="Laptop CPU" />);

    const label = screen.getByTestId("sparkline-threshold-label");
    fireEvent.click(label);

    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.queryByRole("dialog")).toBeNull();
    // Nothing gained focus either: it is a label, not a control.
    expect(document.activeElement).toBe(document.body);
  });

  it("keeps the label inside its gutter when the line sits on an edge", () => {
    render(<Sparkline points={[10, 85]} threshold={85} thresholdLabel="85%" label="Laptop CPU" />);
    expect(
      Number.parseFloat(screen.getByTestId("sparkline-threshold-label").style.top),
    ).toBeLessThanOrEqual(88);
    cleanup();

    render(<Sparkline points={[85, 10]} threshold={85} thresholdLabel="85%" label="Laptop CPU" />);
    expect(
      Number.parseFloat(screen.getByTestId("sparkline-threshold-label").style.top),
    ).toBeGreaterThanOrEqual(12);
  });

  it("draws no gutter without a label", () => {
    render(<Sparkline points={[10, 20]} threshold={15} />);
    expect(screen.queryByTestId("sparkline-threshold-label")).toBeNull();
  });
});
