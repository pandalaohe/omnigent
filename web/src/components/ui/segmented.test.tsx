import { useState } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { Segmented } from "./segmented";

afterEach(cleanup);

it("selects one option, preserves one tab stop and cycles arrows in both directions", () => {
  function Example() {
    const [value, setValue] = useState("Tokens");
    return (
      <Segmented
        label="Usage unit"
        options={[
          { value: "Tokens", label: "Tokens" },
          { value: "Percent", label: "Percent" },
        ]}
        value={value}
        onChange={setValue}
        stretch
      />
    );
  }
  render(<Example />);
  const tokens = screen.getByRole("radio", { name: "Tokens" });
  const percent = screen.getByRole("radio", { name: "Percent" });
  fireEvent.click(percent);
  expect(percent).toHaveAttribute("aria-checked", "true");
  expect(tokens).toHaveAttribute("aria-checked", "false");
  expect(percent).toHaveAttribute("tabindex", "0");
  expect(tokens).toHaveAttribute("tabindex", "-1");
  fireEvent.keyDown(percent, { key: "ArrowRight" });
  expect(tokens).toHaveFocus();
  expect(tokens).toHaveAttribute("aria-checked", "true");
  fireEvent.keyDown(tokens, { key: "ArrowLeft" });
  expect(percent).toHaveFocus();
  expect(percent).toHaveAttribute("aria-checked", "true");
});
