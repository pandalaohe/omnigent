import { useId } from "react";
import type { ArchiveWorktreeMode } from "@/lib/archiveWorktreePreferences";

const TIERS: readonly [ArchiveWorktreeMode, string, string][] = [
  ["never", "Never delete worktrees", "Keep every worktree directory."],
  [
    "green_only",
    "Delete green worktrees",
    "Remove clean worktrees and their rebuildable local files.",
  ],
  [
    "yellow_too",
    "Delete green and yellow worktrees",
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
    <div className="space-y-3">
      {TIERS.map(([value, label, description]) => (
        <label key={value} className="flex items-start gap-2.5 text-ui">
          <input
            type="radio"
            name={name ?? id}
            value={value}
            checked={mode === value}
            disabled={disabled}
            onChange={() => onChange(value)}
            className="mt-1 accent-primary"
          />
          <span>
            <span className="block font-medium">{label}</span>
            <span className="block text-sm text-muted-foreground">{description}</span>
          </span>
        </label>
      ))}
    </div>
  );
}
