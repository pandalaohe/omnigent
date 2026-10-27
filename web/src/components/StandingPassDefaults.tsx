import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useSessionCollabPreferences } from "@/hooks/useSessionCollabPreferences";
import {
  readSessionCollabPreferences,
  writeSessionCollabPreferences,
} from "@/lib/sessionCollabPreferences";

const KINDS = [
  { key: "install", label: "Install" },
  { key: "merge", label: "Merge" },
  { key: "release", label: "Release" },
];

export function StandingPassDefaults() {
  const preferences = useSessionCollabPreferences();
  const [extra, setExtra] = useState("");
  const [error, setError] = useState<string | null>(null);
  const kinds = preferences.standingPassDefault;
  const update = (next: string[]) => {
    writeSessionCollabPreferences({ ...readSessionCollabPreferences(), standingPassDefault: next });
    setError(null);
  };
  const add = () => {
    const kind = extra.trim().replace(/\s+/g, " ");
    if (!kind || [...extra].length > 120) {
      setError("Enter a kind with 1–120 characters.");
      return;
    }
    if (!kinds.includes(kind) && kinds.length >= 32) {
      setError("Use at most 32 kinds.");
      return;
    }
    update([...new Set([...kinds, kind])]);
    setExtra("");
  };
  return (
    <div className="mt-5 space-y-3 border-t border-border pt-4">
      <h3 className="text-sm font-medium">Default standing pass</h3>
      <p className="text-sm text-muted-foreground">
        Allow these kinds of actions for new parent sessions and their children. Project exceptions
        and actions that always require approval still apply. Changes affect new sessions only.
      </p>
      <div className="flex flex-wrap gap-x-6 gap-y-3">
        {KINDS.map(({ key, label }) => (
          <label key={key} className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={kinds.includes(key)}
              disabled={!kinds.includes(key) && kinds.length >= 32}
              onChange={(event) =>
                update(
                  event.target.checked ? [...kinds, key] : kinds.filter((kind) => kind !== key),
                )
              }
              className="size-4 accent-primary"
            />
            {label}
          </label>
        ))}
      </div>
      {kinds
        .filter((kind) => !KINDS.some((entry) => entry.key === kind))
        .map((kind) => (
          <div
            key={kind}
            className="flex min-w-0 items-center justify-between gap-2 rounded-md bg-muted px-3 py-2 text-sm"
          >
            <span className="min-w-0 break-words">{kind}</span>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              aria-label={`Remove ${kind}`}
              onClick={() => update(kinds.filter((value) => value !== kind))}
            >
              Remove
            </Button>
          </div>
        ))}
      <div className="flex flex-wrap items-end gap-2">
        <label className="min-w-0 flex-1 text-sm font-medium">
          Extra standing pass kind
          <Input
            className="mt-1"
            value={extra}
            maxLength={120}
            onChange={(event) => setExtra(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                add();
              }
            }}
            placeholder="Enter another kind"
          />
        </label>
        <Button type="button" variant="outline" onClick={add}>
          Add kind
        </Button>
      </div>
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
    </div>
  );
}
