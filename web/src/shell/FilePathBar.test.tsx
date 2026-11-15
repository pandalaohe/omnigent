import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { FilePathBar } from "./FilePathBar";

afterEach(cleanup);

function setup(validate = vi.fn().mockResolvedValue(undefined)) {
  const onNavigate = vi.fn();
  render(
    <FilePathBar
      path="docs/current.md"
      absolutePath="/work/docs/current.md"
      validate={validate}
      onNavigate={onNavigate}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "Edit file path: docs/current.md" }));
  return {
    input: screen.getByRole("textbox", { name: "File path" }) as HTMLInputElement,
    validate,
    onNavigate,
  };
}

it.each(["/work/target.md", "docs/target.md"])("validates and opens %s on Enter", async (path) => {
  const { input, validate, onNavigate } = setup();
  expect(input.selectionStart).toBe(0);
  expect(input.selectionEnd).toBe(input.value.length);
  fireEvent.change(input, { target: { value: path } });
  fireEvent.submit(input.closest("form")!);
  await waitFor(() => expect(onNavigate).toHaveBeenCalledWith(path));
  expect(validate).toHaveBeenCalledWith(path, expect.any(AbortSignal));
});

it.each(["Escape", "blur"])(
  "restores current path on %s and cancels a pending navigation",
  async (action) => {
    let finish!: () => void;
    const { input, onNavigate } = setup(
      vi.fn(
        () =>
          new Promise<void>((resolve) => {
            finish = resolve;
          }),
      ),
    );
    fireEvent.change(input, { target: { value: "target.md" } });
    fireEvent.submit(input.closest("form")!);
    if (action === "blur") fireEvent.blur(input);
    else fireEvent.keyDown(input, { key: "Escape" });
    await act(async () => {
      finish();
    });
    expect(onNavigate).not.toHaveBeenCalled();
    expect(
      screen.getByRole("button", { name: "Edit file path: docs/current.md" }),
    ).toBeInTheDocument();
  },
);

it("shows an inline error and leaves the current file open", async () => {
  const { input, onNavigate } = setup(vi.fn().mockRejectedValue(new Error("404 Not Found")));
  fireEvent.change(input, { target: { value: "missing.md" } });
  fireEvent.submit(input.closest("form")!);
  expect(await screen.findByRole("alert")).toHaveTextContent("404 Not Found");
  expect(onNavigate).not.toHaveBeenCalled();
  expect(input).toHaveAttribute("aria-invalid", "true");
});
