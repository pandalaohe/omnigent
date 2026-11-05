import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Host } from "@/hooks/useHosts";
import {
  callingDefaultsPatch,
  patchCallingDefaults,
  readCallingDefaults,
  useCallingDefaults,
  type CallingDefaultsTable,
} from "@/lib/callingDefaults";
import { AgentDefaultsTable, type AgentField } from "./AgentDefaultsTable";

const { queuePatch } = vi.hoisted(() => ({ queuePatch: vi.fn() }));
vi.mock("@/lib/userPreferencesSync", () => ({ queueUserPreferencePatch: queuePatch }));
const hosts: Host[] = [
  {
    host_id: "desktop",
    name: "Desktop",
    owner: "local",
    status: "online",
    platform: "darwin",
    configured_harnesses: { "codex-native": true, "claude-native": true },
  },
  {
    host_id: "server",
    name: "Server",
    owner: "local",
    status: "offline",
    platform: "linux",
    configured_harnesses: { "codex-native": true, "claude-native": true },
  },
];

function Table({
  field = "permission",
  hostList = hosts,
}: {
  field?: AgentField;
  hostList?: Host[];
}) {
  const master = useCallingDefaults();
  return (
    <AgentDefaultsTable
      hosts={hostList}
      master={master}
      field={field}
      catalogs={[]}
      onChange={(next) => patchCallingDefaults(callingDefaultsPatch(master, next))}
    />
  );
}

function seed(master: CallingDefaultsTable) {
  localStorage.setItem("omnigent:calling-defaults", JSON.stringify(master));
}

async function choose(value: string) {
  fireEvent.click(screen.getByRole("combobox", { name: "Permission" }));
  fireEvent.click(await screen.findByRole("option", { name: value }));
}

afterEach(() => {
  cleanup();
  localStorage.clear();
  queuePatch.mockReset();
});

describe("agent layered defaults", () => {
  it("shows skipped agents at a marked host and never offers Full access on totals", async () => {
    seed({
      "*": { "codex-native": { permission: "approve-for-me" } },
      desktop: { "*": { permission: "edit-automatic", host_wins: true } },
    });
    render(<Table />);
    expect(screen.getByTestId("layered-cell-codex-native-desktop")).toHaveTextContent(
      "Approve for me",
    );
    fireEvent.click(screen.getByTestId("layered-col-desktop"));
    expect(screen.getByText("Skipped on: Codex")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("combobox", { name: "Permission" }));
    expect(screen.queryByRole("option", { name: /Full access/i })).not.toBeInTheDocument();
    fireEvent.click(await screen.findByRole("option", { name: "Read only" }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(readCallingDefaults().desktop["*"]).toEqual({
      permission: "read-only",
      host_wins: true,
    });
    expect(screen.getByTestId("layered-cell-codex-native-desktop")).toHaveTextContent("Read Only");
    expect(screen.getByTestId("layered-col-desktop")).toHaveAccessibleName(/Host wins|Desktop/);
  });

  it("promotes one cell to every host and one Undo restores both original values", () => {
    const before = {
      desktop: { "codex-native": { effort: "high", model: "first", speed: "fast" } },
      server: { "codex-native": { model: "second", permission: "read-only" } },
    };
    seed(before);
    render(<Table field="model" />);
    fireEvent.click(screen.getByTestId("layered-cell-codex-native-desktop"));
    fireEvent.click(screen.getByRole("button", { name: "Use on all hosts" }));
    expect(queuePatch).toHaveBeenCalledTimes(1);
    expect(readCallingDefaults()).toEqual({
      "*": { "codex-native": { model: "first" } },
      desktop: { "codex-native": { effort: "high", speed: "fast" } },
      server: { "codex-native": { permission: "read-only" } },
    });
    expect(screen.getByTestId("layered-cell-codex-native-server")).toHaveTextContent("first");
    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    expect(readCallingDefaults()).toEqual({ ...before, "*": {} });
    expect(queuePatch).toHaveBeenCalledTimes(2);
    expect(screen.getByTestId("layered-cell-codex-native-server")).toHaveTextContent("second");
  });

  it("keeps an equal row saved until Use inherited explicitly clears it", () => {
    seed({ "*": { "*": { permission: "manual" }, "claude-native": { permission: "default" } } });
    render(<Table />);
    fireEvent.click(screen.getByTestId("layered-row-claude-native"));
    expect(screen.getByText("Saved here")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Use inherited" }));
    expect(readCallingDefaults()["*"]["claude-native"]).toEqual({});
    expect(readCallingDefaults()["*"]["*"]).toEqual({ permission: "manual" });
  });

  it("clears only the active field below a header and restores it with Undo", () => {
    seed({
      "*": {
        "*": { permission: "manual" },
        "codex-native": { permission: "approve-for-me", model: "row-model" },
      },
      desktop: {
        "*": { host_wins: true },
        "codex-native": { permission: "read-only", effort: "high" },
      },
    });
    const before = readCallingDefaults();
    render(<Table />);
    fireEvent.click(screen.getByTestId("layered-corner"));
    fireEvent.click(screen.getByRole("button", { name: "Clear 3 values" }));
    expect(readCallingDefaults()).toEqual({
      "*": { "*": { permission: "manual" }, "codex-native": { model: "row-model" } },
      desktop: { "*": {}, "codex-native": { effort: "high" } },
    });
    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    expect(readCallingDefaults()).toEqual(before);
  });

  it("does not list reserved keys and keeps Windows Native cells read-only", () => {
    seed({ "*": { "*": { permission: "manual" } } });
    render(
      <Table
        hostList={[
          { ...hosts[0], platform: "win32" },
          { ...hosts[1], host_id: "*", name: "Reserved" },
        ]}
      />,
    );
    expect(screen.queryByTestId("layered-row-*")).not.toBeInTheDocument();
    expect(screen.queryByTestId("layered-col-*")).not.toBeInTheDocument();
    expect(screen.queryByTestId("layered-cell-codex-native-desktop")).not.toBeInTheDocument();
    expect(screen.getAllByText("Not on Windows").length).toBeGreaterThan(0);
  });

  it("keeps the agent name visible when it has no advertised permission options", () => {
    render(<Table />);
    expect(screen.getByRole("rowheader", { name: /Devin/ })).toHaveTextContent(
      "No advertised permission options",
    );
    expect(screen.queryByTestId("layered-row-devin-native")).not.toBeInTheDocument();
  });

  it("saves Manual as a real permission value with one patch", async () => {
    render(<Table />);
    fireEvent.click(screen.getByTestId("layered-cell-claude-native-desktop"));
    await choose("Manual");
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(readCallingDefaults().desktop["claude-native"]).toEqual({ permission: "default" });
    expect(queuePatch).toHaveBeenCalledTimes(1);
  });
});
