import { useState } from "react";
import { UsersIcon, XIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";

export function CompactMembership({
  label,
  items,
  onRemove,
  disabled = false,
}: {
  label: string;
  items: readonly { id: string; label: string }[];
  onRemove?: (id: string) => void;
  disabled?: boolean;
}) {
  const [query, setQuery] = useState("");
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const visible = items.filter((item) => item.label.toLocaleLowerCase().includes(normalizedQuery));

  if (items.length === 0) {
    return (
      <span
        aria-label={`${label}: 0`}
        className="inline-flex min-w-5 justify-center text-muted-foreground"
      >
        —
      </span>
    );
  }

  const preview = items.slice(0, 3);
  return (
    <Popover>
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <PopoverTrigger asChild>
              <Button
                type="button"
                size="sm"
                variant="secondary"
                disabled={disabled}
                aria-label={`${label}: ${items.length}`}
              >
                <UsersIcon className="size-4" aria-hidden="true" />
                <span className="tabular-nums">{items.length}</span>
              </Button>
            </PopoverTrigger>
          </TooltipTrigger>
          <TooltipContent className="max-w-72">
            <div className="space-y-1">
              <p className="font-medium">
                {label} · {items.length}
              </p>
              {preview.map((item) => (
                <p key={item.id} className="truncate text-xs">
                  {item.label}
                </p>
              ))}
              {items.length > preview.length && (
                <p className="text-xs opacity-75">and {items.length - preview.length} more</p>
              )}
            </div>
          </TooltipContent>
        </Tooltip>
      </TooltipProvider>
      <PopoverContent
        align="start"
        onKeyDown={(event) => {
          if (["ArrowDown", "ArrowUp", "Enter", " "].includes(event.key)) event.stopPropagation();
        }}
      >
        <p className="font-medium">{label}</p>
        <Input
          aria-label={`Search ${label}`}
          placeholder={`Search ${label}`}
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
        <p className="text-xs text-muted-foreground">
          Showing {visible.length} of {items.length}
        </p>
        <div className="max-h-64 overflow-auto">
          {visible.map((item) => (
            <div key={item.id} className="flex min-w-0 items-center justify-between gap-2 py-1">
              <span className="min-w-0 break-all">{item.label}</span>
              {onRemove && (
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  disabled={disabled}
                  aria-label={`Remove ${item.label}`}
                  onClick={() => onRemove(item.id)}
                >
                  <XIcon className="size-4" aria-hidden="true" />
                </Button>
              )}
            </div>
          ))}
          {visible.length === 0 && (
            <p className="py-2 text-sm text-muted-foreground">No matches.</p>
          )}
        </div>
      </PopoverContent>
    </Popover>
  );
}
