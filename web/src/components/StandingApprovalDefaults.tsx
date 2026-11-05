import { useId, useState } from "react";
import { Button } from "@/components/ui/button";
import { HelpTip } from "@/components/HelpTip";
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
  { key: "answer-child-approvals", label: "Answer child approvals" },
];

export function StandingApprovalDefaults() {
  const actionId = useId();
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
    <div className="space-y-3">
      <div className="flex items-center gap-1.5 text-ui font-medium">
        Standing approval
        <HelpTip label="About standing approval" className="size-11 md:size-4">
          Omnigent adds the selected actions to a new parent session&apos;s instructions as plain
          text, and its children read them. The agent decides by that text: it answers no Claude or
          Codex permission prompt and no policy. A project can remove an action, and a parent can
          lose or gain one for itself. Changes affect new sessions only.
        </HelpTip>
      </div>
      <p className="text-sm text-muted-foreground">
        Selected: the parent goes ahead. Others: it asks first.
      </p>
      <div className="flex flex-wrap gap-2">
        {KINDS.map(({ key, label }) => (
          <Button
            key={key}
            type="button"
            size="sm"
            variant={kinds.includes(key) ? "secondary" : "outline"}
            aria-pressed={kinds.includes(key)}
            disabled={!kinds.includes(key) && kinds.length >= 32}
            onClick={() =>
              update(kinds.includes(key) ? kinds.filter((kind) => kind !== key) : [...kinds, key])
            }
          >
            {label}
          </Button>
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
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5">
            <label htmlFor={actionId} className="text-ui font-medium">
              Another action
            </label>
            <HelpTip label="About another action" className="size-11 md:size-4">
              <table className="w-full text-left">
                <thead>
                  <tr>
                    <th>Write</th>
                    <th>The agent reads</th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <td className="pr-2 align-top">run local checks</td>
                    <td>Go ahead with local checks.</td>
                  </tr>
                  <tr>
                    <td className="pr-2 align-top">update docs</td>
                    <td>Go ahead with documentation updates.</td>
                  </tr>
                </tbody>
              </table>
              <p className="mt-2">
                Use an action your agent understands, up to 120 characters. Select at most 32
                actions.
              </p>
            </HelpTip>
          </div>
          <Input
            id={actionId}
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
            placeholder="e.g. run local checks"
          />
        </div>
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
