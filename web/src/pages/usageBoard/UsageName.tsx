import type { ComponentPropsWithoutRef } from "react";
import { cn } from "@/lib/utils";

type UsageNameProps = Omit<ComponentPropsWithoutRef<"span">, "children" | "title"> & {
  children: string;
};

/** Bound a reported name while retaining its full text for inspection. */
export function UsageName({ children, className, ...props }: UsageNameProps) {
  return (
    <span
      {...props}
      className={cn("block min-w-0 max-w-full truncate", className)}
      title={children}
      data-usage-name
    >
      {children}
    </span>
  );
}
