import { useState } from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import { Combobox, COMBOBOX_OPTION_THRESHOLD, type ComboboxOption } from "./combobox";

afterEach(cleanup);
const options: ComboboxOption[] = [
  { key: "a", label: "Alpha", description: "speech" },
  { key: "b", label: "Beta", description: "chat" },
  { key: "c", label: "Gamma", disabled: true },
];

function setup(mode: "select" | "editable", initial = "", choices = options) {
  const change = vi.fn();
  const pick = vi.fn();
  const openChange = vi.fn();
  function Example() {
    const [value, setValue] = useState(initial);
    return (
      <>
        <Combobox
          mode={mode}
          options={choices}
          value={value}
          aria-label="Model"
          onValueChange={(next) => {
            change(next);
            setValue(next);
          }}
          onPick={pick}
          onOpenChange={openChange}
        />
        <button type="button">Outside</button>
      </>
    );
  }
  render(<Example />);
  return { input: screen.getByRole("combobox"), change, pick, openChange, user: userEvent.setup() };
}

it.each(["select", "editable"] as const)(
  "reports each open/close transition in %s mode",
  async (mode) => {
    const { input, user, openChange } = setup(mode);
    expect(openChange).not.toHaveBeenCalled();
    await user.click(input);
    await user.click(input);
    await user.keyboard("{Escape}");
    await user.keyboard("{ArrowDown}{Escape}{ArrowUp}");
    await user.click(screen.getByRole("button", { name: "Outside" }));
    await user.click(input);
    await user.click(screen.getByRole("option", { name: /Alpha/ }));
    await user.clear(input);
    await user.type(input, "draft");
    await user.tab();
    expect(openChange.mock.calls).toEqual([
      [true],
      [false],
      [true],
      [false],
      [true],
      [false],
      [true],
      [false],
      [true],
      [false],
    ]);
  },
);

it("reports an initially auto-opened list", async () => {
  const openChange = vi.fn();
  render(
    <Combobox
      mode="select"
      options={options}
      value=""
      aria-label="Model"
      autoOpen
      onValueChange={vi.fn()}
      onOpenChange={openChange}
    />,
  );
  expect(openChange.mock.calls).toEqual([[true]]);
  await userEvent.keyboard("{Escape}");
  expect(openChange.mock.calls).toEqual([[true], [false]]);
});

it("owns open and closed ARIA, and selects through the portal without moving input focus", async () => {
  const { input, user, change, pick } = setup("select");
  expect(input).toHaveAttribute("aria-expanded", "false");
  expect(input).not.toHaveAttribute("aria-controls");
  expect(input).not.toHaveAttribute("aria-activedescendant");
  await user.click(input);
  expect(input).toHaveFocus();
  const list = screen.getByRole("listbox");
  expect(input).toHaveAttribute("aria-controls", list.id);
  await waitFor(() =>
    expect(input).toHaveAttribute(
      "aria-activedescendant",
      screen.getByRole("option", { name: /Alpha/ }).id,
    ),
  );
  await user.keyboard("{ArrowDown}{Enter}");
  expect(change).toHaveBeenLastCalledWith("b");
  expect(pick).toHaveBeenLastCalledWith(options[1]);
  expect(input).toHaveValue("Beta");
  expect(input).toHaveAttribute("aria-expanded", "false");
  expect(input).not.toHaveAttribute("aria-controls");
  expect(input).not.toHaveAttribute("aria-activedescendant");
});

it("restores select text on Escape and blur without committing a custom value", async () => {
  const { input, user, change } = setup("select", "a");
  await user.click(input);
  await user.clear(input);
  await user.type(input, "unknown");
  expect(screen.getByText("No matches")).toBeVisible();
  await user.keyboard("{Escape}");
  expect(input).toHaveValue("Alpha");
  await user.clear(input);
  await user.type(input, "unknown");
  await user.tab();
  expect(input).toHaveValue("Alpha");
  expect(change).not.toHaveBeenCalled();
});

it("commits arbitrary editable text on blur and through the typed option", async () => {
  const { input, user, change } = setup("editable");
  await user.type(input, "private-model");
  await user.tab();
  expect(change).toHaveBeenLastCalledWith("private-model");
  await user.click(input);
  await user.clear(input);
  await user.type(input, "other-model");
  await user.click(screen.getByRole("option", { name: 'Use "other-model" as typed' }));
  expect(change).toHaveBeenLastCalledWith("other-model");
});

it("closes on first Escape and restores the original editable value on the second", async () => {
  const { input, user, change } = setup("editable", "original");
  await user.click(input);
  await user.clear(input);
  await user.type(input, "draft");
  await user.keyboard("{Escape}");
  expect(input).toHaveValue("draft");
  expect(input).toHaveAttribute("aria-expanded", "false");
  await user.keyboard("{Escape}");
  expect(input).toHaveValue("original");
  expect(change).toHaveBeenLastCalledWith("original");
});

it("filters descriptions, prioritizes prefixes, caps options and explains the count", async () => {
  const choices = Array.from({ length: 80 }, (_, i) => ({
    key: `${i}`,
    label: `Model ${i}`,
    description: "speech",
  }));
  choices.push({ key: "prefix", label: "Speech first", description: "" });
  const { input, user } = setup("select", "", choices);
  await user.type(input, "SPEECH");
  expect(screen.getAllByRole("option")).toHaveLength(50);
  expect(screen.getAllByRole("option")[0]).toHaveTextContent("Speech first");
  expect(screen.getByText("Showing 50 of 81 — keep typing")).toBeVisible();
  expect(COMBOBOX_OPTION_THRESHOLD).toBe(5);
});

it("skips disabled options and opens on ArrowDown", async () => {
  const { input, user, change } = setup("select");
  input.focus();
  await user.keyboard("{ArrowDown}");
  expect(input).toHaveAttribute("aria-expanded", "true");
  await user.keyboard("{ArrowDown}{ArrowDown}{Enter}");
  expect(change).toHaveBeenLastCalledWith("b");
});

it("ignores Enter during IME composition", async () => {
  const { input, user, change } = setup("editable");
  await user.type(input, "draft");
  fireEvent.keyDown(input, { key: "Enter", isComposing: true });
  expect(change).not.toHaveBeenCalled();
});
