// The latest-point dot must sit on the last polyline vertex: a single-point
// series draws its point at x = 50, longer series end at x = 100.

import { cleanup, render, screen } from "@testing-library/react";
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
});
