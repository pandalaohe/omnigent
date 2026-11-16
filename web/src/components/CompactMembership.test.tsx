import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import { CompactMembership } from "./CompactMembership";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

it("shows the membership preview and describes the button on keyboard focus", async () => {
  const user = userEvent.setup();
  render(
    <CompactMembership
      label="Members"
      items={[
        { id: "alpha", label: "Alpha" },
        { id: "beta", label: "Beta" },
      ]}
    />,
  );

  await user.tab();
  const trigger = screen.getByRole("button", { name: "Members: 2" });
  expect(trigger).toHaveFocus();
  const tooltip = await screen.findByRole("tooltip");
  expect(tooltip.closest('[data-slot="tooltip-content"]')).toBeVisible();
  expect(tooltip).toHaveTextContent("Members · 2");
  expect(within(tooltip).getByText("Alpha")).toBeInTheDocument();
  expect(within(tooltip).getByText("Beta")).toBeInTheDocument();
  expect(trigger).toHaveAttribute("aria-describedby", tooltip.id);
  expect(trigger).toHaveAccessibleDescription(/Members · 2\s*Alpha\s*Beta/);
});

it.each([390, 1280])("keeps membership actions and keyboard dismissal at %ipx", async (width) => {
  const matchMedia = window.matchMedia;
  vi.spyOn(window, "matchMedia").mockImplementation((query) => ({
    ...matchMedia(query),
    matches: query === "(max-width: 767.98px)" ? width < 768 : matchMedia(query).matches,
  }));
  const user = userEvent.setup();
  const onRemove = vi.fn();
  const onKeyDown = vi.fn();
  render(
    <div onKeyDown={onKeyDown}>
      <CompactMembership
        label="Members"
        items={[
          { id: "alpha", label: "Alpha" },
          { id: "beta", label: "Beta" },
        ]}
        onRemove={onRemove}
      />
    </div>,
  );

  const trigger = screen.getByRole("button", { name: "Members: 2" });
  await user.tab();
  expect(trigger).toHaveFocus();
  await user.keyboard("{Enter}");
  const panel = screen.getByRole("dialog", { name: "Members" });
  expect(panel).toHaveAttribute("data-slot", width < 768 ? "dialog-content" : "popover-content");
  if (width < 768) expect(panel).toHaveClass("bottom-0", "w-full", "translate-y-0");
  const search = within(panel).getByRole("textbox", { name: "Search Members" });
  expect(search).toHaveFocus();
  onKeyDown.mockClear();
  await user.keyboard("{ArrowDown}{ArrowUp} ");
  expect(onKeyDown).not.toHaveBeenCalled();
  await user.clear(search);
  await user.type(search, "beta");
  expect(within(panel).getByText("Showing 1 of 2")).toBeVisible();
  expect(within(panel).queryByText("Alpha")).toBeNull();
  await user.click(within(panel).getByRole("button", { name: "Remove Beta" }));
  expect(onRemove).toHaveBeenCalledExactlyOnceWith("beta");
  await user.keyboard("{Escape}");
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  await waitFor(() => expect(trigger).toHaveFocus());

  if (width < 768) {
    await user.keyboard("{Enter}");
    await user.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(trigger).toHaveFocus());
  }
});

it("shows a compact count and filters the bounded detail list", () => {
  const onRemove = vi.fn();
  render(
    <CompactMembership
      label="Members"
      items={[
        { id: "alpha", label: "Alpha" },
        { id: "beta", label: "Beta" },
      ]}
      onRemove={onRemove}
    />,
  );

  const trigger = screen.getByRole("button", { name: "Members: 2" });
  expect(trigger).toHaveTextContent("2");
  expect(trigger).not.toHaveTextContent("Members");
  fireEvent.click(trigger);
  fireEvent.change(screen.getByRole("textbox", { name: "Search Members" }), {
    target: { value: "beta" },
  });
  expect(screen.getByText("Showing 1 of 2")).toBeVisible();
  expect(screen.queryByText("Alpha")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Remove Beta" }));
  expect(onRemove).toHaveBeenCalledWith("beta");
});

it("uses a muted dash when the membership is empty", () => {
  render(<CompactMembership label="Members" items={[]} />);

  expect(screen.getByLabelText("Members: 0")).toHaveTextContent("—");
  expect(screen.queryByRole("button")).toBeNull();
});
