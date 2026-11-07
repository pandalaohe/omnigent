import { useState } from "react";
import { Command } from "cmdk";
import { CheckIcon, SearchIcon, XIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { t } from "./taskBoardText";

export interface BoardSearchOption {
  id: string;
  label: string;
  hint?: string;
}
/** The shared picker retains text on mouse picks and selects it after keyboard picks. */
export function TaskBoardSearch({
  options,
  selected,
  onChange,
  label = t("search.title"),
}: {
  options: BoardSearchOption[];
  selected: string[];
  onChange: (ids: string[]) => void;
  label?: string;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState("");
  const filtered = options.filter((option) =>
    `${option.label} ${option.hint ?? ""}`.toLocaleLowerCase().includes(query.toLocaleLowerCase()),
  );
  const pick = (ids: string[], invert = false) => {
    const next = new Set(selected),
      remove = !invert && ids.every((id) => next.has(id));
    ids.forEach((id) => {
      if (remove || (invert && next.has(id))) next.delete(id);
      else next.add(id);
    });
    onChange([...next]);
  };
  return (
    <div className="tb-search">
      <Button variant="outline" size="sm" onClick={() => setOpen(!open)} aria-expanded={open}>
        <SearchIcon className="size-4" />
        {label}
        {selected.length > 0 && <span>{selected.length}</span>}
      </Button>
      {open && (
        <Command
          label={label}
          shouldFilter={false}
          value={active}
          onValueChange={setActive}
          className="tb-search-panel"
        >
          <Command.Input
            autoFocus
            value={query}
            onValueChange={setQuery}
            placeholder={t("search.placeholder")}
            aria-label={label}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                event.stopPropagation();
                if (event.ctrlKey || event.metaKey) pick(filtered.map((option) => option.id));
                else {
                  const id = filtered.find((option) => option.id === active)?.id ?? filtered[0]?.id;
                  if (id) pick([id]);
                }
                event.currentTarget.select();
              } else if (event.key === "Escape") {
                event.preventDefault();
                event.stopPropagation();
                if (query) setQuery("");
                else setOpen(false);
              }
            }}
          />
          <div className="flex flex-wrap gap-1 border-b border-border px-2 py-1">
            <Button
              variant="ghost"
              size="sm"
              onClick={() => pick(filtered.map((option) => option.id))}
            >
              {t(
                filtered.length && filtered.every((option) => selected.includes(option.id))
                  ? "search.none"
                  : "search.all",
              )}
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={() =>
                pick(
                  filtered.map((option) => option.id),
                  true,
                )
              }
            >
              {t("search.invert")}
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                onChange([]);
                setQuery("");
              }}
            >
              {t("search.reset")}
            </Button>
            <Button
              variant="ghost"
              size="icon-sm"
              className="ml-auto"
              aria-label={t("detail.close")}
              onClick={() => setOpen(false)}
            >
              <XIcon className="size-4" />
            </Button>
          </div>
          <Command.List>
            <Command.Empty>{t("search.empty")}</Command.Empty>
            {filtered.map((option) => (
              <Command.Item
                key={option.id}
                value={option.id}
                onSelect={() => pick([option.id])}
                aria-selected={selected.includes(option.id)}
              >
                <span className="min-w-0 flex-1">
                  <span className="block truncate">{option.label}</span>
                  {option.hint && (
                    <small className="block truncate text-muted-foreground">{option.hint}</small>
                  )}
                </span>
                {selected.includes(option.id) && <CheckIcon className="size-4 shrink-0" />}
              </Command.Item>
            ))}
          </Command.List>
        </Command>
      )}
    </div>
  );
}
