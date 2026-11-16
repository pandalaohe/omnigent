import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it } from "vitest";
import { StatusIcon } from "./status-icon";
import { TooltipProvider } from "./tooltip";

afterEach(cleanup);

it("names the status and exposes the same explanation on keyboard focus", async () => {
  render(
    <TooltipProvider delayDuration={0}>
      <StatusIcon tone="warn" label="Model no longer listed" icon={<svg />} />
    </TooltipProvider>,
  );
  const icon = screen.getByRole("img", { name: "Model no longer listed" });
  await userEvent.setup().tab();
  expect(icon).toHaveFocus();
  expect(await screen.findByRole("tooltip")).toHaveTextContent("Model no longer listed");
});

it("can be omitted from the tab order", () => {
  render(
    <TooltipProvider>
      <StatusIcon tone="ok" label="Listed" icon={<svg />} focusable={false} />
    </TooltipProvider>,
  );
  expect(screen.getByRole("img", { name: "Listed" })).not.toHaveAttribute("tabindex");
});
