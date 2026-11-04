import { useState } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LevelGlyph, LevelMeter, MenuLevelRow } from "./level-meter";
import { DropdownMenu, DropdownMenuContent, DropdownMenuTrigger } from "./dropdown-menu";

afterEach(cleanup);
const options = ["Low", "Medium", "High", "Higher", "Maximum"].map((label) => ({
  value: label,
  label,
}));

function Meter({ inherited, disabled }: { inherited?: string; disabled?: boolean }) {
  const [value, setValue] = useState<string | null>(null);
  return (
    <LevelMeter
      label="Thinking"
      options={options}
      value={value}
      inherited={inherited}
      disabled={disabled}
      onChange={setValue}
    />
  );
}

describe("LevelMeter", () => {
  it("selects the third block, clamps arrow focus, and resets to Default on the meter", () => {
    render(<Meter />);
    const radios = screen.getAllByRole("radio");
    expect(screen.getByText("Default")).toBeVisible();
    expect(radios.filter((radio) => radio.tabIndex === 0)).toHaveLength(1);
    fireEvent.click(radios[2]);
    expect(radios[2]).toHaveAttribute("aria-checked", "true");
    expect(radios[2].firstElementChild).toHaveClass("bg-companion");
    expect(radios[3].firstElementChild).toHaveClass("bg-muted-foreground/20");
    fireEvent.keyDown(radios[2], { key: "ArrowRight" });
    expect(radios[3]).toHaveFocus();
    fireEvent.keyDown(radios[4], { key: "ArrowRight" });
    expect(radios[4]).toHaveFocus();
    fireEvent.click(screen.getByRole("button", { name: "Reset Thinking" }));
    expect(screen.getByText("Default")).toBeVisible();
    expect(radios[0]).toHaveFocus();
    expect(screen.queryByRole("button", { name: "Reset Thinking" })).not.toBeInTheDocument();
  });

  it("shows a known inherited level faintly, keeps Default, and returns focus there on reset", () => {
    render(<Meter inherited="High" />);
    const radios = screen.getAllByRole("radio");
    expect(screen.getByText("Default")).toBeVisible();
    expect(radios[2]).toHaveAttribute("aria-checked", "false");
    expect(radios[2].firstElementChild).toHaveClass("bg-companion/45");
    expect(radios[3].firstElementChild).toHaveClass("bg-muted-foreground/20");
    fireEvent.click(radios[4]);
    fireEvent.click(screen.getByRole("button", { name: "Reset Thinking" }));
    expect(radios[2]).toHaveFocus();
  });

  it("keeps all blocks empty for an unknown default and respects disabled", () => {
    render(<Meter disabled />);
    for (const radio of screen.getAllByRole("radio")) {
      expect(radio).toBeDisabled();
      expect(radio.firstElementChild).toHaveClass("bg-muted-foreground/20");
    }
  });

  it("draws the chip glyph with an accessible level title", () => {
    render(<LevelGlyph level={3} total={5} title="Thinking: High" />);
    const glyph = screen.getByRole("img", { name: "Thinking: High" });
    expect(glyph.querySelectorAll(".bg-companion")).toHaveLength(3);
    expect(glyph.children).toHaveLength(5);
  });
});

describe("MenuLevelRow", () => {
  it("stops editing when the focused menu row becomes disabled", () => {
    const onChange = vi.fn();
    const onUnset = vi.fn();
    const menu = (disabled: boolean) => (
      <DropdownMenu defaultOpen>
        <DropdownMenuTrigger>Open thinking menu</DropdownMenuTrigger>
        <DropdownMenuContent>
          <MenuLevelRow
            label="Thinking"
            options={options}
            value="Low"
            onChange={onChange}
            onUnset={onUnset}
            disabled={disabled}
          />
        </DropdownMenuContent>
      </DropdownMenu>
    );
    const { rerender } = render(menu(false));
    const row = screen.getByRole("menuitem");
    fireEvent.focus(row);
    rerender(menu(true));
    fireEvent.keyDown(row, { key: "ArrowRight" });
    fireEvent.keyDown(row, { key: "ArrowLeft" });
    expect(onChange).not.toHaveBeenCalled();
    expect(onUnset).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Reset Thinking" })).toBeDisabled();
  });
  it.each([true, false])("keeps the menu open for pointer and lateral keys (reset %s)", (reset) => {
    function Menu() {
      const [value, setValue] = useState<string | null>("Low");
      return (
        <DropdownMenu defaultOpen>
          <DropdownMenuTrigger>Open thinking menu</DropdownMenuTrigger>
          <DropdownMenuContent>
            <MenuLevelRow
              label="Thinking"
              options={options}
              value={value}
              onChange={setValue}
              onUnset={reset ? () => setValue(null) : undefined}
            />
          </DropdownMenuContent>
        </DropdownMenu>
      );
    }
    render(<Menu />);
    const row = screen.getByRole("menuitem");
    fireEvent.keyDown(row, { key: "ArrowLeft" });
    expect(row).toHaveTextContent(reset ? "Default" : "Low");
    fireEvent.click(screen.getByRole("button", { name: "Thinking: High" }));
    expect(screen.getByRole("menu")).toBeVisible();
    expect(row).toHaveFocus();
    fireEvent.keyDown(row, { key: "ArrowRight" });
    expect(screen.getByRole("button", { name: "Thinking: Higher" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.queryByRole("button", { name: "Reset Thinking" }) !== null).toBe(reset);
  });
});
