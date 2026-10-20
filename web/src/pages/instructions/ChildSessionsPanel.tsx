import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useSessionCollabPreferences } from "@/hooks/useSessionCollabPreferences";
import {
  QUOTA_PERIOD_PATTERN,
  QUOTA_PRODUCTS,
  readSessionCollabPreferences,
  writeSessionCollabPreferences,
  type QuotaProduct,
  type SessionCollabPreferences,
} from "@/lib/sessionCollabPreferences";
import { USER_PREFERENCES_PATCH_REJECTED_EVENT } from "@/lib/userPreferencesSync";
import { NATIVE_CODING_AGENTS } from "@/lib/nativeCodingAgents";

interface Draft {
  childCap: string;
  quotaTightBelowPct: string;
  quotaLowBelowPct: string;
  quotaFloorPct: string;
  quotaPeriods: Partial<Record<QuotaProduct, string>>;
}

const PRODUCT_NAMES = Object.fromEntries(
  NATIVE_CODING_AGENTS.map((agent) => [agent.key, agent.displayName]),
);

function draftFromPreferences(preferences: SessionCollabPreferences): Draft {
  return {
    childCap: preferences.childCap?.toString() ?? "",
    quotaTightBelowPct: preferences.quotaTightBelowPct.toString(),
    quotaLowBelowPct: preferences.quotaLowBelowPct.toString(),
    quotaFloorPct: preferences.quotaFloorPct?.toString() ?? "",
    quotaPeriods: Object.fromEntries(
      QUOTA_PRODUCTS.map((product) => [product, preferences.quotaPeriods[product] ?? ""]),
    ),
  };
}

function parsePercentage(text: string): number | null {
  if (!/^\d+(?:\.\d+)?$/.test(text.trim())) return null;
  const value = Number(text.trim());
  return Number.isFinite(value) ? value : null;
}

