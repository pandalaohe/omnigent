import { useCallback, useEffect, useState } from "react";
import { DicesIcon } from "lucide-react";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { isHexColor } from "@/lib/customTheme";
import { cn } from "@/lib/utils";
import {
  SETTINGS_NO_TOOLTIP,
  useSettingsOwnership,
} from "@/pages/settings/search/settingsOwnership";

import { ColorSpectrum, hsvToHex } from "./ColorSpectrum";

function randomColor(): string {
  return hsvToHex({
    hue: Math.random() * 360,
    saturation: 0.55 + Math.random() * 0.35,
    value: 0.7 + Math.random() * 0.25,
  });
}

export function ThemeColorPicker({
  label,
  value,
  testId,
  onChange,
}: {
  label: string;
  value: string;
  testId: string;
  onChange: (value: string) => void;
}) {
  const [open, setOpen] = useState(false);
  // Under a Settings owner the trigger is that part, named by its value; the popover is its overlay.
  const owned = useSettingsOwnership();
  const [draft, setDraft] = useState(value.toUpperCase());

  useEffect(() => setDraft(value.toUpperCase()), [value]);

  const updateDraft = useCallback(
    (next: string) => {
      setDraft(next.toUpperCase());
      const normalized = next.startsWith("#") ? next : `#${next}`;
      if (isHexColor(normalized)) onChange(normalized.toLowerCase());
    },
    [onChange],
  );

  return (
    <div className="flex items-center justify-between gap-4 border-b border-border/70 py-3 last:border-b-0">
      <span className="text-ui font-normal md:font-medium">{label}</span>
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <button
            type="button"
            {...owned.control({
              name: { runtime: value.toUpperCase() },
              ...SETTINGS_NO_TOOLTIP,
              outside: { "aria-label": `${label}: ${value.toUpperCase()}` },
            })}
            data-testid={`${testId}-trigger`}
            className={cn(
              "group flex h-10 w-44 items-center gap-2 rounded-xl border border-input bg-background p-1.5 text-left shadow-xs outline-none transition-[border-color,box-shadow,transform] dark:bg-input",
              "hover:border-border-strong hover:shadow-sm focus-visible:ring-2 focus-visible:ring-ring/40 active:scale-[0.99]",
              open && "border-primary/60 ring-2 ring-primary/15",
            )}
          >
            <span
              aria-hidden
              className="size-7 shrink-0 rounded-lg border border-white/25 shadow-sm ring-1 ring-black/10"
              style={{ backgroundColor: value }}
            />
            <span className="min-w-0 flex-1 font-mono text-sm font-medium tracking-wide">
              {value.toUpperCase()}
            </span>
          </button>
        </PopoverTrigger>
        <PopoverContent
          {...owned.overlay()}
          align="end"
          sideOffset={8}
          className="w-72 gap-0 overflow-hidden rounded-2xl border border-border/70 bg-popover p-0 shadow-xl ring-1 ring-black/5"
        >
          <div className="flex items-center justify-between px-3.5 py-2.5">
            <span className="text-sm font-medium">{label}</span>
            <span className="font-mono text-sm text-muted-foreground">{value.toUpperCase()}</span>
          </div>
          <div className="px-2.5 pb-2.5">
            <ColorSpectrum label={label} value={value} testId={testId} onChange={onChange} />
          </div>
          <div className="border-t border-border/70 bg-muted/20 px-3 py-3">
            <div className="flex items-center gap-2">
              <span
                aria-hidden
                className="size-8 shrink-0 rounded-xl border border-white/25 shadow-sm ring-1 ring-black/10"
                style={{ backgroundColor: value }}
              />
              <input
                type="text"
                value={draft}
                aria-label={`${label} hex value`}
                data-testid={`${testId}-input`}
                maxLength={7}
                spellCheck={false}
                onChange={(event) => updateDraft(event.target.value)}
                onBlur={() => setDraft(value.toUpperCase())}
                className="h-9 min-w-0 flex-1 rounded-xl border bg-background px-3 font-mono text-sm font-medium uppercase tracking-wide outline-none transition-shadow focus:ring-2 focus:ring-ring/40"
              />
              <button
                type="button"
                aria-label={`Randomize ${label.toLowerCase()}`}
                title={`Randomize ${label.toLowerCase()}`}
                data-testid={`${testId}-randomize`}
                onClick={() => onChange(randomColor())}
                className="group/randomize flex size-9 shrink-0 items-center justify-center rounded-xl border bg-background text-muted-foreground outline-none transition-[background-color,border-color,color,transform] hover:border-primary/40 hover:bg-primary/10 hover:text-primary focus-visible:ring-2 focus-visible:ring-ring/40 active:scale-95"
              >
                <DicesIcon className="size-4 transition-transform duration-300 group-hover/randomize:-rotate-12 group-active/randomize:rotate-90" />
              </button>
            </div>
          </div>
        </PopoverContent>
      </Popover>
    </div>
  );
}
