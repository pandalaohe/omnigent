import { TriangleAlertIcon } from "lucide-react";
import { useEffect, useState, type ReactElement, type ReactNode } from "react";

import { HelpTip } from "@/components/HelpTip";
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
  /** The line beside the tools, left of the table. */
  note?: ReactNode;
  /** Page actions, placed before `Select cells`. */
  tools?: ReactNode;
  /** Entries the page adds after the shared dot legend. */
  legend?: ReactNode;
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

/** The one mark for a value set at this level; `LayeredOwn` and the legend use it. */
export function LayeredMark() {
  return <span aria-hidden="true" className="size-1.5 shrink-0 rounded-full bg-pink-500" />;
}

/** A value that differs from what its level would inherit. */
export function LayeredOwn({ children }: { children: ReactNode }) {
  return (
    <span className="inline-flex min-w-0 items-center gap-1.5 font-semibold text-foreground">
      <LayeredMark />
      <span className="min-w-0 wrap-anywhere">{children}</span>
    </span>
  );
}

function LevelState({ children, mark }: { children: ReactNode; mark?: boolean }) {
  return (
    <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5 text-sm font-normal text-muted-foreground">
      {mark ? <LayeredOwn>{children}</LayeredOwn> : children}
    </span>
  );
}

const LEVEL_CELL = "h-px border-b border-border bg-background p-0 text-left align-top font-normal";
const FIRST_COLUMN = "sticky left-0 min-w-34 max-w-34 border-r md:min-w-50 md:max-w-none";
const BODY_COLUMN = "min-w-37.5";
const LEVEL_NAME = "block min-w-0 text-ui font-medium text-foreground";

