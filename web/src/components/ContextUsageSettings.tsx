import { useEffect, useState } from "react";

import { useCodexRateLimits, useHosts } from "@/hooks/useHosts";
import { registrySourceLabels, useAgentRegistry } from "@/hooks/useAgentRegistry";
import { useSession } from "@/hooks/useSession";
import { useUsageContextPreferences } from "@/hooks/useUsageContextPreferences";
import { useStableProviderUsageLimits } from "@/hooks/useStableProviderUsageLimits";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { SavedContextSources } from "@/components/SavedContextSources";
import { Switch } from "@/components/ui/switch";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { formatTokenCountShort } from "@/lib/formatCost";
import {
  formatProviderUsageLimits,
  providerUsageLimitsFromCodex,
  type ProviderUsageLimitsSnapshot,
} from "@/lib/providerUsageLimits";
import {
  fixedBufferFitsCapacity,
  resolveUsageContext,
  usageContextLevelKey,
  usageContextLevelLabel,
  usageContextSourceKey,
  patchUsageContextOverrides,
  writeUsageContextPreferences,
  type UsageContextLevel,
  type UsageContextOverridePatch,
} from "@/lib/usageContextPreferences";
import { useChatStore } from "@/store/chatStore";

function optionalNumber(value: string): number | null {
  if (value.trim() === "") return null;
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : null;
}

function bufferNumber(value: string, percent: boolean): number | null {
  if (value.trim() === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) &&
    parsed >= 0 &&
    (percent ? parsed < 100 : Number.isInteger(parsed))
    ? parsed
    : null;
}

function SourceValue({ label, value }: { label: string; value: string }) {
  return (
    <span className="min-w-0">
      <span className="text-muted-foreground">{label}</span>{" "}
      <span className="break-all font-medium text-foreground">{value}</span>
    </span>
  );
}

