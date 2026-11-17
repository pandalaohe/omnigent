import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { defaultLayout } from "@/lib/sidebarLayout";
import { NewSectionDialog } from "./NewSectionDialog";

afterEach(cleanup);

it("searches the twenty recent-count choices and saves the picked count", () => {
  const layout = defaultLayout();
  const saveLayout = vi.fn();
  render(<NewSectionDialog open onOpenChange={vi.fn()} layout={layout} saveLayout={saveLayout} />);

  fireEvent.click(screen.getByTestId("new-section-kind-recent"));
  fireEvent.click(screen.getByTestId("new-section-continue"));
  const count = screen.getByRole("combobox", { name: "Sessions shown" });
  fireEvent.change(count, { target: { value: "12" } });
  fireEvent.click(screen.getByRole("option", { name: "12" }));
  fireEvent.click(screen.getByTestId("new-section-create"));

  expect(saveLayout).toHaveBeenCalledOnce();
  const update = saveLayout.mock.calls[0][0];
  const saved = typeof update === "function" ? update(layout) : update;
  expect(saved.sections[0]).toMatchObject({ kind: "recent", count: 12 });
});
