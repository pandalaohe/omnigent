import { useRef } from "react";

import { DropdownMenuItem } from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";

export interface LevelMeterProps<T extends string> {
  label: string;
  options: readonly { value: T; label: string; testId?: string }[];
  value: T | null;
  inherited?: T | null;
  unsetLabel?: string;
  onChange: (value: T | null) => void;
  disabled?: boolean;
  stretch?: boolean;
  className?: string;
}

function LevelBlock({ filled, inherited }: { filled: boolean; inherited: boolean }) {
  return (
    <span
      aria-hidden="true"
      className={cn(
        "h-3 w-full rounded-sm",
        filled ? (inherited ? "bg-companion/45" : "bg-companion") : "bg-muted-foreground/20",
      )}
    />
  );
}

/** Compact, non-interactive amount indicator for a model chip. */
export function LevelGlyph({
  level,
  total,
  title,
}: {
  level: number;
  total: number;
  title?: string;
}) {
  return (
    <span
      role={title ? "img" : undefined}
      aria-label={title}
      aria-hidden={title ? undefined : true}
      title={title}
      className="inline-flex items-end gap-0.5"
    >
      {Array.from({ length: total }, (_, index) => (
        <span
          key={index}
          className={cn(
            "h-2 w-1 rounded-xs",
            index < level ? "bg-companion" : "bg-muted-foreground/20",
          )}
        />
      ))}
    </span>
  );
}

export function LevelMeter<T extends string>({
  label,
  options,
  value,
  inherited,
  unsetLabel = "Default",
  onChange,
  disabled,
  stretch,
  className,
}: LevelMeterProps<T>) {
  const buttons = useRef<(HTMLButtonElement | null)[]>([]);
  const selected = options.findIndex((option) => option.value === value);
  const shown = options.findIndex((option) => option.value === (value ?? inherited));
  const focusIndex = Math.max(0, shown);
  return (
    <div
      className={cn(
        "flex min-w-0 max-w-full flex-wrap items-center gap-x-3 gap-y-1",
        stretch && "w-full",
        className,
      )}
    >
      {/* The blocks own one full-width line at every width: seven 44 px phone
          targets do not fit beside the level name, and a line that sizes itself
          to its content cannot shrink inside a narrow menu. */}
      <div
        role="radiogroup"
        aria-label={label}
        aria-disabled={disabled}
        className="flex min-w-0 basis-full flex-wrap gap-0.5"
      >
        {options.map((option, index) => (
          <button
            key={option.value}
            ref={(node) => {
              buttons.current[index] = node;
            }}
            type="button"
            role="radio"
            aria-label={option.label}
            aria-checked={selected === index}
            data-testid={option.testId}
            data-effort-level={option.value}
            disabled={disabled}
            tabIndex={index === focusIndex ? 0 : -1}
            className="flex min-h-11 min-w-11 flex-1 items-center rounded-md px-1 outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50 md:min-h-7 md:min-w-0"
            onClick={() => onChange(option.value)}
            onKeyDown={(event) => {
              if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
              event.preventDefault();
              const next = Math.max(
                0,
                Math.min(options.length - 1, index + (event.key === "ArrowRight" ? 1 : -1)),
              );
              onChange(options[next].value);
              buttons.current[next]?.focus();
            }}
          >
            <LevelBlock filled={index <= shown} inherited={value === null} />
          </button>
        ))}
      </div>
      <span className="text-ui" aria-live="polite">
        {selected < 0 ? unsetLabel : options[selected].label}
      </span>
      {value !== null && (
        <button
          type="button"
          disabled={disabled}
          aria-label={`Reset ${label}`}
          className="min-h-11 min-w-11 rounded-md px-2 text-sm text-muted-foreground outline-none hover:bg-foreground/10 focus-visible:ring-2 focus-visible:ring-ring md:min-h-7"
          onClick={() => {
            onChange(null);
            const defaultIndex = options.findIndex((option) => option.value === inherited);
            buttons.current[Math.max(0, defaultIndex)]?.focus();
          }}
        >
          Reset {label}
        </button>
      )}
    </div>
  );
}

/** A single menu focus stop; Up/Down remain owned by Radix. */
export function MenuLevelRow<T extends string>({
  label,
  options,
  value,
  inherited,
  unsetLabel = "Default",
  onChange,
  onUnset,
  disabled,
  className,
}: Omit<LevelMeterProps<T>, "stretch"> & { onUnset?: () => void }) {
  const selected = options.findIndex((option) => option.value === value);
  const shown = options.findIndex((option) => option.value === (value ?? inherited));
  const row = useRef<HTMLDivElement | null>(null);
  return (
    <DropdownMenuItem
      disabled={disabled}
      className={cn("flex min-w-0 max-w-full flex-wrap gap-x-3 gap-y-1 whitespace-normal", className)}
      onSelect={(event) => event.preventDefault()}
      onKeyDown={(event) => {
        if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
        event.preventDefault();
        event.stopPropagation();
        if (disabled || !options.length) return;
        if (event.key === "ArrowLeft" && selected === 0 && onUnset) {
          onUnset();
          return;
        }
        const next = Math.max(
          0,
          Math.min(
            options.length - 1,
            (selected < 0 ? shown : selected) + (event.key === "ArrowRight" ? 1 : -1),
          ),
        );
        onChange(options[next].value);
      }}
    >
      <div
        ref={row}
        className="flex w-full min-w-0 flex-wrap items-center gap-x-3 gap-y-1"
        aria-label={label}
      >
        <div className="flex min-w-0 basis-full flex-wrap gap-0.5">
          {options.map((option, index) => (
            <button
              key={option.value}
              type="button"
              tabIndex={-1}
              aria-label={`${label}: ${option.label}`}
              aria-pressed={selected === index}
              data-testid={option.testId}
              data-effort-level={option.value}
              disabled={disabled}
              className="flex min-h-11 min-w-11 flex-1 items-center rounded-md px-1 md:min-h-7 md:min-w-0"
              onClick={(event) => {
                event.stopPropagation();
                onChange(option.value);
                row.current?.closest<HTMLElement>('[role="menuitem"]')?.focus();
              }}
            >
              <LevelBlock filled={index <= shown} inherited={value === null} />
            </button>
          ))}
        </div>
        <span className="text-ui">
          {label}: {selected < 0 ? unsetLabel : options[selected].label}
        </span>
        {value !== null && onUnset && (
          <button
            type="button"
            disabled={disabled}
            tabIndex={-1}
            aria-label={`Reset ${label}`}
            className="min-h-11 min-w-11 rounded-md px-2 text-sm text-muted-foreground hover:bg-foreground/10 md:min-h-7"
            onClick={(event) => {
              event.stopPropagation();
              onUnset();
              row.current?.closest<HTMLElement>('[role="menuitem"]')?.focus();
            }}
          >
            Reset {label}
          </button>
        )}
      </div>
    </DropdownMenuItem>
  );
}
