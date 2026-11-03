import { useCallback, useState } from "react";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AgentNameTag } from "@/components/AgentNameTag";
import {
  crossingClashes,
  inheritedAt,
  levelsBelow,
  resolveField,
  type LayeredLevel,
  type LayeredStore,
} from "@/lib/layeredValues";
import { LayeredEditor } from "./LayeredEditor";
import { LayeredTable, type LayeredCellLevel } from "./LayeredTable";

vi.mock("@/lib/userPreferencesSync", () => ({ queueUserPreferencePatch: vi.fn() }));

afterEach(cleanup);

type Store = LayeredStore<"state">;
type At = LayeredLevel;

const baseStore: Store = {
  corner: { state: "On" },
  rows: { codex: {} },
  cols: { desktop: { state: "Off" } },
  cells: { desktop: { codex: { state: "On" } } },
};
const NO_UNAVAILABLE: Record<string, string> = {};

function clone(store: Store): Store {
  return structuredClone(store);
}

function ownRecord(store: Store, at: At) {
  if (at.level === "corner") return store.corner;
  if (at.level === "row") return store.rows[at.row];
  if (at.level === "col") return store.cols[at.col];
  return store.cells[at.col]?.[at.row];
}

function withoutOwn(store: Store, at: At): Store {
  const next = clone(store);
  const record = ownRecord(next, at);
  if (record) delete record.state;
  if (at.level === "col") delete next.hostWins?.[at.col];
  return next;
}

function withValue(store: Store, at: At, value: string): Store {
  if (at.level === "cell" && inheritedAt(store, at, "state").value === value) {
    return withoutOwn(store, at);
  }
  const next = clone(store);
  if (at.level === "corner") next.corner.state = value;
  if (at.level === "row") {
    next.rows[at.row] ??= {};
    next.rows[at.row].state = value;
  }
  if (at.level === "col") {
    next.cols[at.col] ??= {};
    next.cols[at.col].state = value;
  }
  if (at.level === "cell") {
    next.cells[at.col] ??= {};
    next.cells[at.col][at.row] ??= {};
    next.cells[at.col][at.row].state = value;
  }
  return next;
}

function sourceName(source: LayeredLevel | null): string {
  if (!source) return "Built-in";
  if (source.level === "corner") return "All";
  if (source.level === "row") return "Codex";
  if (source.level === "col") return "Desktop";
  return "This cell";
}

function StatefulTable({
  initial = baseStore,
  unavailable = NO_UNAVAILABLE,
  showExtraRows = false,
}: {
  initial?: Store;
  unavailable?: Record<string, string>;
  showExtraRows?: boolean;
}) {
  const [store, setStore] = useState(initial);
  const [undo, setUndo] = useState<Store | null>(null);

  function clearBelow(at: At) {
    const previous = clone(store);
    let next = clone(store);
    for (const level of levelsBelow(store, at)) next = withoutOwn(next, level);
    setUndo(previous);
    setStore(next);
  }

  const renderSelectionActions = useCallback(
    (selected: readonly LayeredCellLevel[], clear: () => void) => (
      <>
        <button
          type="button"
          onClick={() => {
            let next = clone(store);
            for (const at of selected) next = withValue(next, at, "Off");
            setStore(next);
            clear();
          }}
        >
          Set values to Off
        </button>
        <button
          type="button"
          onClick={() => {
            let next = clone(store);
            for (const at of selected) next = withoutOwn(next, at);
            setStore(next);
            clear();
          }}
        >
          Use inherited
        </button>
      </>
    ),
    [store],
  );

  return (
    <LayeredTable
      label="Keep-warm test layers"
      cornerLabel="All agents on all hosts"
      rows={[
        {
          id: "codex",
          label: "Codex",
          content: <AgentNameTag agentId={null} name="Codex" harness="codex" />,
          mark:
            store.rows.codex?.state !== undefined &&
            store.rows.codex.state !==
              inheritedAt(store, { level: "row", row: "codex" }, "state").value,
        },
        ...(showExtraRows
          ? [
              { id: "native", label: "Native" },
              { id: "acp", label: "ACP agent" },
            ]
          : []),
      ]}
      columns={[
        {
          id: "desktop",
          label: "Desktop",
          hostWins: !!store.hostWins?.desktop,
          mark:
            store.cols.desktop?.state !== undefined &&
            store.cols.desktop.state !==
              inheritedAt(store, { level: "col", col: "desktop" }, "state").value,
        },
      ]}
      renderCell={(row, col) => {
        const unavailableText = unavailable[`${row}/${col}`];
        if (unavailableText) return { content: null, unavailable: unavailableText };
        const resolved = resolveField(store, row, col, "state");
        const own = store.cells[col]?.[row]?.state;
        const inherited = inheritedAt(store, { level: "cell", row, col }, "state");
        const clash = crossingClashes(store, row, col, ["state"])[0];
        return {
          content: String(resolved.value ?? "Auto"),
          mark: own !== undefined && own !== inherited.value,
          clash: clash
            ? `${clash.field}: Codex and Desktop differ; ${clash.winner === "row" ? "Codex" : "Desktop"} wins`
            : undefined,
        };
      }}
      renderEditor={(at, trigger) => {
        const inherited = inheritedAt(store, at, "state");
        const saved = ownRecord(store, at)?.state !== undefined;
        const below = levelsBelow(store, at);
        return (
          <LayeredEditor
            trigger={trigger}
            title={
              at.level === "corner"
                ? "All agents on all hosts"
                : at.level === "row"
                  ? "Codex, all hosts"
                  : at.level === "col"
                    ? "Desktop, all agents"
                    : "Codex on Desktop"
            }
            from={sourceName(inherited.source)}
            savedHere={saved}
            useInheritedLabel={
              at.level === "corner"
                ? undefined
                : `Use ${String(inherited.value ?? "default")} from ${sourceName(inherited.source)}`
            }
            onUseInherited={
              at.level === "corner"
                ? undefined
                : () => setStore((current) => withoutOwn(current, at))
            }
            onClear={saved ? () => setStore((current) => withoutOwn(current, at)) : undefined}
            clearBelowCount={below.length}
            onClearBelow={below.length ? () => clearBelow(at) : undefined}
            actions={
              <button
                type="button"
                disabled={!undo}
                onClick={() => {
                  if (undo) setStore(undo);
                  setUndo(null);
                }}
              >
                Undo
              </button>
            }
          >
            {at.level === "cell" && (
              <p className="text-sm text-muted-foreground">
                {store.hostWins?.desktop
                  ? "This cell, then Desktop, then Codex, then All."
                  : "This cell, then Codex, then Desktop, then All."}
              </p>
            )}
            <div className="flex gap-2">
              <button
                type="button"
                onClick={() => setStore((current) => withValue(current, at, "On"))}
              >
                Set On
              </button>
              <button
                type="button"
                onClick={() => setStore((current) => withValue(current, at, "Off"))}
              >
                Set Off
              </button>
            </div>
          </LayeredEditor>
        );
      }}
      selectionActions={renderSelectionActions}
    />
  );
}

