import { useState, type ReactElement, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import type { LayeredLevel } from "@/lib/layeredValues";
import { cn } from "@/lib/utils";

export interface LayeredTableRow {
  id: string;
  label: string;
  content?: ReactNode;
  detail?: ReactNode;
  mark?: boolean;
}

export interface LayeredTableColumn {
  id: string;
  label: string;
  content?: ReactNode;
  detail?: ReactNode;
  mark?: boolean;
  hostWins?: boolean;
}

export interface LayeredCell {
  content: ReactNode;
  mark?: boolean;
  clash?: string;
  unavailable?: string;
}

export type LayeredCellLevel = Extract<LayeredLevel, { level: "cell" }>;

export interface LayeredTableProps {
  label: string;
  cornerLabel: string;
  cornerDetail?: ReactNode;
  cornerMark?: boolean;
  rows: readonly LayeredTableRow[];
  columns: readonly LayeredTableColumn[];
  renderCell: (row: string, col: string) => LayeredCell;
  renderEditor: (at: LayeredLevel, trigger: ReactElement) => ReactNode;
  selectionActions?: (selected: readonly LayeredCellLevel[], clear: () => void) => ReactNode;
  className?: string;
  testId?: string;
}

function selectionKey(row: string, col: string): string {
  return JSON.stringify([row, col]);
}

function Mark() {
  return <span aria-hidden="true" className="size-1.5 shrink-0 rounded-full bg-pink-500" />;
}

/** Field-neutral layered grid; pages own values, editors and bulk mutations. */
export function LayeredTable({
  label,
  cornerLabel,
  cornerDetail,
  cornerMark,
  rows,
  columns,
  renderCell,
  renderEditor,
  selectionActions,
  className,
  testId,
}: LayeredTableProps) {
  const [selecting, setSelecting] = useState(false);
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const selectedLevels: LayeredCellLevel[] = [...selected].map((key) => {
    const [row, col] = JSON.parse(key) as [string, string];
    return { level: "cell", row, col };
  });

  const availableLevels: LayeredCellLevel[] = rows.flatMap((row) =>
    columns.flatMap((col) =>
      renderCell(row.id, col.id).unavailable
        ? []
        : [{ level: "cell" as const, row: row.id, col: col.id }],
    ),
  );

  function toggleCells(levels: readonly LayeredCellLevel[]) {
    const keys = levels.map(({ row, col }) => selectionKey(row, col));
    const allSelected = keys.length > 0 && keys.every((key) => selected.has(key));
    const next = new Set(selected);
    for (const key of keys) {
      if (allSelected) next.delete(key);
      else next.add(key);
    }
    setSelected(next);
  }

  function stopSelecting() {
    setSelecting(false);
    setSelected(new Set());
  }

  function editorButton(
    at: LayeredLevel,
    content: ReactNode,
    ariaLabel: string,
    mark?: boolean,
    clash?: string,
    levels?: readonly LayeredCellLevel[],
    cellTestId?: string,
  ) {
    const isSelected =
      !!levels?.length && levels.every(({ row, col }) => selected.has(selectionKey(row, col)));
    const button = (
      <button
        type="button"
        aria-label={ariaLabel}
        aria-pressed={selecting ? isSelected : undefined}
        disabled={selecting && levels?.length === 0}
        data-testid={cellTestId}
        className={cn(
          "flex min-h-11 w-full min-w-0 items-center justify-between gap-2 rounded-md px-2 py-1.5 text-left text-ui outline-none transition-colors hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring md:min-h-9",
          mark && "font-semibold",
          isSelected && selecting && "bg-primary/10 ring-1 ring-primary",
          "disabled:pointer-events-none disabled:opacity-50",
        )}
        onClick={() => {
          if (selecting && levels) toggleCells(levels);
        }}
      >
        <span className="flex min-w-0 items-center gap-1.5">
          {mark && <Mark />}
          <span className="min-w-0 wrap-anywhere">{content}</span>
        </span>
        {clash && (
          <span
            aria-label={`Warning: ${clash}`}
            title={clash}
            className="inline-flex size-4 shrink-0 items-center justify-center rounded-sm border border-warning/60 text-[11px] font-medium text-warning"
          >
            ?
          </span>
        )}
      </button>
    );

    if (selecting) return button;
    return renderEditor(at, button);
  }

  const selectionCount = (
    <span className="text-sm text-muted-foreground" aria-live="polite">
      {selected.size} selected
    </span>
  );

  return (
    <div className={cn("flex min-w-0 flex-col gap-2", className)} data-testid={testId}>
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="min-h-11 md:min-h-7"
          onClick={() => (selecting ? stopSelecting() : setSelecting(true))}
        >
          {selecting ? "Done selecting" : "Select cells"}
        </Button>
        {selecting && selectionCount}
        {selecting && selectionActions?.(selectedLevels, () => setSelected(new Set()))}
      </div>
      <div className="max-w-full overflow-x-auto rounded-lg border border-border">
        <table aria-label={label} className="w-full min-w-max border-collapse text-left">
          <thead>
            <tr className="border-b border-border bg-muted/30">
              <th className="lt-c sticky left-0 z-20 min-w-40 bg-muted p-1.5 align-middle">
                {editorButton(
                  { level: "corner" },
                  <span>
                    <span className="block">{cornerLabel}</span>
                    {cornerDetail && (
                      <span className="block text-sm font-normal text-muted-foreground">
                        {cornerDetail}
                      </span>
                    )}
                  </span>,
                  selecting ? "Select every available cell" : `Edit ${cornerLabel}`,
                  cornerMark,
                  undefined,
                  availableLevels,
                  "layered-corner",
                )}
              </th>
              {columns.map((col) => (
                <th key={col.id} scope="col" className="min-w-36 p-1.5 align-middle">
                  {editorButton(
                    { level: "col", col: col.id },
                    <span>
                      <span className="block">{col.content ?? col.label}</span>
                      {(col.hostWins || col.detail) && (
                        <span className="mt-0.5 flex flex-wrap items-center gap-1.5 text-sm font-normal text-muted-foreground">
                          {col.hostWins && (
                            <span className="inline-flex items-center gap-1.5 font-semibold text-foreground">
                              <Mark />
                              Host wins
                            </span>
                          )}
                          {col.detail}
                        </span>
                      )}
                    </span>,
                    selecting ? `Select ${col.label} cells` : `Edit ${col.label}`,
                    col.mark,
                    undefined,
                    rows.flatMap((row) =>
                      renderCell(row.id, col.id).unavailable
                        ? []
                        : [{ level: "cell" as const, row: row.id, col: col.id }],
                    ),
                    `layered-col-${col.id}`,
                  )}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.id} className="border-b border-border last:border-b-0">
                <th
                  scope="row"
                  className="lt-c sticky left-0 z-10 min-w-40 bg-card p-1.5 align-middle"
                >
                  {editorButton(
                    { level: "row", row: row.id },
                    <span>
                      <span className="block">{row.content ?? row.label}</span>
                      {row.detail && (
                        <span className="block text-sm font-normal text-muted-foreground">
                          {row.detail}
                        </span>
                      )}
                    </span>,
                    selecting ? `Select ${row.label} cells` : `Edit ${row.label}`,
                    row.mark,
                    undefined,
                    columns.flatMap((col) =>
                      renderCell(row.id, col.id).unavailable
                        ? []
                        : [{ level: "cell" as const, row: row.id, col: col.id }],
                    ),
                    `layered-row-${row.id}`,
                  )}
                </th>
                {columns.map((col) => {
                  const cell = renderCell(row.id, col.id);
                  const at: LayeredCellLevel = { level: "cell", row: row.id, col: col.id };
                  return (
                    <td key={col.id} className="min-w-36 p-1.5 align-middle">
                      {cell.unavailable ? (
                        <span className="flex min-h-11 items-center px-2 text-sm text-muted-foreground md:min-h-9">
                          {cell.unavailable}
                        </span>
                      ) : (
                        editorButton(
                          at,
                          cell.content,
                          selecting
                            ? `Select ${row.label} on ${col.label}`
                            : `Edit ${row.label} on ${col.label}`,
                          cell.mark,
                          cell.clash,
                          [at],
                          `layered-cell-${row.id}-${col.id}`,
                        )
                      )}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
