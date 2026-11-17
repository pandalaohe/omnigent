import { useState } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { ExceptionList } from "./ExceptionList";

vi.mock("@/components/AgentPicker", () => ({ AgentPicker: () => null }));
afterEach(cleanup);

function renderEditor(row: { value: string }, patch: (value: { value: string }) => void) {
  return (
    <input
      aria-label="Edit value"
      value={row.value}
      onChange={(event) => patch({ value: event.target.value })}
    />
  );
}

function Example() {
  const [rows, setRows] = useState<Record<string, { value: string }>>({
    alpha: { value: "one" },
    beta: { value: "two" },
  });
  return (
    <ExceptionList
      rows={rows}
      items={[]}
      summary={(row) => row.value}
      editor={renderEditor}
      onAdd={() => {}}
      onPatch={(key, patch) =>
        setRows((current) => ({ ...current, [key]: { ...current[key], ...patch } }))
      }
      onRemove={(key) =>
        setRows((current) =>
          Object.fromEntries(Object.entries(current).filter(([id]) => id !== key)),
        )
      }
    />
  );
}

it("filters exceptions but keeps the editing row and its actions available", () => {
  render(<Example />);
  fireEvent.click(screen.getByRole("button", { name: "Edit alpha" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Search exceptions" }), {
    target: { value: "beta" },
  });
  expect(screen.getByText("Showing 2 of 2")).toBeVisible();
  fireEvent.change(screen.getByRole("textbox", { name: "Edit value" }), {
    target: { value: "changed" },
  });
  expect(screen.getByText("changed")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Remove alpha" }));
  expect(screen.getByText("Showing 1 of 1")).toBeVisible();
  expect(screen.queryByText("alpha")).toBeNull();
});
