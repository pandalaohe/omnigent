import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PhoneBarScroller } from "./PhoneBarScroller";

function sizeScroller(element: HTMLElement, scrollWidth: number, clientWidth: number) {
  Object.defineProperty(element, "scrollWidth", { configurable: true, value: scrollWidth });
  Object.defineProperty(element, "clientWidth", { configurable: true, value: clientWidth });
}

describe("PhoneBarScroller", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("leaves desktop children unwrapped", () => {
    render(
      <div data-testid="bar">
        <PhoneBarScroller enabled={false}>
          <button type="button">Ring</button>
        </PhoneBarScroller>
      </div>,
    );
    expect(screen.queryByTestId("composer-workspace-scroller")).toBeNull();
    expect(screen.getByRole("button", { name: "Ring" }).parentElement).toBe(
      screen.getByTestId("bar"),
    );
  });

  it("marks the edges that hide controls as the row scrolls", () => {
    render(
      <PhoneBarScroller enabled>
        <button type="button">Ring</button>
      </PhoneBarScroller>,
    );
    const scroller = screen.getByTestId("composer-workspace-scroller");
    expect(scroller).not.toHaveAttribute("data-hidden-end");

    sizeScroller(scroller, 360, 300);
    fireEvent.scroll(scroller);
    expect(scroller).toHaveAttribute("data-hidden-end", "true");
    expect(scroller).not.toHaveAttribute("data-hidden-start");

    scroller.scrollLeft = 60;
    fireEvent.scroll(scroller);
    expect(scroller).toHaveAttribute("data-hidden-start", "true");
    expect(scroller).not.toHaveAttribute("data-hidden-end");
  });

  it("scrolls a keyboard-focused control clear of the faded edges", async () => {
    const scrollIntoView = vi.spyOn(Element.prototype, "scrollIntoView");
    render(
      <PhoneBarScroller enabled>
        <button type="button">Ring</button>
        <button type="button">Approval</button>
      </PhoneBarScroller>,
    );
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: "Ring" }));
    expect(scrollIntoView).not.toHaveBeenCalled();

    await user.tab();
    expect(screen.getByRole("button", { name: "Approval" })).toHaveFocus();
    expect(scrollIntoView).toHaveBeenCalledWith({ block: "nearest", inline: "nearest" });
  });
});
