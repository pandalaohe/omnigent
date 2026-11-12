import { useState, useRef, useEffect, type CSSProperties } from "react";
import { XIcon } from "lucide-react";
import { t } from "./taskBoardText";
import { routeState, type MapGroup } from "./taskBoardRouteModel";

export function TaskBoardRouteFocus({
  groups,
  selected,
  onChange,
  sourceName,
  sourceColor,
  onClose,
}: {
  groups: MapGroup[];
  selected: string[];
  onChange: (ids: string[]) => void;
  sourceName: (source: string) => string;
  sourceColor: (source: string) => string;
  onClose: () => void;
}) {
  const [query, setQuery] = useState(""),
    [active, setActive] = useState(0),
    [page, setPage] = useState(0),
    input = useRef<HTMLInputElement>(null);
  useEffect(() => {
    input.current?.focus();
  }, []);
  const open = (group: MapGroup) =>
    group.tasks.filter(
      (task) =>
        task["omnigent.ai:sourceDisplay"]?.historyState?.active !== false &&
        routeState(task) !== "done",
    ).length;
  const sources = [...new Set(groups.map((group) => group.source))]
    .map((source) => ({
      source,
      rows: groups
        .filter(
          (group) =>
            group.source === source &&
            (group.name + group.badge + source).toLowerCase().includes(query.trim().toLowerCase()),
        )
        .sort((a, b) => open(b) - open(a)),
    }))
    .filter((source) => source.rows.length);
  const visible = sources.flatMap((source) => source.rows.map((group) => group.id));
  const toggle = (ids: string[]) => {
    const held = new Set(selected),
      all = ids.every((id) => held.has(id));
    ids.forEach((id) => (all ? held.delete(id) : held.add(id)));
    onChange([...held]);
  };
  const bullet = (color: string, label: string) => (
    <i
      className={`rt-bullet${label.length === 1 ? " round" : ""}`}
      style={{ "--rt-color": color } as CSSProperties}
    >
      {label}
    </i>
  );
  return (
    <section className="rt-focus" aria-label={t("routes.focusTitle")}>
      <header>
        <b>{t("routes.focusTitle")}</b>
        <input
          ref={input}
          value={query}
          placeholder={t("routes.focusPlaceholder")}
          onChange={(event) => {
            setQuery(event.target.value);
            setActive(0);
            setPage(0);
          }}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              if (event.ctrlKey || event.metaKey) toggle(visible);
              else if (visible[active]) toggle([visible[active]]);
              event.currentTarget.select();
            }
            if (event.key === "ArrowDown" || event.key === "ArrowUp") {
              event.preventDefault();
              if (visible.length)
                setActive(
                  (active + (event.key === "ArrowDown" ? 1 : -1) + visible.length) % visible.length,
                );
            }
            if (event.key === "Escape") {
              event.preventDefault();
              event.stopPropagation();
              if (query) {
                setQuery("");
                setActive(0);
                setPage(0);
              } else onClose();
            }
          }}
        />
        <button type="button" onClick={() => toggle(visible)}>
          {t(
            visible.length && visible.every((id) => selected.includes(id))
              ? "search.none"
              : "search.all",
          )}
        </button>
        <button
          type="button"
          onClick={() => {
            const held = new Set(selected);
            visible.forEach((id) => (held.has(id) ? held.delete(id) : held.add(id)));
            onChange([...held]);
          }}
        >
          {t("search.invert")}
        </button>
        <button
          type="button"
          onClick={() => {
            setQuery("");
            setActive(0);
            onChange([]);
          }}
        >
          {t("routes.focusReset")}
        </button>
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
        {sources.slice(page * 12, (page + 1) * 12).map(({ source, rows }) => (
          <section key={source}>
            <h6>
              <label>
                <input
                  type="checkbox"
                  checked={rows.every((group) => selected.includes(group.id))}
                  onChange={() => toggle(rows.map((group) => group.id))}
                />
                {bullet(sourceColor(source), sourceName(source).slice(0, 1).toUpperCase())}
                <b>{sourceName(source)}</b>
              </label>
            </h6>
            {rows.map((group) => (
              <label className={visible[active] === group.id ? "active" : ""} key={group.id}>
                <input
                  type="checkbox"
                  checked={selected.includes(group.id)}
                  onChange={() => toggle([group.id])}
                />
                {bullet(group.color, group.badge)}
                <span>{group.name}</span>
                {open(group) > 0 && <em>{open(group)}</em>}
              </label>
            ))}
          </section>
        ))}
        {sources.length > 12 && (
          <nav>
            <button type="button" disabled={!page} onClick={() => setPage(page - 1)}>
              {t("pager.previous")}
            </button>
            <span>
              {t("pager.page", { page: page + 1, pages: Math.ceil(sources.length / 12) })}
            </span>
            <button
              type="button"
              disabled={(page + 1) * 12 >= sources.length}
              onClick={() => setPage(page + 1)}
            >
              {t("pager.next")}
            </button>
          </nav>
        )}
      </div>
      <footer>
        {t(selected.length ? "routes.focusSelected" : "routes.focusAll", {
          count: selected.length,
        })}
      </footer>
    </section>
  );
}
