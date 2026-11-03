export type LayeredLevel =
  | { level: "corner" }
  | { level: "row"; row: string }
  | { level: "col"; col: string }
  | { level: "cell"; row: string; col: string };

export interface LayeredStore<F extends string> {
  corner: Partial<Record<F, unknown>>;
  rows: Record<string, Partial<Record<F, unknown>>>;
  cols: Record<string, Partial<Record<F, unknown>>>;
  cells: Record<string, Record<string, Partial<Record<F, unknown>>>>;
  hostWins?: Record<string, true>;
}

export interface LayeredOptions<F extends string> {
  canHold?: (row: string, field: F, value: unknown) => boolean;
  normalize?: (row: string, field: F, value: unknown) => unknown;
  colFields?: readonly F[];
}

export interface ResolvedField {
  value: unknown;
  stored: unknown;
  source: LayeredLevel | null;
}

interface Candidate {
  stored: unknown;
  source: LayeredLevel;
}

function ownCandidate<F extends string>(
  record: Partial<Record<F, unknown>> | undefined,
  row: string,
  field: F,
  source: LayeredLevel,
  options: LayeredOptions<F>,
): Candidate | null {
  if (!record || !Object.hasOwn(record, field) || record[field] === undefined) return null;
  const stored = record[field];
  if (options.canHold && !options.canHold(row, field, stored)) return null;
  return { stored, source };
}

function cornerCandidate<F extends string>(
  store: LayeredStore<F>,
  row: string,
  field: F,
  options: LayeredOptions<F>,
): Candidate | null {
  if (options.colFields && !options.colFields.includes(field)) return null;
  return ownCandidate(store.corner, row, field, { level: "corner" }, options);
}

function resolve<F extends string>(
  store: LayeredStore<F>,
  row: string,
  col: string,
  field: F,
  options: LayeredOptions<F>,
  includeCell: boolean,
): ResolvedField {
  const candidates: (Candidate | null)[] = [];
  if (includeCell) {
    candidates.push(
      ownCandidate(store.cells[col]?.[row], row, field, { level: "cell", row, col }, options),
    );
  }

  const colAllowed = !options.colFields || options.colFields.includes(field);
  const column = colAllowed
    ? ownCandidate(store.cols[col], row, field, { level: "col", col }, options)
    : null;
  if (store.hostWins?.[col]) candidates.push(column);

  candidates.push(ownCandidate(store.rows[row], row, field, { level: "row", row }, options));
  if (!store.hostWins?.[col]) candidates.push(column);
  candidates.push(cornerCandidate(store, row, field, options));

  const winner = candidates.find((candidate) => candidate !== null);
  if (winner) {
    return {
      value: options.normalize ? options.normalize(row, field, winner.stored) : winner.stored,
      stored: winner.stored,
      source: winner.source,
    };
  }
  return {
    value: options.normalize ? options.normalize(row, field, undefined) : undefined,
    stored: undefined,
    source: null,
  };
}

/** Resolve one cell field in runtime order, returning both stored and normalized values. */
export function resolveField<F extends string>(
  store: LayeredStore<F>,
  row: string,
  col: string,
  field: F,
  options: LayeredOptions<F> = {},
): ResolvedField {
  return resolve(store, row, col, field, options, true);
}

/** Resolve the value a level would inherit without its own field. */
export function inheritedAt<F extends string>(
  store: LayeredStore<F>,
  at: LayeredLevel,
  field: F,
  options: LayeredOptions<F> = {},
): { value: unknown; source: LayeredLevel | null } {
  if (at.level === "cell") {
    const { value, source } = resolve(store, at.row, at.col, field, options, false);
    return { value, source };
  }
  if (at.level === "corner") {
    return {
      value: options.normalize ? options.normalize("", field, undefined) : undefined,
      source: null,
    };
  }

  const row = at.level === "row" ? at.row : "";
  const candidate = cornerCandidate(store, row, field, options);
  if (!candidate) {
    return {
      value: options.normalize ? options.normalize(row, field, undefined) : undefined,
      source: null,
    };
  }
  return {
    value: options.normalize ? options.normalize(row, field, candidate.stored) : candidate.stored,
    source: candidate.source,
  };
}

/** Find fields whose two headers disagree while no valid cell value settles them. */
export function crossingClashes<F extends string>(
  store: LayeredStore<F>,
  row: string,
  col: string,
  fields: readonly F[],
  options: LayeredOptions<F> = {},
): { field: F; winner: "row" | "col" }[] {
  const clashes: { field: F; winner: "row" | "col" }[] = [];
  for (const field of fields) {
    const cell = ownCandidate(
      store.cells[col]?.[row],
      row,
      field,
      { level: "cell", row, col },
      options,
    );
    if (cell) continue;

    const rowValue = ownCandidate(store.rows[row], row, field, { level: "row", row }, options);
    const colValue =
      !options.colFields || options.colFields.includes(field)
        ? ownCandidate(store.cols[col], row, field, { level: "col", col }, options)
        : null;
    if (!rowValue || !colValue) continue;

    const normalizedRow = options.normalize
      ? options.normalize(row, field, rowValue.stored)
      : rowValue.stored;
    const normalizedCol = options.normalize
      ? options.normalize(row, field, colValue.stored)
      : colValue.stored;
    if (!Object.is(normalizedRow, normalizedCol)) {
      clashes.push({ field, winner: store.hostWins?.[col] ? "col" : "row" });
    }
  }
  return clashes;
}

function holdsValue(record: Partial<Record<string, unknown>> | undefined): boolean {
  return !!record && Object.values(record).some((value) => value !== undefined);
}

/** List lower levels that a Clear action must visit. */
export function levelsBelow<F extends string>(
  store: LayeredStore<F>,
  at: LayeredLevel,
): LayeredLevel[] {
  if (at.level === "cell") return [];
  if (at.level === "row") {
    return Object.entries(store.cells).flatMap(([col, rows]) =>
      holdsValue(rows[at.row]) ? [{ level: "cell" as const, row: at.row, col }] : [],
    );
  }
  if (at.level === "col") {
    return Object.entries(store.cells[at.col] ?? {}).flatMap(([row, record]) =>
      holdsValue(record) ? [{ level: "cell" as const, row, col: at.col }] : [],
    );
  }

  const levels: LayeredLevel[] = [];
  for (const [row, record] of Object.entries(store.rows)) {
    if (holdsValue(record)) levels.push({ level: "row", row });
  }
  for (const [col, record] of Object.entries(store.cols)) {
    if (holdsValue(record) || store.hostWins?.[col]) levels.push({ level: "col", col });
  }
  for (const [col, rows] of Object.entries(store.cells)) {
    for (const [row, record] of Object.entries(rows)) {
      if (holdsValue(record)) levels.push({ level: "cell", row, col });
    }
  }
  return levels;
}

/** Shared unavailable-cell wording for every layered table. */
export function missingCellText(
  harness: string | null,
  platform: string | null | undefined,
): "Not on Windows" | "Not installed here" {
  const native =
    harness === "native" || harness?.includes("-native") || harness?.startsWith("native-");
  return native && platform === "win32" ? "Not on Windows" : "Not installed here";
}