export function ChildSessionsPanel() {
  const preferences = useSessionCollabPreferences();
  const [draft, setDraft] = useState(() => draftFromPreferences(preferences));
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => setDraft(draftFromPreferences(preferences)), [preferences]);
  useEffect(() => {
    const rejected = (event: Event) => {
      if ((event as CustomEvent<{ namespace: string }>).detail?.namespace === "session_collab") {
        setError("The server rejected these settings. Review the values and save again.");
        setSaved(false);
      }
    };
    window.addEventListener(USER_PREFERENCES_PATCH_REJECTED_EVENT, rejected);
    return () => window.removeEventListener(USER_PREFERENCES_PATCH_REJECTED_EVENT, rejected);
  }, []);

  const update = (field: keyof Omit<Draft, "quotaPeriods">, value: string) => {
    setDraft((current) => ({ ...current, [field]: value }));
    setError(null);
    setSaved(false);
  };

  const save = () => {
    const capText = draft.childCap.trim();
    const cap = capText === "" ? null : Number(capText);
    if (capText !== "" && (!/^[1-9]\d*$/.test(capText) || !Number.isSafeInteger(cap))) {
      setError("Child limit must be a whole number of at least 1, or blank for unlimited.");
      return;
    }
    const tight = parsePercentage(draft.quotaTightBelowPct);
    const low = parsePercentage(draft.quotaLowBelowPct);
    if (low === null || tight === null || low <= 0 || low >= tight || tight > 100) {
      setError("Use percentages where 0 < low < tight ≤ 100.");
      return;
    }
    const floorText = draft.quotaFloorPct.trim();
    const floor = floorText === "" ? null : parsePercentage(floorText);
    if (floorText !== "" && (floor === null || floor < 0 || floor > 100)) {
      setError("Remaining quota floor must be between 0 and 100, or blank.");
      return;
    }
    const periods: SessionCollabPreferences["quotaPeriods"] = {};
    for (const product of QUOTA_PRODUCTS) {
      const period = (draft.quotaPeriods[product] ?? "").trim();
      if (period !== "" && !QUOTA_PERIOD_PATTERN.test(period)) {
        setError("Period names must use 1–32 letters, digits, underscores or hyphens.");
        return;
      }
      if (period) periods[product] = period;
    }
    writeSessionCollabPreferences({
      ...readSessionCollabPreferences(),
      childCap: cap,
      quotaTightBelowPct: tight,
      quotaLowBelowPct: low,
      quotaFloorPct: floor,
      quotaPeriods: periods,
    });
    setError(null);
    setSaved(true);
  };

  return (
    <section
      aria-labelledby="child-sessions-heading"
      className="rounded-xl border border-border bg-card p-4 sm:p-5"
    >
      <h2 id="child-sessions-heading" className="text-lg font-semibold">
        Child sessions
      </h2>
      <p className="mt-1 text-sm text-muted-foreground">
        Set your child-session limit and choose which quota window guides new child sessions.
      </p>
      <p className="mt-1 text-xs text-muted-foreground">
        Pace compares remaining quota per time left with the plan's average rate. These thresholds
        guide agent choices; they do not move running children.
      </p>
      <div className="mt-4 grid gap-4 sm:grid-cols-2">
        <label className="block text-sm font-medium">
          Child session limit
          <Input
            className="mt-1"
            type="text"
            inputMode="numeric"
            placeholder="Unlimited"
            value={draft.childCap}
            onChange={(event) => update("childCap", event.target.value)}
          />
          <span className="mt-1 block text-xs font-normal text-muted-foreground">
            Blank allows unlimited children.
          </span>
        </label>
        <label className="block text-sm font-medium">
          Remaining quota floor (%)
          <Input
            className="mt-1"
            type="text"
            inputMode="decimal"
            placeholder="No floor"
            value={draft.quotaFloorPct}
            onChange={(event) => update("quotaFloorPct", event.target.value)}
          />
          <span className="mt-1 block text-xs font-normal text-muted-foreground">
            Optional absolute minimum remaining.
          </span>
        </label>
        <label className="block text-sm font-medium">
          Tight below (%)
          <Input
            className="mt-1"
            type="text"
            inputMode="decimal"
            value={draft.quotaTightBelowPct}
            onChange={(event) => update("quotaTightBelowPct", event.target.value)}
          />
        </label>
        <label className="block text-sm font-medium">
          Low below (%)
          <Input
            className="mt-1"
            type="text"
            inputMode="decimal"
            value={draft.quotaLowBelowPct}
            onChange={(event) => update("quotaLowBelowPct", event.target.value)}
          />
        </label>
      </div>
      <p className="mt-4 text-sm font-medium">Deciding period by product</p>
      <p className="mt-1 text-xs text-muted-foreground">
        Leave blank to use the source-pinned window, or the tightest window when none is pinned.
        Examples: 5h, 7d, month.
      </p>
      <div className="mt-3 grid gap-4 sm:grid-cols-2">
        {QUOTA_PRODUCTS.map((product) => (
          <label key={product} className="block text-sm font-medium">
            {PRODUCT_NAMES[product]} period
            <Input
              className="mt-1"
              type="text"
              maxLength={32}
              placeholder="Automatic"
              value={draft.quotaPeriods[product] ?? ""}
              onChange={(event) => {
                setDraft((current) => ({
                  ...current,
                  quotaPeriods: { ...current.quotaPeriods, [product]: event.target.value },
                }));
                setError(null);
                setSaved(false);
              }}
            />
          </label>
        ))}
      </div>
      <div className="mt-4 flex flex-wrap items-center gap-3">
        <Button type="button" onClick={save}>
          Save child settings
        </Button>
        {saved && (
          <span role="status" className="text-sm text-muted-foreground">
            Saved preferences.
          </span>
        )}
      </div>
      {error && (
        <p role="alert" className="mt-3 text-sm text-destructive">
          {error}
        </p>
      )}
    </section>
  );
}
