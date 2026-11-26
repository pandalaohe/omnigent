import { useRef, type ReactNode } from "react";

import { cn } from "@/lib/utils";

export interface SegmentedProps<T extends string> {
  "data-settings-row-field"?: string;
  label: string;
  options: readonly {
    value: T;
    label: ReactNode;
    title?: string;
    ariaLabel?: string;
    testId?: string;
  }[];
  value: T;
  onChange: (value: T) => void;
  disabled?: boolean;
  stretch?: boolean;
  /** For a control that qualifies a value beside it, not the value itself. */
  quiet?: boolean;
  /**
   * Fit a fixed-height header without the regular touch target minimum: a
   * 24px track (the header Share button's h-6) with an even 4px inset.
   */
  fitHeight?: boolean;
  className?: string;
}

/** Exclusive short options, with a single tab stop and cycling arrow keys. */
export function Segmented<T extends string>({
  label,
  options,
  value,
  onChange,
  disabled,
  stretch,
  quiet,
  fitHeight,
  className,
  "data-settings-row-field": settingsRowField,
}: SegmentedProps<T>) {
  const buttons = useRef<(HTMLButtonElement | null)[]>([]);
  return (
    <div
      role="radiogroup"
      aria-label={label}
      aria-disabled={disabled}
      data-settings-row-field={settingsRowField}
      className={cn(
        "inline-flex max-w-full rounded-lg bg-muted p-1",
        quiet && "p-0.5",
        stretch && "flex w-full",
        fitHeight && "h-6 p-1",
        className,
      )}
    >
      {options.map((option, index) => (
        <button
          key={option.value}
          ref={(node) => {
            buttons.current[index] = node;
          }}
          type="button"
          role="radio"
          aria-checked={value === option.value}
          disabled={disabled}
          tabIndex={
            value === option.value || (!options.some((item) => item.value === value) && index === 0)
              ? 0
              : -1
          }
          title={option.title}
          aria-label={option.ariaLabel}
          data-testid={option.testId}
          className={cn(
            "inline-flex min-w-11 items-center justify-center gap-1.5 rounded-md px-2 text-ui whitespace-normal wrap-anywhere outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background disabled:cursor-not-allowed disabled:opacity-60 md:px-3",
            fitHeight ? "min-h-0" : "min-h-11 md:min-h-8",
            stretch && "flex-1",
            value === option.value
              ? "bg-primary-cta font-semibold text-primary-cta-foreground"
              : quiet
                ? "text-muted-foreground hover:text-foreground"
                : "text-foreground/90 hover:bg-foreground/10",
          )}
          onClick={() => onChange(option.value)}
          onKeyDown={(event) => {
            if (disabled) return;
            const direction =
              event.key === "ArrowRight" || event.key === "ArrowDown"
                ? 1
                : event.key === "ArrowLeft" || event.key === "ArrowUp"
                  ? -1
                  : 0;
            if (!direction) return;
            event.preventDefault();
            const next = (index + direction + options.length) % options.length;
            onChange(options[next].value);
            buttons.current[next]?.focus();
          }}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}
