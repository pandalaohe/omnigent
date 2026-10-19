import { useState } from "react";
import { ChevronsUpDownIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import {
  Command,
  CommandEmpty,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";

export function RoutePicker({
  label,
  items,
  value,
  onChange,
}: {
  label: string;
  items: { value: string; label: string }[];
  value: string;
  onChange: (value: string) => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="min-w-0 flex-1">
      <span className="mb-1 block text-xs font-medium text-muted-foreground">{label}</span>
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <Button
            variant="outline"
            role="combobox"
            aria-label={label}
            aria-expanded={open}
            className="w-full justify-between"
          >
            <span className="truncate">
              {items.find((item) => item.value === value)?.label ??
                (value || `Select ${label.toLowerCase()}`)}
            </span>
            <ChevronsUpDownIcon className="shrink-0 size-3.5" />
          </Button>
        </PopoverTrigger>
        <PopoverContent className="w-[min(22rem,90vw)] p-0" align="start">
          <Command>
            <CommandInput placeholder={`Search ${label.toLowerCase()}…`} />
            <CommandList>
              <CommandEmpty>No matches.</CommandEmpty>
              {items.map((item) => (
                <CommandItem
                  key={item.value}
                  value={`${item.label} ${item.value}`}
                  onSelect={() => {
                    onChange(item.value);
                    setOpen(false);
                  }}
                >
                  {item.label}
                </CommandItem>
              ))}
            </CommandList>
          </Command>
        </PopoverContent>
      </Popover>
    </div>
  );
}
