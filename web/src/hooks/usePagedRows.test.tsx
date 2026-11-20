import { act, renderHook } from "@testing-library/react";
import { expect, it } from "vitest";
import { usePagedRows } from "./usePagedRows";

const rows = Array.from({ length: 45 }, (_, index) => index + 1);

it("clamps after a deletion, stays on the page after a refetch, and resets for a new search", () => {
  const { result, rerender } = renderHook(
    ({ data, filter }) => usePagedRows(data, { resetKey: filter }),
    { initialProps: { data: rows, filter: "" } },
  );
  const next = () => act(() => result.current.onNext());
  next();
  next();
  expect(result.current.pageRows).toEqual([41, 42, 43, 44, 45]);

  rerender({ data: [...rows], filter: "" });
  expect(result.current.page).toBe(2);

  const shortened = rows.slice(0, 40);
  rerender({ data: shortened, filter: "" });
  expect(result.current.page).toBe(1);
  expect(result.current.pageRows).toEqual(rows.slice(20, 40));

  rerender({ data: rows.slice(14), filter: "filtered" });
  expect(result.current.page).toBe(0);
  expect(result.current.pageRows).toEqual(rows.slice(14, 34));
  next();
  rerender({ data: rows.slice(14).reverse(), filter: "filtered" });
  expect(result.current.page).toBe(1);
  expect(result.current.pageRows[0]).toBe(25);
});
