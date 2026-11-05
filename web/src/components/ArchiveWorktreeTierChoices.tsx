import { useId } from "react";
import { AlertTriangleIcon } from "lucide-react";
import { HelpTip } from "@/components/HelpTip";
import { cn } from "@/lib/utils";
import type { ArchiveWorktreeMode } from "@/lib/archiveWorktreePreferences";

const TIERS: readonly [ArchiveWorktreeMode, string, string][] = [
  ["never", "Keep all", "Keep every worktree directory."],
  ["green_only", "Remove clean", "Remove clean worktrees and their rebuildable local files."],
  [
    "yellow_too",
    "Remove clean and ignored",
    "Also remove other ignored local files. These files will be lost.",
  ],
];

export function ArchiveWorktreeTierChoices({
  mode,
  onChange,
  disabled = false,
  name,
}: {
  mode: ArchiveWorktreeMode;
  onChange: (mode: ArchiveWorktreeMode) => void;
  disabled?: boolean;
  name?: string;
}) {
  const id = useId();
  return (
    <div className="flex flex-wrap gap-2" role="radiogroup" aria-label="Worktrees on archive">
      {TIERS.map(([value, label, description]) => (
        <div
          key={value}
          className={cn(
            "flex min-h-11 cursor-pointer items-center gap-2 rounded-lg border px-3 py-2 text-ui md:min-h-9",
            mode === value ? "border-primary bg-primary/5" : "border-border",
            value === "yellow_too" && "text-destructive",
            disabled && "cursor-not-allowed opacity-60",
          )}
        >
          <input
            id={`${id}-${value}`}
            type="radio"
            name={name ?? id}
            value={value}
            checked={mode === value}
            disabled={disabled}
            onChange={() => onChange(value)}
            className="accent-primary"
          />
          {value === "yellow_too" && (
            <AlertTriangleIcon aria-hidden="true" className="size-4 shrink-0" />
          )}
          <label htmlFor={`${id}-${value}`} className="cursor-inherit">
            {label}
          </label>
          <HelpTip label={`About ${label}`}>{description}</HelpTip>
        </div>
      ))}
    </div>
  );
}