describe("LayeredTable and LayeredEditor", () => {
  it("opens one whole-cell target, drops an equal cell value, and restores a differing mark", async () => {
    const user = userEvent.setup();
    render(<StatefulTable />);
    const cell = screen.getByTestId("layered-cell-codex-desktop");
    expect(cell).toHaveTextContent("On");
    expect(cell.closest("td")?.querySelectorAll("button")).toHaveLength(1);
    expect(cell.querySelector(".bg-pink-500")).not.toBeNull();

    await user.click(cell);
    expect(screen.getByText("Codex on Desktop")).toBeVisible();
    expect(screen.getByText("From Desktop")).toBeVisible();
    expect(screen.getByText("Saved here")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Set Off" }));

    expect(cell).toHaveTextContent("Off");
    expect(cell.querySelector(".bg-pink-500")).toBeNull();
    expect(screen.queryByText("Saved here")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Set On" }));
    expect(cell).toHaveTextContent("On");
    expect(cell.querySelector(".bg-pink-500")).not.toBeNull();
  });

  it("keeps an equal agent header without a dot and names the clash winner", async () => {
    const user = userEvent.setup();
    render(
      <StatefulTable
        initial={{
          corner: { state: "On" },
          rows: { codex: { state: "On" } },
          cols: { desktop: { state: "Off" } },
          cells: { desktop: { codex: {} } },
        }}
      />,
    );
    const row = screen.getByTestId("layered-row-codex");
    expect(row.querySelector(".bg-pink-500")).toBeNull();
    expect(screen.getByTestId("layered-cell-codex-desktop")).toHaveTextContent("On");
    const warning = screen.getByLabelText("Warning: state: Codex and Desktop differ; Codex wins");
    await user.hover(warning);
    expect(await screen.findByText("Click this cell to set it here.")).toBeVisible();
    expect(screen.queryByText("Saved here")).toBeNull();
    await user.unhover(warning);
    await user.keyboard("{Escape}");
    await user.click(row);
    expect(screen.getByText("Saved here")).toBeVisible();
  });

  it("shows a marked host and changes the cell's resolution order line", async () => {
    const user = userEvent.setup();
    render(
      <StatefulTable
        initial={{
          corner: { state: "On" },
          rows: { codex: { state: "On" } },
          cols: { desktop: { state: "Off" } },
          cells: { desktop: { codex: {} } },
          hostWins: { desktop: true },
        }}
      />,
    );
    expect(screen.getByText("Host wins")).toBeVisible();
    expect(
      screen.getByLabelText("Warning: state: Codex and Desktop differ; Desktop wins"),
    ).toBeVisible();
    await user.click(screen.getByTestId("layered-cell-codex-desktop"));
    expect(screen.getByText("This cell, then Desktop, then Codex, then All.")).toBeVisible();
  });

  it("selects a row, a column and the corner for page-owned bulk actions", async () => {
    const user = userEvent.setup();
    render(<StatefulTable />);
    await user.click(screen.getByRole("button", { name: "Select cells" }));
    await user.click(screen.getByTestId("layered-row-codex"));
    expect(screen.getByText("1 cell selected")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Set values to Off" }));
    expect(screen.getByTestId("layered-cell-codex-desktop")).toHaveTextContent("Off");

    await user.click(screen.getByTestId("layered-col-desktop"));
    expect(screen.getByText("1 cell selected")).toBeVisible();
    await user.click(screen.getByTestId("layered-corner"));
    expect(screen.getByText("0 cells selected")).toBeVisible();
    await user.click(screen.getByTestId("layered-corner"));
    expect(screen.getByText("1 cell selected")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Use inherited" }));
    expect(screen.getByTestId("layered-cell-codex-desktop")).toHaveTextContent("Off");
  });

  it("removes a newly unavailable cell from selection and bulk actions", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<StatefulTable />);
    await user.click(screen.getByRole("button", { name: "Select cells" }));
    await user.click(screen.getByTestId("layered-cell-codex-desktop"));
    expect(screen.getByText("1 cell selected")).toBeVisible();

    rerender(<StatefulTable unavailable={{ "codex/desktop": "Not installed here" }} />);
    expect(screen.getByText("0 cells selected")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Use inherited" }));

    rerender(<StatefulTable />);
    const restored = screen.getByTestId("layered-cell-codex-desktop");
    expect(restored).toHaveTextContent("On");
    expect(restored.querySelector(".bg-pink-500")).not.toBeNull();
  });

  it("clears lower values and Host-wins marks, then Undo restores both", async () => {
    const user = userEvent.setup();
    render(
      <StatefulTable
        initial={{
          corner: { state: "On" },
          rows: { codex: { state: "Off" } },
          cols: { desktop: { state: "Off" } },
          cells: { desktop: { codex: { state: "On" } } },
          hostWins: { desktop: true },
        }}
      />,
    );
    await user.click(screen.getByTestId("layered-corner"));
    await user.click(screen.getByRole("button", { name: "Clear 3 values" }));
    expect(screen.queryByText("Host wins")).toBeNull();
    expect(screen.getByTestId("layered-cell-codex-desktop")).toHaveTextContent("On");
    await user.click(screen.getByRole("button", { name: "Undo" }));
    expect(screen.getByText("Host wins")).toBeVisible();
    expect(screen.getByTestId("layered-cell-codex-desktop")).toHaveTextContent("On");
    expect(
      screen.getByTestId("layered-cell-codex-desktop").querySelector(".bg-pink-500"),
    ).not.toBeNull();
  });

  it("renders unavailable cells as plain text and excludes them from selection", async () => {
    const user = userEvent.setup();
    render(
      <StatefulTable
        showExtraRows
        unavailable={{
          "codex/desktop": "Not on Windows",
          "native/desktop": "Not on Windows",
          "acp/desktop": "Not installed here",
        }}
      />,
    );
    expect(screen.getAllByText("Not on Windows")).toHaveLength(2);
    expect(screen.getByText("Not installed here")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Edit Codex on Desktop" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Select cells" }));
    await user.click(screen.getByTestId("layered-corner"));
    expect(screen.getByText("0 cells selected")).toBeVisible();
  });

  it("displays the normalized runtime value rather than the stored seconds", () => {
    const store: LayeredStore<"intervalSeconds"> = {
      corner: {},
      rows: { codex: {} },
      cols: { desktop: {} },
      cells: { desktop: { codex: { intervalSeconds: 60 } } },
    };
    render(
      <LayeredTable
        label="Interval"
        cornerLabel="All"
        rows={[{ id: "codex", label: "Codex" }]}
        columns={[{ id: "desktop", label: "Desktop" }]}
        renderCell={(row, col) => ({
          content: `${
            Number(
              resolveField(store, row, col, "intervalSeconds", {
                normalize: (_row, _field, value) => Math.max(300, Number(value)),
              }).value,
            ) / 60
          } min`,
        })}
        renderEditor={(_at, trigger) => trigger}
      />,
    );
    expect(screen.getByTestId("layered-cell-codex-desktop")).toHaveTextContent("5 min");
    expect(screen.getByTestId("layered-cell-codex-desktop")).not.toHaveTextContent("1 min");
  });
});
