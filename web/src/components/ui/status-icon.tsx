import type { ReactNode } from "react";

import { cn } from "@/lib/utils";
import { Tooltip, TooltipContent, TooltipTrigger } from "./tooltip";

const tones = {
  ok: "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400",
  warn: "bg-amber-500/10 text-amber-600 dark:text-amber-400",
  bad: "bg-red-500/10 text-red-600 dark:text-red-400",
  neutral: "bg-muted text-muted-foreground",
};

export function StatusIcon({
  tone,
  icon,
  label,
  focusable = true,
}: {
  tone: keyof typeof tones;
  icon: ReactNode;
  label: string;
  focusable?: boolean;
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          role="img"
          aria-label={label}
          tabIndex={focusable ? 0 : undefined}
          className={cn(
            "inline-flex size-[22px] shrink-0 items-center justify-center rounded-full outline-offset-2 [&_svg]:size-3.5",
            tones[tone],
          )}
        >
          <span aria-hidden="true">{icon}</span>
        </span>
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  );
}
