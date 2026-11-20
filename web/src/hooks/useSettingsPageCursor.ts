import { useCallback, useState } from "react";

export function useSettingsPageCursor(initialCursors: readonly string[] = []) {
  const [cursors, setCursors] = useState(initialCursors);
  const cursor = cursors.at(-1);
  const page = cursors.length;
  const previous = useCallback(() => {
    setCursors((current) => (current.length ? current.slice(0, -1) : current));
  }, []);
  const next = (nextCursor: string | null | undefined) => {
    if (nextCursor) {
      setCursors((current) => (current.at(-1) === nextCursor ? current : [...current, nextCursor]));
    }
  };
  const restart = useCallback(() => setCursors([]), []);
  return { cursor, cursors, page, previous, next, restart };
}
