import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { AgentBadgeEditor } from "./AgentBadgeEditor";

const LEGACY_OUTLINE_COLORS = [
  "#ef4444",
  "#ea580c",
  "#ca8a04",
  "#16a34a",
  "#0891b2",
  "#3b82f6",
  "#8b5cf6",
  "#db2777",
];

function cssColorToRgb(color: string): [number, number, number] {
  const rgb = /^rgb\(\s*(\d+),\s*(\d+),\s*(\d+)\s*\)$/i.exec(color);
  if (rgb) return [Number(rgb[1]), Number(rgb[2]), Number(rgb[3])];
  const hex = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(color);
  if (hex) {
    return [Number.parseInt(hex[1], 16), Number.parseInt(hex[2], 16), Number.parseInt(hex[3], 16)];
  }
  throw new Error(`Unexpected swatch color: ${color}`);
}

function rgbToHex([r, g, b]: [number, number, number]): string {
  return `#${[r, g, b].map((c) => c.toString(16).padStart(2, "0")).join("")}`;
}

function relativeLuminance([r, g, b]: [number, number, number]): number {
  const linear = (channel: number) => {
    const value = channel / 255;
    return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b);
}

describe("AgentBadgeEditor", () => {
  it("keeps a badge optional and emits only a complete valid value", () => {
    const onChange = vi.fn();
    const onValidityChange = vi.fn();
    render(
      <AgentBadgeEditor value={null} onChange={onChange} onValidityChange={onValidityChange} />,
    );

    expect(screen.queryByLabelText("Badge text")).toBeNull();
    expect(onValidityChange).toHaveBeenLastCalledWith(true);
    fireEvent.click(screen.getByRole("switch", { name: "Show badge" }));
    const label = screen.getByLabelText("Badge text");
    expect(onChange).not.toHaveBeenCalled();
    expect(onValidityChange).toHaveBeenLastCalledWith(false);

    fireEvent.change(label, { target: { value: "助手" } });
    expect(screen.getByText("Use one wide symbol or up to two narrow characters.")).toBeVisible();
    expect(onChange).not.toHaveBeenCalled();

    fireEvent.change(label, { target: { value: "AI" } });
    expect(onValidityChange).toHaveBeenLastCalledWith(true);
    expect(onChange).toHaveBeenLastCalledWith({
      label: "AI",
      borderColor: "#8b5cf6",
      textColor: "theme",
    });
  });

  it("renders a 5x8 chromatic palette ordered light to dark with every legacy color selectable", () => {
    const onChange = vi.fn();
    const { rerender } = render(
      <AgentBadgeEditor
        value={{ label: "AI", borderColor: "#111111", textColor: "#eeeeee" }}
        onChange={onChange}
      />,
    );

    const group = screen.getByRole("group", { name: "Badge color" });
    expect(group.className).toContain("grid-cols-8");
    const buttons = within(group).getAllByRole("button");
    expect(buttons).toHaveLength(40);

    const gridHexes = buttons.map((button) => {
      const chip = button.querySelector("span");
      expect(chip).not.toBeNull();
      const rgb = cssColorToRgb(chip!.style.backgroundColor);
      // Every swatch is chromatic (no black, white, or grey).
      expect(Math.max(...rgb) - Math.min(...rgb)).toBeGreaterThan(20);
      return { hex: rgbToHex(rgb), rgb };
    });

    // All 40 swatches are unique.
    expect(new Set(gridHexes.map((entry) => entry.hex)).size).toBe(40);

    // Each of the 8 hue columns goes monotonically light -> dark across its 5 rows.
    for (let col = 0; col < 8; col += 1) {
      const columnLuminances = [0, 1, 2, 3, 4].map((row) =>
        relativeLuminance(gridHexes[row * 8 + col].rgb),
      );
      for (let row = 0; row < 4; row += 1) {
        expect(columnLuminances[row]).toBeGreaterThan(columnLuminances[row + 1]);
      }
    }

    // Every legacy saved badge color stays present and shows as selected.
    for (const legacy of LEGACY_OUTLINE_COLORS) {
      expect(gridHexes.some((entry) => entry.hex === legacy)).toBe(true);
      rerender(
        <AgentBadgeEditor
          value={{ label: "AI", borderColor: legacy, textColor: "theme" }}
          onChange={onChange}
        />,
      );
      const pressed = within(screen.getByRole("group", { name: "Badge color" }))
        .getAllByRole("button")
        .filter((button) => button.getAttribute("aria-pressed") === "true");
      expect(pressed).toHaveLength(1);
    }
  });

  it("previews a badge color from the palette and retains its text color", () => {
    const onChange = vi.fn();
    render(
      <AgentBadgeEditor
        value={{ label: "AI", borderColor: "#111111", textColor: "#eeeeee" }}
        onChange={onChange}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Medium Blue outline" }));
    expect(screen.getByTestId("agent-badge-preview")).toHaveStyle({
      borderColor: "#3b82f6",
      color: "#eeeeee",
    });
    expect(onChange).toHaveBeenLastCalledWith({
      label: "AI",
      borderColor: "#3b82f6",
      textColor: "#eeeeee",
    });
    fireEvent.click(screen.getByRole("radio", { name: "Follow theme" }));
    expect(onChange).toHaveBeenLastCalledWith({
      label: "AI",
      borderColor: "#3b82f6",
      textColor: "theme",
    });
    expect(screen.queryByLabelText("Text color hex")).toBeNull();
  });

  it("provides an inline continuous spectrum, hue rail, and hex input", () => {
    const onChange = vi.fn();
    const onValidityChange = vi.fn();
    render(
      <AgentBadgeEditor
        value={{ label: "AI", borderColor: "#ff0000", textColor: "theme" }}
        onChange={onChange}
        onValidityChange={onValidityChange}
      />,
    );

    const spectrum = screen.getByTestId("agent-badge-color-spectrum");
    const hue = screen.getByTestId("agent-badge-color-hue") as HTMLInputElement;
    expect(spectrum).toBeVisible();
    expect(hue).toBeVisible();

    // Sliding the hue rail changes the color continuously.
    fireEvent.change(hue, { target: { value: "120" } });
    expect(onChange).toHaveBeenLastCalledWith({
      label: "AI",
      borderColor: "#00ff00",
      textColor: "theme",
    });

    // Keyboard stepping on the spectrum updates saturation/brightness continuously.
    fireEvent.keyDown(spectrum, { key: "ArrowDown", shiftKey: true });
    expect(onChange).toHaveBeenLastCalledWith({
      label: "AI",
      borderColor: "#00f200",
      textColor: "theme",
    });

    // Typing a 6-digit hex without '#' normalizes it; invalid hex marks invalid without breaking the picker.
    const hexInput = screen.getByLabelText("Outline color hex");
    fireEvent.change(hexInput, { target: { value: "1a2b3c" } });
    expect(onChange).toHaveBeenLastCalledWith({
      label: "AI",
      borderColor: "#1a2b3c",
      textColor: "theme",
    });

    fireEvent.change(hexInput, { target: { value: "#zzz" } });
    expect(hexInput).toHaveAttribute("aria-invalid", "true");
    expect(onValidityChange).toHaveBeenLastCalledWith(false);
    expect(screen.getByTestId("agent-badge-color-spectrum")).toBeVisible();

    // Pointer dragging to pure white preserves the active hue on the rail; non-primary mouse buttons are ignored.
    fireEvent.change(hexInput, { target: { value: "#00ff00" } });
    expect(hue.value).toBe("120");
    expect(spectrum).toHaveAttribute("aria-valuetext", "Saturation 100%, brightness 100%");
    vi.spyOn(spectrum, "getBoundingClientRect").mockReturnValue({
      left: 0,
      top: 0,
      width: 200,
      height: 100,
      right: 200,
      bottom: 100,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    });
    const callsBeforeRightClick = onChange.mock.calls.length;
    fireEvent.pointerDown(spectrum, {
      pointerId: 2,
      pointerType: "mouse",
      isPrimary: true,
      button: 2,
      clientX: 0,
      clientY: 0,
    });
    expect(onChange).toHaveBeenCalledTimes(callsBeforeRightClick);

    fireEvent.pointerDown(spectrum, {
      pointerId: 1,
      pointerType: "mouse",
      isPrimary: true,
      button: 0,
      buttons: 1,
      clientX: 0,
      clientY: 0,
    });
    // Another primary pointer cannot take over an active drag, and releasing the primary mouse button ends it.
    fireEvent.pointerDown(spectrum, {
      pointerId: 3,
      pointerType: "touch",
      isPrimary: true,
      button: 0,
      clientX: 200,
      clientY: 100,
    });
    expect(onChange).toHaveBeenLastCalledWith({
      label: "AI",
      borderColor: "#ffffff",
      textColor: "theme",
    });
    fireEvent.pointerMove(spectrum, {
      pointerId: 1,
      pointerType: "mouse",
      buttons: 2,
      clientX: 200,
      clientY: 100,
    });
    expect(onChange).toHaveBeenLastCalledWith({
      label: "AI",
      borderColor: "#ffffff",
      textColor: "theme",
    });
    fireEvent.pointerUp(spectrum, { pointerId: 1 });
    expect(hue.value).toBe("120");
    expect(spectrum).toHaveAttribute("aria-valuetext", "Saturation 0%, brightness 100%");
  });

  it("edits outline and text colors independently and can remove the badge", () => {
    const onChange = vi.fn();
    render(
      <AgentBadgeEditor
        value={{ label: "A", borderColor: "#111111", textColor: "#eeeeee" }}
        onChange={onChange}
      />,
    );

    fireEvent.change(screen.getByLabelText("Outline color hex"), {
      target: { value: "#123456" },
    });
    expect(onChange).toHaveBeenLastCalledWith({
      label: "A",
      borderColor: "#123456",
      textColor: "#eeeeee",
    });

    fireEvent.change(screen.getByLabelText("Text color hex"), { target: { value: "#abcdef" } });
    expect(onChange).toHaveBeenLastCalledWith({
      label: "A",
      borderColor: "#123456",
      textColor: "#abcdef",
    });

    fireEvent.click(screen.getByRole("switch", { name: "Show badge" }));
    expect(onChange).toHaveBeenLastCalledWith(null);
  });

  it("opens the 5x8 palette and continuous picker in a bottom sheet on mobile viewports", () => {
    const originalMatchMedia = window.matchMedia;
    window.matchMedia = vi.fn().mockImplementation((query: string) => ({
      matches: query === "(max-width: 767.98px)",
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }));
    try {
      const onChange = vi.fn();
      render(
        <AgentBadgeEditor
          value={{ label: "XS", borderColor: "#ca8a04", textColor: "theme" }}
          onChange={onChange}
        />,
      );

      // On mobile, the 40-swatch grid is tucked behind a visible color trigger button.
      const trigger = screen.getByTestId("agent-badge-color-trigger");
      expect(trigger).toHaveTextContent("#CA8A04");
      expect(screen.queryByTestId("agent-badge-color-spectrum")).toBeNull();

      fireEvent.click(trigger);
      const sheet = screen.getByRole("dialog", { name: "Badge color" });
      expect(
        within(sheet).getByRole("group", { name: "Badge color" }).querySelectorAll("button"),
      ).toHaveLength(40);
      expect(within(sheet).getByTestId("agent-badge-color-spectrum")).toBeVisible();
      expect(within(sheet).getByTestId("agent-badge-color-hue")).toBeVisible();

      fireEvent.click(within(sheet).getByRole("button", { name: "Medium Blue outline" }));
      expect(onChange).toHaveBeenLastCalledWith({
        label: "XS",
        borderColor: "#3b82f6",
        textColor: "theme",
      });

      fireEvent.click(within(sheet).getByRole("button", { name: "Done" }));
      expect(screen.queryByRole("dialog", { name: "Badge color" })).toBeNull();
      expect(screen.getByTestId("agent-badge-color-trigger")).toHaveTextContent("#3B82F6");
    } finally {
      window.matchMedia = originalMatchMedia;
    }
  });
});
