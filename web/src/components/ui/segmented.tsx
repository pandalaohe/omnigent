import { useRef, type ReactNode } from "react";

import { cn } from "@/lib/utils";

export interface SegmentedProps<T extends string> {
  label: string;
  options: readonly { value: T; label: ReactNode; title?: string; testId?: string }[];
  value: T;
  onChange: (value: T) => void;
  stretch?: boolean;
  className?: string;
}

/** Exclusive short options, with a single tab stop and cycling arrow keys. */
export function Segmented<T extends string>({
  label,
  options,
  value,
  onChange,
  stretch,
  className,
}: SegmentedProps<T>) {
  const buttons = useRef<(HTMLButtonElement | null)[]>([]);
  return (
    <div
      role="radiogroup"
      aria-label={label}
      className={cn(
        "inline-flex max-w-full rounded-lg bg-muted p-1",
        stretch && "flex w-full",
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
          tabIndex={
            value === option.value || (!options.some((item) => item.value === value) && index === 0)
              ? 0
              : -1
          }
          title={option.title}
          data-testid={option.testId}
          className={cn(
            "min-h-11 min-w-11 rounded-md px-2 text-ui whitespace-normal wrap-anywhere outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring md:min-h-8 md:px-3",
            stretch && "flex-1",
            value === option.value
              ? "bg-primary-cta font-medium text-primary-cta-foreground"
              : "text-foreground/90 hover:bg-foreground/10",
          )}
          onClick={() => onChange(option.value)}
          onKeyDown={(event) => {
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
