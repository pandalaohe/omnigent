import { useEffect, useState } from "react";

import { useCodexRateLimits, useHosts } from "@/hooks/useHosts";
import { useSession } from "@/hooks/useSession";
import { useUsageContextPreferences } from "@/hooks/useUsageContextPreferences";
import { useStableProviderUsageLimits } from "@/hooks/useStableProviderUsageLimits";
import { Input } from "@/components/ui/input";
import { SavedContextSources } from "@/components/SavedContextSources";
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
  const [bufferDraft, setBufferDraft] = useState(
    activeOverride.autoCompactBufferTokens?.toString() ?? "",
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
    setBufferDraft(activeOverride.autoCompactBufferTokens?.toString() ?? "");
  }, [activeOverride.contextWindowTokens, activeOverride.autoCompactBufferTokens, sourceKey]);

  const writeOverride = (patch: Partial<typeof activeOverride>) =>
    writeUsageContextOverride(preferences, sourceKey, { ...activeOverride, ...patch });
  const commitContextWindow = () => {
    const next = optionalNumber(contextWindowDraft);
    writeOverride({ contextWindowTokens: next === null ? null : Math.round(next) });
  };
  const commitBuffer = () => {
    const next = optionalNumber(bufferDraft);
    writeOverride({ autoCompactBufferTokens: next === null ? null : Math.round(next) });
  };

  const effectiveContextWindow = optionalNumber(contextWindowDraft) ?? reportedContextWindow;
  const effectiveBuffer = optionalNumber(bufferDraft);
  const calculatedCompactPoint =
    effectiveContextWindow != null &&
    effectiveBuffer != null &&
    effectiveContextWindow > effectiveBuffer
      ? Math.round(effectiveContextWindow - effectiveBuffer)
      : reportedCompactLimit;
  const reportedBufferPlaceholder =
    reportedContextWindow != null &&
    reportedCompactLimit != null &&
    reportedContextWindow > reportedCompactLimit
      ? (reportedContextWindow - reportedCompactLimit).toString()
      : "Auto";
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
            Compact buffer (tokens)
            <Input
              type="number"
              min={1}
              step={1000}
              value={bufferDraft}
              placeholder={reportedBufferPlaceholder}
              onChange={(event) => setBufferDraft(event.target.value)}
              onBlur={commitBuffer}
              onKeyDown={(event) => event.key === "Enter" && event.currentTarget.blur()}
              aria-label="Automatic Compact buffer in tokens"
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
          Compact point = context total minus the buffer. Auto follows values reported by the active
          session. It does not learn after a Compact or change when the agent compacts; manual
          values only correct this indicator.
        </p>
        <SavedContextSources
          preferences={preferences}
          currentSourceKey={sourceKey}
          hosts={hosts}
          currentOverride={activeOverride}
        />
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
