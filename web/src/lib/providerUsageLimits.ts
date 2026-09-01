import type { CodexRateLimitsSnapshot } from "@/hooks/useHosts";

export interface ProviderUsageWindow {
  label: string;
  ariaLabel: string;
  usedPercent: number;
  durationMinutes?: number;
  resetsAt?: number;
}

/** Provider-neutral account allowance snapshot produced by a harness adapter. */
export interface ProviderUsageLimitsSnapshot {
  provider: string;
  scope?: string;
  capturedAt: number;
  windows: ProviderUsageWindow[];
}

export interface FormattedProviderUsageLimits {
  text: string;
  ariaLabel: string;
  scope: string;
}

/** Parse the server's bounded snake-case snapshot at the HTTP/SSE boundary. */
export function providerUsageLimitsFromWire(value: unknown): ProviderUsageLimitsSnapshot | null {
  if (!value || typeof value !== "object") return null;
  const raw = value as Record<string, unknown>;
  if (
    typeof raw.provider !== "string" ||
    !raw.provider ||
    typeof raw.captured_at !== "number" ||
    !Number.isInteger(raw.captured_at) ||
    !Array.isArray(raw.windows)
  ) {
    return null;
  }
  const windows: ProviderUsageWindow[] = [];
  for (const candidate of raw.windows) {
    if (!candidate || typeof candidate !== "object") return null;
    const window = candidate as Record<string, unknown>;
    if (
      typeof window.label !== "string" ||
      typeof window.aria_label !== "string" ||
      typeof window.used_percent !== "number" ||
      !Number.isFinite(window.used_percent)
    ) {
      return null;
    }
    windows.push({
      label: window.label,
      ariaLabel: window.aria_label,
      usedPercent: window.used_percent,
      ...(typeof window.duration_mins === "number"
        ? { durationMinutes: window.duration_mins }
        : {}),
      ...(typeof window.resets_at === "number" ? { resetsAt: window.resets_at } : {}),
    });
  }
  return {
    provider: raw.provider,
    ...(typeof raw.scope === "string" ? { scope: raw.scope } : {}),
    capturedAt: raw.captured_at,
    windows,
  };
}

const CODEX_WINDOW_TARGETS = [
  { label: "5h", ariaLabel: "5 hour", minutes: 300 },
  { label: "w", ariaLabel: "weekly", minutes: 10_080 },
  { label: "m", ariaLabel: "monthly", minutes: 43_200 },
] as const;
const HARD_TTL_SECONDS = 3600;

function normalizedLimitName(value: string | null | undefined): string {
  return (value ?? "").toLowerCase().replace(/[^a-z0-9]+/g, "");
}

/** Adapt Codex's account/bucket RPC into the same shape used by other harnesses. */
export function providerUsageLimitsFromCodex(
  snapshot: CodexRateLimitsSnapshot | null | undefined,
  model: string | null | undefined,
): ProviderUsageLimitsSnapshot | null {
  if (!snapshot || !Array.isArray(snapshot.limits) || snapshot.limits.length === 0) return null;
  const normalizedModel = normalizedLimitName(model);
  const modelBucket = normalizedModel
    ? snapshot.limits.find((bucket) => {
        const name = normalizedLimitName(bucket.limit_name);
        return (
          name.length > 0 && (name.includes(normalizedModel) || normalizedModel.includes(name))
        );
      })
    : undefined;
  const bucket =
    modelBucket ??
    snapshot.limits.find((candidate) => candidate.limit_id.toLowerCase() === "codex") ??
    snapshot.limits[0];
  if (!bucket || !Array.isArray(bucket.windows)) return null;

  const windows = CODEX_WINDOW_TARGETS.flatMap((target) => {
    const window = bucket.windows.find(
      (candidate) =>
        Number.isFinite(candidate.window_duration_mins) &&
        Math.abs(candidate.window_duration_mins - target.minutes) <= target.minutes * 0.05,
    );
    if (!window || !Number.isFinite(window.used_percent) || window.used_percent < 0) return [];
    return [
      {
        label: target.label,
        ariaLabel: target.ariaLabel,
        usedPercent: Math.round(Math.min(window.used_percent, 100)),
        durationMinutes: window.window_duration_mins,
        ...(window.resets_at !== undefined ? { resetsAt: window.resets_at } : {}),
      },
    ];
  });
  return {
    provider: "Codex",
    scope: bucket.limit_name ?? bucket.limit_id,
    capturedAt: snapshot.captured_at,
    windows,
  };
}

/** Format only truthful windows; missing provider data renders nothing. */
export function formatProviderUsageLimits(
  snapshot: ProviderUsageLimitsSnapshot | null | undefined,
  nowSeconds = Math.floor(Date.now() / 1000),
): FormattedProviderUsageLimits | null {
  if (
    !snapshot ||
    !Number.isInteger(snapshot.capturedAt) ||
    nowSeconds - snapshot.capturedAt > HARD_TTL_SECONDS ||
    !Array.isArray(snapshot.windows)
  ) {
    return null;
  }
  const windows = snapshot.windows.filter(
    (window) =>
      window.label.length > 0 && Number.isFinite(window.usedPercent) && window.usedPercent >= 0,
  );
  if (windows.length === 0) return null;
  const provider = snapshot.provider || "Provider";
  const scope = snapshot.scope || provider;
  return {
    text: windows
      .map((window) => `${window.label}:${Math.round(Math.min(window.usedPercent, 100))}%`)
      .join(" "),
    ariaLabel: `${provider} ${scope} usage: ${windows
      .map(
        (window) =>
          `${window.ariaLabel || window.label} ${Math.round(Math.min(window.usedPercent, 100))}% used`,
      )
      .join(", ")}`,
    scope,
  };
}
