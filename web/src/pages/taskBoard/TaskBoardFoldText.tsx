import { useState } from "react";
import { t } from "./taskBoardText";

export function TaskBoardFoldText({ value }: { value: string }) {
  const [expanded, setExpanded] = useState(false);
  const long = value.length > 180 || value.split("\n").length > 3;
  return (
    <span className="tb-fold-text">
      <span className={`tb-fold-value${long && !expanded ? " collapsed" : ""}`}>{value}</span>
      {long && (
        <button type="button" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>
          {t(expanded ? "detail.showLess" : "detail.showMore")}
        </button>
      )}
    </span>
  );
}