export function ContextUsageSettings() {
  const preferences = useUsageContextPreferences();
  const registry = useAgentRegistry();
  const conversationId = useChatStore((state) => state.conversationId);
  const agentName = useChatStore((state) => state.boundAgentName);
  const harness = useChatStore((state) => state.sessionHarness);
  const model = useChatStore((state) => state.llmModel);
  const sessionProviderUsageLimits = useChatStore((state) => state.providerUsageLimits);
  const { session } = useSession(conversationId);
  const { data: hosts = [] } = useHosts();
  const source = {
    hostId: session?.hostId ?? "",
    agentName: agentName ?? "",
    harness: harness ?? "",
    model: model ?? "",
  };
  const sourceKey = usageContextSourceKey(source);
  const [editLevel, setEditLevel] = useState<UsageContextLevel>("model");
  const editKey = usageContextLevelKey(source, editLevel);
  const activeOverride = preferences.overrides[editKey] ?? {
    contextWindowTokens: null,
    autoCompactBufferTokens: null,
  };
  const [contextWindowDraft, setContextWindowDraft] = useState(
    activeOverride.contextWindowTokens?.toString() ?? "",
  );
  const [bufferDraft, setBufferDraft] = useState(
    activeOverride.autoCompactBufferTokens?.toString() ?? "",
  );
  const [percentDraft, setPercentDraft] = useState(
    activeOverride.autoCompactBufferPercent?.toString() ?? "",
  );
  const [inputError, setInputError] = useState("");
  const bufferMode = activeOverride.compactBufferMode ?? "tokens";
  const reportedContextWindow = useChatStore((state) => state.contextWindow);
  const reportedCompactLimit = useChatStore((state) => state.autoCompactTokenLimit);
  const resolved = resolveUsageContext(preferences, sourceKey, {
    contextWindow: reportedContextWindow,
    autoCompactTokenLimit: reportedCompactLimit,
  });
  const editedLevelContext = resolveUsageContext(preferences, editKey, {
    contextWindow: reportedContextWindow,
    autoCompactTokenLimit: reportedCompactLimit,
  }).contextWindow.value;
  const codexSession = harness === "codex" || harness === "codex-native";
  const { data: codexRateLimits, isLoading: rateLimitsLoading } = useCodexRateLimits(
    session?.hostId ?? null,
    codexSession && session?.hostId != null,
  );

  useEffect(() => {
    setContextWindowDraft(activeOverride.contextWindowTokens?.toString() ?? "");
    setBufferDraft(activeOverride.autoCompactBufferTokens?.toString() ?? "");
    setPercentDraft(activeOverride.autoCompactBufferPercent?.toString() ?? "");
    setInputError("");
  }, [
    activeOverride.contextWindowTokens,
    activeOverride.autoCompactBufferTokens,
    activeOverride.autoCompactBufferPercent,
    sourceKey,
    editKey,
  ]);

  const writeOverride = (patch: UsageContextOverridePatch) =>
    patchUsageContextOverrides(preferences, [editKey], patch);
  const commitContextWindow = () => {
    const next = optionalNumber(contextWindowDraft);
    if (contextWindowDraft.trim() !== "" && next === null) {
      setInputError("Enter a positive context total.");
      return;
    }
    const fixed = bufferNumber(bufferDraft, false);
    if (bufferDraft.trim() !== "" && fixed === null) {
      setInputError("Enter a non-negative token buffer.");
      return;
    }
    const nextContext = resolveUsageContext(
      {
        ...preferences,
        overrides: {
          ...preferences.overrides,
          [editKey]: { ...activeOverride, contextWindowTokens: next },
        },
      },
      editKey,
      { contextWindow: reportedContextWindow, autoCompactTokenLimit: reportedCompactLimit },
    ).contextWindow.value;
    if (bufferMode === "tokens" && !fixedBufferFitsCapacity(fixed, nextContext)) {
      setInputError("Context total must exceed the saved token buffer.");
      return;
    }
    setInputError("");
    writeOverride({ contextWindowTokens: next });
  };
  const commitBuffer = () => {
    const draft = bufferMode === "percent" ? percentDraft : bufferDraft;
    const next = bufferNumber(draft, bufferMode === "percent");
    if (draft.trim() !== "" && next === null) {
      setInputError(
        bufferMode === "percent"
          ? "Enter a percentage from 0 to less than 100."
          : "Enter a non-negative token buffer.",
      );
      return;
    }
    if (bufferMode === "tokens" && !fixedBufferFitsCapacity(next, editedLevelContext)) {
      setInputError("Token buffer must be less than the context total.");
      return;
    }
    setInputError("");
    writeOverride(
      bufferMode === "percent"
        ? { compactBufferMode: "percent", autoCompactBufferPercent: next }
        : { autoCompactBufferTokens: next },
    );
  };

  const effectiveContextWindow = resolved.contextWindow.value;
  const calculatedCompactPoint = resolved.autoCompactTokenLimit;
  const reportedBufferPlaceholder =
    reportedContextWindow != null &&
    reportedCompactLimit != null &&
    reportedContextWindow > reportedCompactLimit
      ? (reportedContextWindow - reportedCompactLimit).toString()
      : "Auto";
  const hostName = session?.hostId
    ? (hosts.find((host) => host.host_id === session.hostId)?.name ?? "Unknown computer")
    : "Server";
  const sourceLabels = registrySourceLabels(registry.items, agentName, harness);
  const harnessName = sourceLabels.harnessLabel;
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
            title="Each field inherits from the next level when empty. Reported values alone do not create a saved override."
          >
            Model overrides inherit computer × agent values, then agent values for all hosts.
          </p>
        </div>
        <div className="grid gap-1.5 text-sm">
          <span>Edit values for</span>
          <Select
            value={editLevel}
            onValueChange={(value) => setEditLevel(value as UsageContextLevel)}
          >
            <SelectTrigger aria-label="Edit source level">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="model">This model</SelectItem>
              <SelectItem value="cell">
                {hostName} × {sourceLabels.agentLabel}
              </SelectItem>
              <SelectItem value="column">{sourceLabels.agentLabel} · all hosts</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="grid gap-2 rounded-xl border border-border bg-muted/20 p-3 text-sm sm:grid-cols-2">
          <SourceValue label="Computer" value={hostName} />
          <SourceValue label="Agent" value={sourceLabels.agentLabel} />
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
              placeholder={resolved.contextWindow.value?.toString() ?? "Auto"}
              onChange={(event) => setContextWindowDraft(event.target.value)}
              onBlur={commitContextWindow}
              onKeyDown={(event) => event.key === "Enter" && event.currentTarget.blur()}
              aria-label="Context window override in tokens"
            />
          </label>
          <div className="grid gap-1.5 text-sm">
            <span>Compact buffer</span>
            <div className="flex gap-1" role="group" aria-label="Buffer unit">
              <Button
                type="button"
                size="xs"
                variant={bufferMode === "tokens" ? "secondary" : "outline"}
                aria-pressed={bufferMode === "tokens"}
                onClick={() => {
                  if (
                    !fixedBufferFitsCapacity(
                      activeOverride.autoCompactBufferTokens,
                      editedLevelContext,
                    )
                  ) {
                    setInputError("Token buffer must be less than the context total.");
                    return;
                  }
                  setInputError("");
                  writeOverride({ compactBufferMode: "tokens" });
                }}
              >
                Tokens
              </Button>
              <Button
                type="button"
                size="xs"
                variant={bufferMode === "percent" ? "secondary" : "outline"}
                aria-pressed={bufferMode === "percent"}
                onClick={() => writeOverride({ compactBufferMode: "percent" })}
              >
                Percent
              </Button>
            </div>
            <Input
              type="number"
              min={0}
              max={bufferMode === "percent" ? 99.999 : undefined}
              step={bufferMode === "percent" ? "any" : 1}
              value={bufferMode === "percent" ? percentDraft : bufferDraft}
              placeholder={bufferMode === "percent" ? "Auto" : reportedBufferPlaceholder}
              onChange={(event) =>
                bufferMode === "percent"
                  ? setPercentDraft(event.target.value)
                  : setBufferDraft(event.target.value)
              }
              onBlur={commitBuffer}
              onKeyDown={(event) => event.key === "Enter" && event.currentTarget.blur()}
              aria-label={
                bufferMode === "percent"
                  ? "Compact buffer percent"
                  : "Automatic Compact buffer in tokens"
              }
            />
          </div>
        </div>
        {inputError ? (
          <p role="alert" className="text-xs text-destructive">
            {inputError}
          </p>
        ) : null}
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
        <div
          className="grid gap-1 text-xs text-muted-foreground"
          data-testid="current-source-resolution"
        >
          <p>
            Context {resolved.contextWindow.value?.toLocaleString() ?? "Auto"} ·{" "}
            {usageContextLevelLabel(resolved.contextWindow.level, {
              hostLabel: hostName,
              agentLabel: sourceLabels.agentLabel,
            })}
          </p>
          <p>
            Buffer {resolved.buffer.value?.toLocaleString() ?? "Auto"}
            {resolved.buffer.mode === "percent" && resolved.buffer.value != null ? "%" : ""} ·{" "}
            {usageContextLevelLabel(resolved.buffer.level, {
              hostLabel: hostName,
              agentLabel: sourceLabels.agentLabel,
            })}
          </p>
          {resolved.ignored.map((item) => (
            <p key={item.level}>
              Ignored{" "}
              {usageContextLevelLabel(item.level, {
                hostLabel: hostName,
                agentLabel: sourceLabels.agentLabel,
              })}
              : {item.reason}; using{" "}
              {usageContextLevelLabel(resolved.buffer.level, {
                hostLabel: hostName,
                agentLabel: sourceLabels.agentLabel,
              })}
              .
            </p>
          ))}
        </div>
        <p className="text-xs text-muted-foreground">
          Compact point = context total minus the selected buffer. Empty fields inherit from the
          next level. Manual values change only this indicator.
        </p>
        <SavedContextSources
          registryItems={registry.items}
          preferences={preferences}
          currentSourceKey={sourceKey}
          hosts={hosts}
          currentOverride={
            preferences.overrides[sourceKey] ?? {
              contextWindowTokens: null,
              autoCompactBufferTokens: null,
            }
          }
          reportedContextWindow={reportedContextWindow}
          reportedAutoCompactTokenLimit={reportedCompactLimit}
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
