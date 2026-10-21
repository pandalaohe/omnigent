import { useCallback, useEffect, useState } from "react";
import { ExceptionList } from "@/components/ExceptionList";
import { HelpTip } from "@/components/HelpTip";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useAgentRegistry, type RegistryItem } from "@/hooks/useAgentRegistry";
import { useSessionCollabPreferences } from "@/hooks/useSessionCollabPreferences";
import { useHarnessCatalog, type HarnessCapability } from "@/lib/agentLabels";
import {
  QUOTA_PERIOD_PATTERN,
  readSessionCollabPreferences,
  writeSessionCollabPreferences,
  type SessionCollabPreferences,
} from "@/lib/sessionCollabPreferences";
import { USER_PREFERENCES_PATCH_REJECTED_EVENT } from "@/lib/userPreferencesSync";
import { nativeCodingAgentForHarness } from "@/lib/nativeCodingAgents";

interface Draft {
  childCap: string;
  quotaTightBelowPct: string;
  quotaLowBelowPct: string;
  quotaFloorPct: string;
  quotaPeriodDefault: string;
  quotaPeriods: Record<string, { period: string }>;
}

const EMPTY_CAPABILITIES: Record<string, HarnessCapability> = {};

function draftFromPreferences(preferences: SessionCollabPreferences): Draft {
  return {
    childCap: preferences.childCap?.toString() ?? "",
    quotaTightBelowPct: preferences.quotaTightBelowPct.toString(),
    quotaLowBelowPct: preferences.quotaLowBelowPct.toString(),
    quotaFloorPct: preferences.quotaFloorPct?.toString() ?? "",
    quotaPeriodDefault: preferences.quotaPeriodDefault ?? "",
    quotaPeriods: Object.fromEntries(
      Object.entries(preferences.quotaPeriods).map(([product, period]) => [product, { period }]),
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
  const registry = useAgentRegistry();
  const capabilities = useHarnessCatalog(
    (catalog) => catalog.capabilitiesByHarness,
    EMPTY_CAPABILITIES,
  );
  const [draft, setDraft] = useState(() => draftFromPreferences(preferences));
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const productKeys = new Set<string>();
  const quotaItems = registry.items.map((item): RegistryItem => {
    const harness = item.harness;
    if (!harness || item.key !== `harness:${harness}`) return item;
    // Older servers have no capability map; retain their known native products.
    const product = capabilities[harness]
      ? capabilities[harness].native_agent_key
      : nativeCodingAgentForHarness(harness)?.key;
    if (!product) return item;
    productKeys.add(product);
    return { ...item, key: product };
  });

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

  const renderPeriodEditor = useCallback(
    (row: { period: string }, patch: (next: Partial<{ period: string }>) => void, key: string) => (
      <label className="block text-sm font-medium">
        {quotaItems.find((item) => item.key === key)?.label ?? key} period
        <Input
          className="mt-1"
          type="text"
          maxLength={32}
          placeholder="e.g. 5h"
          value={row.period}
          onChange={(event) => patch({ period: event.target.value })}
        />
      </label>
    ),
    [quotaItems],
  );

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
    const defaultPeriod = draft.quotaPeriodDefault.trim();
    if (defaultPeriod && !QUOTA_PERIOD_PATTERN.test(defaultPeriod)) {
      setError("Period names must use 1–32 letters, digits, underscores or hyphens.");
      return;
    }
    const periods: SessionCollabPreferences["quotaPeriods"] = {};
    for (const [product, row] of Object.entries(draft.quotaPeriods)) {
      const period = row.period.trim();
      if (period === "") {
        setError("Enter a period for each exception, or remove it.");
        return;
      }
      if (!QUOTA_PERIOD_PATTERN.test(period)) {
        setError("Period names must use 1–32 letters, digits, underscores or hyphens.");
        return;
      }
      periods[product] = period;
    }
    writeSessionCollabPreferences({
      ...readSessionCollabPreferences(),
      childCap: cap,
      quotaTightBelowPct: tight,
      quotaLowBelowPct: low,
      quotaFloorPct: floor,
      quotaPeriodDefault: defaultPeriod || null,
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
      <div className="flex items-center gap-1.5">
        <h2 id="child-sessions-heading" className="text-lg font-semibold">
          Child sessions
        </h2>
        <HelpTip label="About child-session quota">
          Pace compares remaining quota per time left with the plan's average rate. These thresholds
          guide agent choices; they do not move running children.
        </HelpTip>
      </div>
      <p className="mt-1 text-sm text-muted-foreground">
        Set your child-session limit and choose which quota window guides new child sessions.
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
      <div className="mt-5 rounded-lg border border-border p-3">
        <div className="flex items-center gap-1.5 text-sm font-medium">
          Deciding period
          <HelpTip label="About deciding periods">
            Automatic uses the source-pinned window, or the tightest window when none is pinned.
            Period names can be 5h, 7d or month. An exception takes precedence for its product.
          </HelpTip>
        </div>
        <label className="mt-3 block text-sm font-medium">
          All products
          <Input
            className="mt-1"
            type="text"
            maxLength={32}
            placeholder="Automatic"
            value={draft.quotaPeriodDefault}
            onChange={(event) => update("quotaPeriodDefault", event.target.value)}
          />
        </label>
        <div className="mt-4 border-t border-border pt-3">
          <ExceptionList
            rows={draft.quotaPeriods}
            items={quotaItems}
            summary={(row) => row.period || "Choose a period"}
            editor={renderPeriodEditor}
            supports={(item) =>
              productKeys.has(item.key) ? null : "Quota periods apply only to native products"
            }
            onAdd={(key) => {
              setDraft((current) => ({
                ...current,
                quotaPeriods: { ...current.quotaPeriods, [key]: { period: "" } },
              }));
              setSaved(false);
            }}
            onPatch={(key, patch) => {
              setDraft((current) => ({
                ...current,
                quotaPeriods: {
                  ...current.quotaPeriods,
                  [key]: { ...current.quotaPeriods[key], ...patch },
                },
              }));
              setError(null);
              setSaved(false);
            }}
            onRemove={(key) => {
              setDraft((current) => ({
                ...current,
                quotaPeriods: Object.fromEntries(
                  Object.entries(current.quotaPeriods).filter(([product]) => product !== key),
                ),
              }));
              setSaved(false);
            }}
            isLoading={registry.isLoading}
            isError={registry.isError}
            onRetry={registry.retry}
          />
        </div>
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
