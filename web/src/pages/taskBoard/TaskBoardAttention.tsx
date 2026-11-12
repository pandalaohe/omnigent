import { Fragment, useState, type ReactNode } from "react";
import { Link } from "@/lib/routing";
import { boardExitProps } from "@/lib/boardFrame";
import { BoardPager } from "./TaskBoardRoutes";
import type { BoardTask } from "./taskBoardData";
import type { AttentionRow } from "./boardAttentionData";
import { BoardIdentityMark } from "./BoardIdentityMark";

export function TaskBoardAttention({
  rows,
  onSelect,
  inlineDetail,
}: {
  inlineDetail?: ReactNode;
  rows: AttentionRow[];
  onSelect: (task: BoardTask) => void;
}) {
  const [detailRow, setDetailRow] = useState<string | null>(null);
  const [page, setPage] = useState(0),
    pages = Math.max(1, Math.ceil(rows.length / 8)),
    current = Math.min(page, pages - 1);
  return (
    <>
      <ul>
        {rows.slice(current * 8, (current + 1) * 8).map((row) => (
          <Fragment key={row.id}>
            <li>
              {row.task ? (
                <button
                  type="button"
                  onClick={() => {
                    setDetailRow(row.id);
                    onSelect(row.task!);
                  }}
                >
                  {row.session && (
                    <BoardIdentityMark
                      id={row.session.agentId}
                      label={row.session.label}
                      product={row.session.agentId}
                      size={26}
                    />
                  )}
                  <span>
                    <b>{row.text}</b>
                    <small>{row.caption}</small>
                  </span>
                </button>
              ) : (
                <Link {...boardExitProps()} to={`/c/${encodeURIComponent(row.session!.id)}`}>
                  <BoardIdentityMark
                    id={row.session!.agentId}
                    label={row.session!.label}
                    product={row.session!.agentId}
                    size={26}
                  />
                  <span>
                    <b>{row.text}</b>
                    <small>{row.caption}</small>
                  </span>
                </Link>
              )}
            </li>
            {detailRow === row.id && inlineDetail && (
              <li className="tb-inline-row">{inlineDetail}</li>
            )}
          </Fragment>
        ))}
      </ul>
      <BoardPager page={current} pages={pages} onChange={setPage} />
    </>
  );
}
