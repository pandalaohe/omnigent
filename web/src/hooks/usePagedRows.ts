import { useState } from "react";
import { SETTINGS_PAGE_SIZE } from "@/lib/settingsPagination";

/** Page the full filtered result, restarting only when the caller changes its scope. */
export function usePagedRows<T>(
  rows: readonly T[],
  { pageSize = SETTINGS_PAGE_SIZE, resetKey }: { pageSize?: number; resetKey?: string } = {},
) {
  const pageCount = Math.ceil(rows.length / pageSize);
  const lastPage = Math.max(0, pageCount - 1);
  const [state, setState] = useState({ resetKey, page: 0 });
  const page = state.resetKey === resetKey ? Math.min(state.page, lastPage) : 0;
  if (state.resetKey !== resetKey || state.page !== page) {
    setState({ resetKey, page });
  }

  return {
    page,
    pageCount,
    pageRows: rows.slice(page * pageSize, (page + 1) * pageSize),
    onPrevious: () => setState((current) => ({ ...current, page: Math.max(0, current.page - 1) })),
    onNext: () =>
      setState((current) => ({ ...current, page: Math.min(lastPage, current.page + 1) })),
  };
}
