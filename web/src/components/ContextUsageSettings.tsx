import { useEffect, useState } from "react";

import { useCodexRateLimits, useHosts } from "@/hooks/useHosts";
import { useSession } from "@/hooks/useSession";
import { useUsageContextPreferences } from "@/hooks/useUsageContextPreferences";
import { useStableProviderUsageLimits } from "@/hooks/useStableProviderUsageLimits";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { BRAIN_HARNESS_LABELS } from "@/lib/agentLabels";
import { formatTokenCountShort } from "@/lib/formatCost";
import {
  formatProviderUsageLimits,
  providerUsageLimitsFromCodex,
  type ProviderUsageLimitsSnapshot,
} from "@/lib/providerUsageLimits";
import {
  usageContextOverrideFor,
  usageContextSourceFromKey,
  usageContextSourceKey,
  writeUsageContextOverride,
  writeUsageContextPreferences,
} from "@/lib/usageContextPreferences";
import { useChatStore } from "@/store/chatStore";

function optionalNumber(value: string): number | null {
  if (value.trim() === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

function SourceValue({ label, value }: { label: string; value: string }) {
  return (
    <span className="min-w-0">
      <span className="text-muted-foreground">{label}</span>{" "}
      <span className="font-medium text-foreground">{value}</span>
    </span>
  );
}

export function ContextUsageSettings() {
  const preferences = useUsageContextPreferences();
  const conversationId = useChatStore((state) => state.conversationId);
  const agentName = useChatStore((state) => state.boundAgentName);
  const harness = useChatStore((state) => state.sessionHarness);
  const model = useChatStore((state) => state.llmModel);
  const sessionProviderUsageLimits = useChatStore((state) => state.providerUsageLimits);
  const { session } = useSession(conversationId);
  const { data: hosts = [] } = useHosts();
  const sourceKey = usageContextSourceKey({ hostId: session?.hostId, agentName, harness, model });
  const activeOverride = usageContextOverrideFor(preferences, sourceKey);
  const [contextWindowDraft, setContextWindowDraft] = useState(
    activeOverride.contextWindowTokens?.toString() ?? "",
  );
  const [thresholdDraft, setThresholdDraft] = useState(
    activeOverride.autoCompactThresholdPercent?.toString() ?? "",
  );
  const reportedContextWindow = useChatStore((state) => state.contextWindow);
  const reportedCompactLimit = useChatStore((state) => state.autoCompactTokenLimit);
  const codexSession = harness === "codex" || harness === "codex-native";
  const { data: codexRateLimits, isLoading: rateLimitsLoading } = useCodexRateLimits(
    session?.hostId ?? null,
    codexSession && session?.hostId != null,
  );

  useEffect(() => {
    setContextWindowDraft(activeOverride.contextWindowTokens?.toString() ?? "");
    setThresholdDraft(activeOverride.autoCompactThresholdPercent?.toString() ?? "");
  }, [activeOverride.contextWindowTokens, activeOverride.autoCompactThresholdPercent, sourceKey]);

  const writeOverride = (patch: Partial<typeof activeOverride>) =>
    writeUsageContextOverride(preferences, sourceKey, { ...activeOverride, ...patch });
  const commitContextWindow = () => {
    const next = optionalNumber(contextWindowDraft);
    writeOverride({ contextWindowTokens: next === null ? null : Math.round(next) });
  };
  const commitThreshold = () => {
    const next = optionalNumber(thresholdDraft);
    writeOverride({
      autoCompactThresholdPercent:
        next !== null && next >= 1 && next <= 100 ? Math.round(next * 10) / 10 : null,
    });
  };

  const effectiveContextWindow = optionalNumber(contextWindowDraft) ?? reportedContextWindow;
  const effectiveThreshold = optionalNumber(thresholdDraft);
  const calculatedCompactPoint =
    effectiveContextWindow != null && effectiveThreshold != null && effectiveThreshold <= 100
      ? Math.round((effectiveContextWindow * effectiveThreshold) / 100)
      : reportedCompactLimit;
  const hostName = session?.hostId
    ? (hosts.find((host) => host.host_id === session.hostId)?.name ?? "Unknown computer")
    : "Server";
  const harnessName = harness ? (BRAIN_HARNESS_LABELS[harness] ?? harness) : "Unknown";
  const providerLimits: ProviderUsageLimitsSnapshot | null = useStableProviderUsageLimits({
    preferences,
    sourceKey,
    fresh: codexSession
      ? providerUsageLimitsFromCodex(codexRateLimits, model)
      : sessionProviderUsageLimits,
    agentName,
    harness,
  });
  const savedOverrideCount = Object.keys(preferences.overrides).length;
  const savedSources = Object.entries(preferences.overrides).flatMap(([key, override]) => {
    const source = usageContextSourceFromKey(key);
    return source ? [{ key, source, override }] : [];
  });
  const currentProviderLimits = formatProviderUsageLimits(providerLimits) ? providerLimits : null;
  const providerStatusDetail = currentProviderLimits
    ? `${currentProviderLimits.provider} usage limits for this session.`
    : rateLimitsLoading
      ? "Checking usage limits."
      : harness
        ? `No usage limits received from ${harnessName} for this session.`
        : "Open a session to view usage limits.";
  const providerStatus = currentProviderLimits
    ? `${currentProviderLimits.provider} · ${currentProviderLimits.windows.length}`
    : rateLimitsLoading
      ? "Checking…"
      : harness
        ? "No data"
        : "Open a session";

  return (
    <div className="grid gap-6">
      <div className="grid gap-3">
        <div>
          <h3 className="text-sm font-medium text-foreground">Current source</h3>
          <p
            className="mt-1 text-sm text-muted-foreground"
            title="Overrides are saved for this exact computer, agent, harness, and model. Reported values alone do not create a saved override."
          >
            Overrides are remembered when all four fields match.
          </p>
        </div>
        <div className="grid gap-2 rounded-xl border border-border bg-muted/20 p-3 text-sm sm:grid-cols-2">
          <SourceValue label="Computer" value={hostName} />
          <SourceValue label="Agent" value={agentName || "Unknown"} />
          <SourceValue label="Harness" value={harnessName} />
          <SourceValue label="Model" value={model || "Auto"} />
        </div>
        <div className="grid gap-4 sm:grid-cols-2">
          <label className="grid gap-1.5 text-sm">
            Context total (tokens)
            <Input
              type="number"
              min={1}
              step={1000}
              value={contextWindowDraft}
              placeholder={reportedContextWindow?.toString() ?? "Auto"}
              onChange={(event) => setContextWindowDraft(event.target.value)}
              onBlur={commitContextWindow}
              onKeyDown={(event) => event.key === "Enter" && event.currentTarget.blur()}
              aria-label="Context window override in tokens"
            />
          </label>
          <label className="grid gap-1.5 text-sm">
            Compact at (%)
            <Input
              type="number"
              min={1}
              max={100}
              step={0.1}
              value={thresholdDraft}
              placeholder="Auto"
              onChange={(event) => setThresholdDraft(event.target.value)}
              onBlur={commitThreshold}
              onKeyDown={(event) => event.key === "Enter" && event.currentTarget.blur()}
              aria-label="Automatic Compact threshold percent"
            />
          </label>
        </div>
        <p
          className="text-xs tabular-nums text-muted-foreground"
          title={`${effectiveContextWindow?.toLocaleString() ?? "No context total"} total${calculatedCompactPoint != null ? `; Compact at ${calculatedCompactPoint.toLocaleString()}` : "; Compact point unavailable"}`}
        >
          {effectiveContextWindow != null
            ? `Context ${formatTokenCountShort(effectiveContextWindow)}`
            : "Context unavailable"}
          {calculatedCompactPoint != null
            ? ` · Compact ${formatTokenCountShort(calculatedCompactPoint)}`
            : " · Compact unavailable"}
          {savedOverrideCount > 0
            ? ` · ${savedOverrideCount} source${savedOverrideCount === 1 ? "" : "s"} saved`
            : ""}
        </p>
        <p className="text-xs text-muted-foreground">
          Auto follows values reported by the active session. It does not learn after a Compact or
          change when the agent compacts; manual values only correct this indicator.
        </p>
        {savedSources.length > 0 ? (
          <div
            className="grid gap-2 border-t border-border pt-4"
            data-testid="saved-context-sources"
          >
            <div>
              <h4 className="text-sm font-medium text-foreground">Saved sources</h4>
              <p className="mt-1 text-xs text-muted-foreground">
                Each computer, agent, harness, and model combination keeps its own values.
              </p>
            </div>
            <div className="divide-y divide-border overflow-hidden rounded-lg border border-border">
              {savedSources.map(({ key, source, override }) => {
                const savedHostName = source.hostId
                  ? (hosts.find((host) => host.host_id === source.hostId)?.name ?? source.hostId)
                  : "Server";
                const savedHarnessName = source.harness
                  ? (BRAIN_HARNESS_LABELS[source.harness] ?? source.harness)
                  : "Unknown";
                const isCurrent = key === sourceKey;
                return (
                  <div key={key} className="grid gap-2 px-3 py-2.5 text-xs sm:grid-cols-2">
                    <div className="flex min-w-0 items-center gap-2 sm:col-span-2">
                      <span className="truncate font-medium text-foreground">{savedHostName}</span>
                      {isCurrent ? (
                        <span className="rounded-full bg-primary/10 px-2 py-0.5 text-[11px] font-medium text-primary">
                          Current
                        </span>
                      ) : null}
                    </div>
                    <span className="truncate text-muted-foreground">
                      {source.agentName || "Unknown agent"} · {savedHarnessName} ·{" "}
                      {source.model || "Auto model"}
                    </span>
                    <span className="tabular-nums text-muted-foreground sm:text-right">
                      Context {override.contextWindowTokens?.toLocaleString() ?? "Auto"} · Compact{" "}
                      {override.autoCompactThresholdPercent != null
                        ? `${override.autoCompactThresholdPercent}%`
                        : "Auto"}
                    </span>
                  </div>
                );
              })}
            </div>
          </div>
        ) : null}
      </div>

      <div className="flex items-start justify-between gap-6 border-t border-border pt-5">
        <div className="flex min-w-0 flex-col">
          <span className="text-ui font-medium">Usage limits</span>
          <span
            className="mt-1 text-xs text-muted-foreground"
            data-testid="provider-usage-source-status"
            title={providerStatusDetail}
          >
            {providerStatus}
          </span>
        </div>
        <Switch
          checked={preferences.showProviderUsageLimits}
          onCheckedChange={(showProviderUsageLimits) =>
            writeUsageContextPreferences({ ...preferences, showProviderUsageLimits })
          }
          aria-label="Show usage limits"
        />
      </div>
    </div>
  );
}
