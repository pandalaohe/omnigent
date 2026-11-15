import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { Highlight, SearchPages, ScopeChip } from "./browserSettingsParts";

afterEach(cleanup);
it("shows scope and keeps literal search matches and clear action accessible", () => {
  const clear = vi.fn();
  render(
    <>
      <ScopeChip />
      <ScopeChip server />
      <Highlight text="example.test/a.b" query="a.b" />
      <SearchPages query="a.b" setQuery={clear} total={30} count={2} kind="history" />
    </>,
  );
  expect(screen.getByText("This desktop")).toBeInTheDocument();
  expect(screen.getByText(/This server ·/)).toBeInTheDocument();
  expect(document.querySelector("mark")).toHaveTextContent("a.b");
  expect(screen.getByText("2 of 30 pages")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Clear search" }));
  expect(clear).toHaveBeenCalledWith("");
});
