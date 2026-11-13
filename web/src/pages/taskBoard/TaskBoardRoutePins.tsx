import { useEffect, useRef, useState, type CSSProperties } from "react";
import { XIcon } from "lucide-react";
import { taskIdentity, type BoardTask } from "./taskBoardData";
import { taskTitle } from "./TaskBoardDetail";
import type { MapGroup } from "./taskBoardRouteModel";
import type { RoutePins } from "./taskBoardRouteView";
import { t } from "./taskBoardText";

interface Choice {
  kind: "groups" | "tasks";
  id: string;
  label: string;
  hint: string;
  color: string;
  badge: string;
}
/** Search a task or group and pin it into the attention view; pinned rows unpin in place. */
export function TaskBoardRoutePins({
  groups,
  pins,
  onChange,
  sourceName,
  onClose,
}: {
  groups: MapGroup[];
  pins: RoutePins;
  onChange: (pins: RoutePins) => void;
  sourceName: (source: string) => string;
  onClose: () => void;
}) {
  const [query, setQuery] = useState(""),
    input = useRef<HTMLInputElement>(null);
  useEffect(() => {
    input.current?.focus();
  }, []);
  const groupChoice = (group: MapGroup): Choice => ({
    kind: "groups",
    id: group.id,
    label: group.name || t("group.default"),
    hint: sourceName(group.source),
    color: group.color,
    badge: group.badge,
  });
  const taskChoice = (task: BoardTask, group: MapGroup): Choice => ({
    kind: "tasks",
    id: taskIdentity(task),
    label: taskTitle(task),
    hint: [task["omnigent.ai:key"], group.name].filter(Boolean).join(" · "),
    color: group.color,
    badge: group.badge,
  });
  const needle = query.trim().toLowerCase();
  const pinned: Choice[] = [],
    found: Choice[] = [];
  for (const group of groups) {
    const choice = groupChoice(group);
    if (pins.groups.includes(group.id)) pinned.push(choice);
    else if (needle && (choice.label + choice.badge + choice.hint).toLowerCase().includes(needle))
      found.push(choice);
    for (const task of group.tasks) {
      const item = taskChoice(task, group);
      if (pins.tasks.includes(item.id)) pinned.push(item);
      else if (needle && (item.label + item.hint).toLowerCase().includes(needle)) found.push(item);
    }
  }
  const shown = found.slice(0, 24);
  const toggle = (choice: Choice) => {
    const list = pins[choice.kind];
    onChange({
      ...pins,
      [choice.kind]: list.includes(choice.id)
        ? list.filter((id) => id !== choice.id)
        : [...list, choice.id],
    });
  };
  const row = (choice: Choice, on: boolean) => (
    <label key={choice.kind + choice.id}>
      <input type="checkbox" checked={on} onChange={() => toggle(choice)} />
      <i className="rt-bullet" style={{ "--rt-color": choice.color } as CSSProperties}>
        {choice.badge}
      </i>
      <span>
        {choice.label}
        <small>{choice.hint}</small>
      </span>
    </label>
  );
  return (
    <section className="rt-focus rt-pins" aria-label={t("routes.pinsTitle")}>
      <header>
        <b>{t("routes.pinsTitle")}</b>
        <input
          ref={input}
          value={query}
          placeholder={t("routes.pinsPlaceholder")}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && shown[0]) {
              event.preventDefault();
              toggle(shown[0]);
            }
            if (event.key === "Escape") {
              event.preventDefault();
              event.stopPropagation();
              if (query) setQuery("");
              else onClose();
            }
          }}
        />
        <button
          type="button"
          className="rt-focus-close"
          aria-label={t("detail.close")}
          onClick={onClose}
        >
          <XIcon size={14} />
        </button>
      </header>
      <div className="rt-focus-list">
        {pinned.length > 0 && (
          <section>
            <h6>{t("routes.pinned", { count: pinned.length })}</h6>
            {pinned.map((choice) => row(choice, true))}
          </section>
        )}
        {needle && (
          <section>
            {shown.map((choice) => row(choice, false))}
            {!shown.length && <p>{t("routes.pinsEmpty")}</p>}
            {found.length > shown.length && (
              <p>{t("routes.pinsMore", { count: found.length - shown.length })}</p>
            )}
          </section>
        )}
      </div>
      <footer>{t("routes.pinsHint")}</footer>
    </section>
  );
}
