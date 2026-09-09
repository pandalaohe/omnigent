import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { AgentBadgeEditor } from "./AgentBadgeEditor";

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

  it("offers eight outline presets and can switch existing text colors to the theme", () => {
    const onChange = vi.fn();
    render(
      <AgentBadgeEditor
        value={{ label: "AI", borderColor: "#111111", textColor: "#eeeeee" }}
        onChange={onChange}
      />,
    );
    expect(
      screen.getByRole("group", { name: "Outline presets" }).querySelectorAll("button"),
    ).toHaveLength(8);
    fireEvent.click(screen.getByRole("button", { name: "Blue outline" }));
    expect(onChange).toHaveBeenLastCalledWith({
      label: "AI",
      borderColor: "#3b82f6",
      textColor: "#eeeeee",
    });
    fireEvent.click(screen.getByRole("button", { name: "Follow theme" }));
    expect(onChange).toHaveBeenLastCalledWith({
      label: "AI",
      borderColor: "#3b82f6",
      textColor: "theme",
    });
    expect(screen.queryByLabelText("Text color hex")).toBeNull();
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
});
