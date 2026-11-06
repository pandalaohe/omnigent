import { useState } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { TaskBoardSearch } from "./TaskBoardSearch";

afterEach(cleanup);
function Search() {
  const [selected, setSelected] = useState<string[]>([]);
  return (
    <>
      <TaskBoardSearch
        options={[
          { id: "alpha", label: "Alpha" },
          { id: "beta", label: "Beta" },
          { id: "gamma", label: "Gamma" },
        ]}
        selected={selected}
        onChange={setSelected}
      />
      <output aria-label="Picked">{selected.join(",")}</output>
    </>
  );
}
it("keeps the list open, selects text after Enter, and clears before closing with Escape", () => {
  render(<Search />);
  fireEvent.click(screen.getByRole("button", { name: "Search and filter" }));
  const input = screen.getByRole("combobox") as HTMLInputElement;
  fireEvent.change(input, { target: { value: "Al" } });
  fireEvent.keyDown(input, { key: "Enter" });
  expect(screen.getByLabelText("Picked").textContent).toBe("alpha");
  expect(input.selectionStart).toBe(0);
  expect(input.selectionEnd).toBe(2);
  fireEvent.change(input, { target: { value: "Be" } });
  fireEvent.keyDown(input, { key: "Enter" });
  expect(screen.getByLabelText("Picked").textContent).toBe("alpha,beta");
  fireEvent.keyDown(input, { key: "Escape" });
  expect(input.value).toBe("");
  fireEvent.keyDown(input, { key: "Backspace" });
  expect(screen.getByLabelText("Picked").textContent).toBe("alpha,beta");
  fireEvent.keyDown(input, { key: "Escape" });
  expect(screen.queryByRole("combobox")).toBeNull();
});
it("applies Ctrl+Enter, select all and invert to the listed results without altering mouse search text", () => {
  render(<Search />);
  fireEvent.click(screen.getByRole("button", { name: "Search and filter" }));
  const input = screen.getByRole("combobox") as HTMLInputElement;
  fireEvent.change(input, { target: { value: "a" } });
  fireEvent.keyDown(input, { key: "Enter", ctrlKey: true });
  expect(screen.getByLabelText("Picked").textContent).toBe("alpha,beta,gamma");
  fireEvent.click(screen.getByRole("button", { name: "Select none" }));
  expect(screen.getByLabelText("Picked").textContent).toBe("");
  fireEvent.change(input, { target: { value: "Al" } });
  fireEvent.click(screen.getByRole("option", { name: "Alpha" }));
  expect(input.value).toBe("Al");
  expect(screen.getByLabelText("Picked").textContent).toBe("alpha");
  fireEvent.click(screen.getByRole("button", { name: "Invert" }));
  expect(screen.getByLabelText("Picked").textContent).toBe("");
});
