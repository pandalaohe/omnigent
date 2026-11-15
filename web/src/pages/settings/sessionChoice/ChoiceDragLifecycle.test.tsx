import { useState } from "react";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { ChoiceDragContext, ChoiceDraggable } from "./ChoiceDrag";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

it.each(["Escape", "blur", "policy change", "release"])(
  "allows the first normal click after mouse %s",
  (cancel) => {
    vi.useFakeTimers();
    function App({ revision = "draft" }: { revision?: string }) {
      const [editing, setEditing] = useState(false);
      return (
        <ChoiceDragContext reset={revision} onDrop={() => ({ focus: "source", message: "Moved" })}>
          <ChoiceDraggable id="source" data={{ kind: "work", work: "work" }} label="Source">
            Source
          </ChoiceDraggable>
          <button type="button" onClick={() => setEditing(true)}>
            Edit preset
          </button>
          {editing && (
            <label>
              Preset name
              <input defaultValue="Code" />
            </label>
          )}
          <input aria-label="Uncontrolled field" defaultValue="" />
        </ChoiceDragContext>
      );
    }
    const view = render(<App />);
    fireEvent.change(screen.getByLabelText("Uncontrolled field"), {
      target: { value: "Keep this edit" },
    });
    const handle = screen.getByRole("button", { name: "Drag Source" });
    fireEvent.mouseDown(handle, { button: 0, buttons: 1, clientX: 10, clientY: 10 });
    fireEvent.mouseMove(document, { buttons: 1, clientX: 30, clientY: 30 });
    expect(screen.getByText("Move Source")).toBeInTheDocument();
    if (cancel === "Escape") fireEvent.keyDown(document, { key: "Escape", code: "Escape" });
    if (cancel === "blur") fireEvent.blur(window);
    if (cancel === "policy change") view.rerender(<App revision="changed" />);
    fireEvent.mouseUp(document);
    fireEvent.click(screen.getByRole("button", { name: "Edit preset" }));
    expect(screen.getByLabelText("Preset name")).toHaveValue("Code");
    expect(screen.getByLabelText("Uncontrolled field")).toHaveValue("Keep this edit");
  },
);

it.each([true, false])("touch hold activation=%s cleans up before the next click", (hold) => {
  vi.useFakeTimers();
  const click = vi.fn();
  render(
    <ChoiceDragContext reset="draft" onDrop={() => ({ focus: "source", message: "Moved" })}>
      <ChoiceDraggable id="source" data={{ kind: "work", work: "work" }} label="Source">
        Source
      </ChoiceDraggable>
      <button type="button" onClick={click}>
        Edit preset
      </button>
    </ChoiceDragContext>,
  );
  const handle = screen.getByRole("button", { name: "Drag Source" });
  fireEvent.touchStart(handle, { touches: [{ clientX: 10, clientY: 10 }] });
  if (hold) act(() => vi.advanceTimersByTime(250));
  fireEvent.touchMove(document, { touches: [{ clientX: 30, clientY: 30 }] });
  if (hold) expect(screen.getByText("Move Source")).toBeInTheDocument();
  else {
    act(() => vi.advanceTimersByTime(500));
    expect(screen.queryByText("Move Source")).not.toBeInTheDocument();
  }
  fireEvent.touchCancel(document);
  fireEvent.click(screen.getByRole("button", { name: "Edit preset" }));
  expect(click).toHaveBeenCalledOnce();
});
