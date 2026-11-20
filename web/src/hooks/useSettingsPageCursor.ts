import { useCallback, useState } from "react";

export function useSettingsPageCursor() {
  const [cursors, setCursors] = useState<(string | undefined)[]>([undefined]);
  const cursor = cursors.at(-1);
  const page = cursors.length - 1;
  const previous = useCallback(() => {
    setCursors((current) => (current.length > 1 ? current.slice(0, -1) : current));
  }, []);
  const next = (nextCursor: string | null | undefined) => {
    if (nextCursor) {
      setCursors((current) => (current.at(-1) === nextCursor ? current : [...current, nextCursor]));
    }
  };
  const restart = useCallback(() => setCursors([undefined]), []);
  return { cursor, page, previous, next, restart };
}
