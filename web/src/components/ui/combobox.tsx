import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from "react";
import { useCommandState } from "cmdk";

import { Command, CommandItem, CommandList } from "./command";
import { Input } from "./input";
import { Popover, PopoverAnchor, PopoverContent } from "./popover";

export const COMBOBOX_OPTION_THRESHOLD = 5;

export interface ComboboxOption {
  key: string;
  label: string;
  description?: string;
  icon?: ReactNode;
  disabled?: boolean;
}

interface ComboboxProps {
  mode: "editable" | "select";
  options: readonly ComboboxOption[];
  value: string;
  onValueChange: (value: string) => void;
  onOpenChange?: (open: boolean) => void;
  onPick?: (option: ComboboxOption) => void;
  placeholder?: string;
  "aria-label": string;
  emptyText?: (typed: string) => string;
  maxRendered?: number;
  autoOpen?: boolean;
  className?: string;
}

function ActiveOption({
  listId,
  onChange,
}: {
  listId?: string;
  onChange: (id: string | undefined) => void;
}) {
  const value = useCommandState((state) => state.value);
  useEffect(() => {
    onChange(
      listId
        ? document.getElementById(listId)?.querySelector('[cmdk-item][aria-selected="true"]')?.id
        : undefined,
    );
  }, [value, listId, onChange]);
  return null;
}

export function Combobox({
  mode,
  options,
  value,
  onValueChange,
  onOpenChange,
  onPick,
  placeholder,
  "aria-label": label,
  emptyText,
  maxRendered = 50,
  autoOpen = false,
  className,
}: ComboboxProps) {
  const selectedLabel =
    mode === "editable" ? value : (options.find((o) => o.key === value)?.label ?? "");
  const [typed, setTyped] = useState(selectedLabel);
  const [open, setOpen] = useState(autoOpen);
  const reportedOpen = useRef(false);
  useEffect(() => {
    if (reportedOpen.current !== open) {
      reportedOpen.current = open;
      onOpenChange?.(open);
    }
  }, [open, onOpenChange]);
  const [active, setActive] = useState("");
  const [activeId, setActiveId] = useState<string>();
  const input = useRef<HTMLInputElement>(null);
  const original = useRef(value);
  const editing = useRef(false);
  const [listId, setListId] = useState<string>();
  const bindList = useCallback((node: HTMLDivElement | null) => {
    setListId(node?.querySelector("[cmdk-list]")?.id);
  }, []);
  const customKey = useId();

  useEffect(() => {
    if (!editing.current) setTyped(selectedLabel);
  }, [selectedLabel]);
  useEffect(() => {
    if (autoOpen) input.current?.focus();
  }, [autoOpen]);

  const query = typed.toLocaleLowerCase();
  const matches = options
    .filter((o) => `${o.label} ${o.description ?? ""}`.toLocaleLowerCase().includes(query))
    .sort(
      (a, b) =>
        Number(b.label.toLocaleLowerCase().startsWith(query)) -
        Number(a.label.toLocaleLowerCase().startsWith(query)),
    );
  const visible = matches.slice(0, Math.max(1, maxRendered));
  const custom =
    mode === "editable" &&
    typed.trim() !== "" &&
    !options.some((o) => o.label.toLocaleLowerCase() === query);

  function finish(next: string, option?: ComboboxOption) {
    editing.current = false;
    setTyped(option?.label ?? next);
    onValueChange(next);
    if (option) onPick?.(option);
    setOpen(false);
  }

  function dismiss() {
    if (!editing.current) return;
    if (mode === "editable") finish(typed);
    else {
      editing.current = false;
      setTyped(selectedLabel);
      setOpen(false);
    }
  }

  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        if (!next) dismiss();
        setOpen(next);
      }}
    >
      <Command shouldFilter={false} value={active} onValueChange={setActive} className={className}>
        <ActiveOption listId={open ? listId : undefined} onChange={setActiveId} />
        <PopoverAnchor asChild>
          <Input
            ref={input}
            role="combobox"
            aria-label={label}
            aria-autocomplete="list"
            aria-expanded={open}
            aria-controls={open ? listId : undefined}
            aria-activedescendant={open ? activeId : undefined}
            placeholder={placeholder}
            value={typed}
            onFocus={() => {
              original.current = value;
              editing.current = true;
            }}
            onClick={() => setOpen(true)}
            onChange={(event) => {
              editing.current = true;
              setTyped(event.target.value);
              setActive("");
              setOpen(true);
            }}
            onBlur={dismiss}
            onKeyDown={(event) => {
              if (event.nativeEvent.isComposing) {
                event.stopPropagation();
                return;
              }
              if (event.key === "Escape") {
                event.preventDefault();
                event.stopPropagation();
                if (open) {
                  setOpen(false);
                  if (mode === "select") {
                    setTyped(selectedLabel);
                    editing.current = false;
                  }
                } else if (mode === "editable") finish(original.current);
              } else if ((event.key === "ArrowDown" || event.key === "ArrowUp") && !open) {
                event.preventDefault();
                event.stopPropagation();
                setOpen(true);
              } else if (event.key === "Enter" && (!open || !active)) {
                event.preventDefault();
                event.stopPropagation();
                if (mode === "editable") finish(typed);
              }
            }}
          />
        </PopoverAnchor>
        <PopoverContent
          align="start"
          className="w-[var(--radix-popover-trigger-width)] min-w-48 gap-0 p-0"
          onOpenAutoFocus={(event) => event.preventDefault()}
          onCloseAutoFocus={(event) => event.preventDefault()}
          onInteractOutside={(event) => {
            if (event.target === input.current) event.preventDefault();
          }}
          onEscapeKeyDown={(event) => event.preventDefault()}
          onMouseDown={(event) => event.preventDefault()}
        >
          <div ref={bindList}>
            <CommandList>
              {visible.map((option) => (
                <CommandItem
                  key={option.key}
                  value={option.key}
                  disabled={option.disabled}
                  onSelect={() => finish(mode === "editable" ? option.label : option.key, option)}
                >
                  {option.icon}
                  <span className="truncate">{option.label}</span>
                  {option.description && (
                    <span className="truncate text-muted-foreground">{option.description}</span>
                  )}
                </CommandItem>
              ))}
              {custom && (
                <CommandItem value={customKey} onSelect={() => finish(typed)}>
                  Use &quot;{typed}&quot; as typed
                </CommandItem>
              )}
              {matches.length === 0 && !custom && (
                <div className="p-2 text-muted-foreground">
                  {emptyText?.(typed) ?? "No matches"}
                </div>
              )}
            </CommandList>
          </div>
          {matches.length > visible.length && (
            <div className="border-t px-2 py-1 text-muted-foreground">
              Showing {visible.length} of {matches.length} — keep typing
            </div>
          )}
        </PopoverContent>
      </Command>
    </Popover>
  );
}
