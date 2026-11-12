import { useState } from "react";
import { Link } from "@/lib/routing";
import { boardExitProps } from "@/lib/boardFrame";
import { BoardPager } from "./TaskBoardRoutes";
import type { BoardTask } from "./taskBoardData";
import type { AttentionRow } from "./boardAttentionData";

export function TaskBoardAttention({
  rows,
  onSelect,
}: {
  rows: AttentionRow[];
  onSelect: (task: BoardTask) => void;
}) {
  const [page, setPage] = useState(0),
    pages = Math.max(1, Math.ceil(rows.length / 8)),
    current = Math.min(page, pages - 1);
  return (
    <>
      <ul>
        {rows.slice(current * 8, (current + 1) * 8).map((row) => (
          <li key={row.id}>
            {row.task ? (
              <button type="button" onClick={() => onSelect(row.task!)}>
                <span>
                  <b>{row.text}</b>
                  <small>{row.caption}</small>
                </span>
              </button>
            ) : (
              <Link {...boardExitProps()} to={`/c/${encodeURIComponent(row.session!.id)}`}>
                <span>
                  <b>{row.text}</b>
                  <small>{row.caption}</small>
                </span>
              </Link>
            )}
          </li>
        ))}
      </ul>
      <BoardPager page={current} pages={pages} onChange={setPage} />
    </>
  );
}
