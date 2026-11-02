import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { SettingRow } from "./SettingsFields";
import { SettingsGroup } from "./SettingsGroup";

afterEach(cleanup);

it("keeps notes visible, help optional and the original longer hint reachable", async () => {
  const { rerender } = render(
    <SettingRow
      label="Always steer"
      note="Follow-ups go straight into the running turn."
      hint="The original longer explanation."
    >
      <button type="button">Choose mode</button>
    </SettingRow>,
  );
  expect(screen.getByText("Always steer")).toHaveClass("text-ui");
  expect(screen.getByText("Follow-ups go straight into the running turn.")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "About Always steer" }));
  expect(await screen.findByText("The original longer explanation.")).toBeVisible();
  rerender(
    <SettingRow label="Timeout">
      <button type="button">Change timeout</button>
    </SettingRow>,
  );
  expect(screen.queryByRole("button", { name: "About Timeout" })).not.toBeInTheDocument();
});

it("bounds ordinary groups and lets a wide table opt out", () => {
  const { rerender } = render(<SettingsGroup title="General">Rows</SettingsGroup>);
  expect(screen.getByRole("heading", { name: "General" })).toHaveClass("text-ui", "font-semibold");
  expect(screen.getByRole("heading").parentElement).toHaveClass("max-w-2xl");
  rerender(
    <SettingsGroup title="Hosts" className="max-w-none">
      Table
    </SettingsGroup>,
  );
  expect(screen.getByRole("heading").parentElement).toHaveClass("max-w-none");
  expect(screen.getByRole("heading").parentElement).not.toHaveClass("max-w-2xl");
});
