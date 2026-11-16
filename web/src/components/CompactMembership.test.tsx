import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { CompactMembership } from "./CompactMembership";

afterEach(cleanup);

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
