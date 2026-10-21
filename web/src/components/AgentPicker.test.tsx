import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { RegistryItem } from "@/hooks/useAgentRegistry";
import { AgentPicker } from "./AgentPicker";

const items: RegistryItem[] = [
  {
    key: "harness:cursor-native",
    label: "Cursor",
    section: "native",
    harness: "cursor-native",
    description: null,
    source: "builtin",
    deployed: true,
  },
  {
    key: "harness:cursor",
    label: "Cursor",
    section: "sdk",
    harness: "cursor",
    description: null,
    source: "builtin",
    deployed: true,
  },
  {
    key: "harness:kiro-native",
    label: "Kiro",
    section: "native",
    harness: "kiro-native",
    description: null,
    source: "builtin",
    deployed: false,
  },
  {
    key: "harness:unsupported",
    label: "Unsupported",
    section: "other",
    harness: "unsupported",
    description: null,
    source: "builtin",
    deployed: true,
  },
];

afterEach(cleanup);

describe("settings agent picker", () => {
  it("keeps search visible when there are no agents", () => {
    render(
      <AgentPicker
        items={[]}
        taken={new Set()}
        onPick={vi.fn()}
        trigger={<button type="button">Add exception</button>}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Add exception" }));
    expect(screen.getByRole("textbox", { name: "Search agents" })).toHaveFocus();
  });

  it("searches distinct forms, marks taken rows, and reveals undeployed and unsupported rows", () => {
    const onPick = vi.fn();
    render(
      <AgentPicker
        items={items}
        taken={new Set(["harness:cursor-native"])}
        supports={(item) => (item.key === "harness:unsupported" ? "No channel" : null)}
        onPick={onPick}
        trigger={<button type="button">Add exception</button>}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Add exception" }));
    expect(screen.getByRole("dialog", { name: "Add exception" })).toHaveAttribute(
      "data-slot",
      "popover-content",
    );
    expect(screen.getByRole("textbox", { name: "Search agents" })).toHaveFocus();
    const native = within(screen.getByRole("region", { name: "Native CLI" }));
    expect(native.getByRole("button", { name: /Cursor.*already listed/ })).toBeDisabled();
    expect(
      within(screen.getByRole("region", { name: "SDK" })).getByRole("button", {
        name: /Cursor.*SDK/,
      }),
    ).toBeEnabled();
    expect(screen.queryByRole("button", { name: /Kiro.*not deployed/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Show 1 not deployed" }));
    expect(native.getByRole("button", { name: /Kiro.*not deployed/ })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "Not supported here (1)" }));
    expect(screen.getByRole("button", { name: /Unsupported.*No channel/ })).toBeDisabled();
    fireEvent.change(screen.getByRole("textbox", { name: "Search agents" }), {
      target: { value: "cursor" },
    });
    expect(screen.getAllByRole("button", { name: /Cursor/ })).toHaveLength(2);
    fireEvent.click(
      within(screen.getByRole("region", { name: "SDK" })).getByRole("button", {
        name: /Cursor.*SDK/,
      }),
    );
    expect(onPick).toHaveBeenCalledWith("harness:cursor");
  });
});
