import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { SessionDefaultModeSelect } from "./SessionDefaultModeSelect";

afterEach(cleanup);

it("uses the shared typeahead above five options and reports its open state", async () => {
  const onChange = vi.fn();
  const onOpenChange = vi.fn();
  render(
    <SessionDefaultModeSelect
      harness="claude-native"
      field="permission"
      value={null}
      label="Permission"
      testId="permission"
      onChange={onChange}
      onOpenChange={onOpenChange}
    />,
  );

  const input = screen.getByRole("combobox", { name: "Permission" });
  expect(input).toHaveValue("Default");
  fireEvent.click(input);
  await waitFor(() => expect(onOpenChange).toHaveBeenLastCalledWith(true));
  fireEvent.change(input, { target: { value: "Bypass" } });
  fireEvent.click(screen.getByRole("option", { name: "Bypass permissions" }));
  expect(onChange).toHaveBeenCalledWith("bypassPermissions");
  await waitFor(() => expect(onOpenChange).toHaveBeenLastCalledWith(false));
});

it("keeps a large typeahead disabled through native fieldset semantics", () => {
  render(
    <SessionDefaultModeSelect
      harness="claude-native"
      field="permission"
      value={null}
      label="Permission"
      testId="permission"
      disabled
      onChange={vi.fn()}
    />,
  );

  expect(screen.getByRole("combobox", { name: "Permission" })).toBeDisabled();
});