/** Field-neutral layered grid; pages own values, editors and bulk mutations. */
export function LayeredTable({
  label,
  cornerLabel,
  cornerDetail,
  cornerMark,
  note,
  tools,
  legend,
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
  const availableLevels: LayeredCellLevel[] = rows.flatMap((row) =>
    columns.flatMap((col) =>
      renderCell(row.id, col.id).unavailable
        ? []
        : [{ level: "cell" as const, row: row.id, col: col.id }],
    ),
  );
  const availableKeys = new Set(availableLevels.map(({ row, col }) => selectionKey(row, col)));
  const selectedLevels: LayeredCellLevel[] = [...selected]
    .filter((key) => availableKeys.has(key))
    .map((key) => {
      const [row, col] = JSON.parse(key) as [string, string];
      return { level: "cell", row, col };
    });

  useEffect(() => {
    const available = new Set(availableLevels.map(({ row, col }) => selectionKey(row, col)));
    setSelected((current) => {
      const next = new Set([...current].filter((key) => available.has(key)));
      return next.size === current.size ? current : next;
    });
  }, [availableLevels]);

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
          "lt-b flex size-full min-h-11 min-w-0 flex-col items-start justify-center gap-0.5 px-2.5 py-1.75 text-left outline-none transition-colors hover:bg-muted aria-expanded:bg-muted focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring",
          clash && "pr-11 md:pr-8",
          isSelected && selecting && "bg-primary/20 ring-1 ring-inset ring-primary",
          "disabled:pointer-events-none disabled:opacity-50",
        )}
        onClick={() => {
          if (selecting && levels) toggleCells(levels);
        }}
      >
        {content}
      </button>
    );

    if (selecting) return button;
    return renderEditor(at, button);
  }

  return (
    <div className={cn("flex min-w-0 flex-col gap-2", className)} data-testid={testId}>
      <div className="flex min-w-0 flex-wrap items-center justify-between gap-x-3 gap-y-2">
        <div className="flex min-w-0 flex-wrap items-center gap-1.5 text-sm text-muted-foreground">
          {note}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {tools}
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="min-h-11 md:min-h-7"
            onClick={() => (selecting ? stopSelecting() : setSelecting(true))}
          >
            {selecting ? "Done selecting" : "Select cells"}
          </Button>
        </div>
      </div>
      {selecting && (
        <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1.5 rounded-lg border border-border bg-muted px-2.5 py-1.5 text-sm">
          <span className="font-semibold text-foreground" aria-live="polite">
            {selectedLevels.length} {selectedLevels.length === 1 ? "cell" : "cells"} selected
          </span>
          <span className="text-muted-foreground">
            Click cells, a header for its line, or the corner for all.
          </span>
          <span className="ml-auto flex flex-wrap items-center gap-1.5">
            {selectionActions?.(selectedLevels, () => setSelected(new Set()))}
          </span>
        </div>
      )}
      <div className="max-w-full overflow-x-auto rounded-lg border border-border">
        <table
          aria-label={label}
          className="w-full min-w-max border-separate border-spacing-0 text-sm"
        >
          <thead>
            <tr>
              <th className={cn("lt-c z-20", LEVEL_CELL, FIRST_COLUMN)}>
                {editorButton(
                  { level: "corner" },
                  <>
                    <span className={LEVEL_NAME}>{cornerLabel}</span>
                    {cornerDetail && <LevelState mark={cornerMark}>{cornerDetail}</LevelState>}
                  </>,
                  selecting ? "Select every available cell" : `Edit ${cornerLabel}`,
                  undefined,
                  availableLevels,
                  "layered-corner",
                )}
              </th>
              {columns.map((col) => (
                <th key={col.id} scope="col" className={cn(LEVEL_CELL, BODY_COLUMN)}>
                  {editorButton(
                    { level: "col", col: col.id },
                    <>
                      <span className={LEVEL_NAME}>{col.content ?? col.label}</span>
                      {(col.hostWins || col.detail) && (
                        <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5">
                          {col.hostWins && <LayeredOwn>Host wins</LayeredOwn>}
                          {col.detail && <LevelState mark={col.mark}>{col.detail}</LevelState>}
                        </span>
                      )}
                    </>,
                    selecting ? `Select ${col.label} cells` : `Edit ${col.label}`,
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
              <tr key={row.id} className="[&:last-child>*]:border-b-0">
                <th scope="row" className={cn("lt-c z-10", LEVEL_CELL, FIRST_COLUMN)}>
                  {editorButton(
                    { level: "row", row: row.id },
                    <>
                      <span className={LEVEL_NAME}>{row.content ?? row.label}</span>
                      {row.detail && <LevelState mark={row.mark}>{row.detail}</LevelState>}
                    </>,
                    selecting ? `Select ${row.label} cells` : `Edit ${row.label}`,
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
                    <td key={col.id} className={cn("relative", LEVEL_CELL, BODY_COLUMN)}>
                      {cell.unavailable ? (
                        <span className="flex size-full min-h-11 items-center px-2.5 py-1.75 text-muted-foreground">
                          {cell.unavailable}
                        </span>
                      ) : (
                        <>
                          {editorButton(
                            at,
                            <LevelState mark={cell.mark}>{cell.content}</LevelState>,
                            selecting
                              ? `Select ${row.label} on ${col.label}`
                              : `Edit ${row.label} on ${col.label}`,
                            cell.clash,
                            [at],
                            `layered-cell-${row.id}-${col.id}`,
                          )}
                          {cell.clash && (
                            <HelpTip
                              label={`Warning: ${cell.clash}`}
                              icon={<TriangleAlertIcon className="size-3.5" />}
                              className="absolute top-0 right-0 size-11 text-warning hover:text-warning md:top-2 md:right-2 md:size-4"
                            >
                              <span className="block whitespace-pre-line">{cell.clash}</span>
                              {!selecting && (
                                <span className="mt-2 block text-muted-foreground">
                                  Click this cell to set it here.
                                </span>
                              )}
                            </HelpTip>
                          )}
                        </>
                      )}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-muted-foreground">
        <LayeredOwn>Differs from the level above</LayeredOwn>
        <span>No dot: inherited</span>
        {legend}
      </p>
    </div>
  );
}
