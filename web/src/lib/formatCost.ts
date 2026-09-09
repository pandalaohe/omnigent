/** Format cumulative session spend: `$x.xx`, or `<$0.01` for sub-cent. */
export function formatSessionCostUsd(costUsd: number): string {
  if (costUsd > 0 && costUsd < 0.01) {
    return "<$0.01";
  }
  return `$${costUsd.toFixed(2)}`;
}

/**
 * Compact token-count formatter, e.g. ``842`` -> ``"842"``,
 * ``12_400`` -> ``"12.4K"``, ``1_530_000`` -> ``"1.5M"``.
 */
export function formatTokenCount(tokens: number): string {
  return new Intl.NumberFormat("en-US", {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(tokens);
}

/** Locale-stable compact token count for dense status UI (``116000`` -> ``116k``). */
export function formatTokenCountShort(tokens: number): string {
  const absolute = Math.abs(tokens);
  const unit =
    absolute >= 1_000_000_000
      ? { divisor: 1_000_000_000, suffix: "b" }
      : absolute >= 1_000_000
        ? { divisor: 1_000_000, suffix: "m" }
        : absolute >= 1_000
          ? { divisor: 1_000, suffix: "k" }
          : null;
  if (!unit) return Math.round(tokens).toString();
  return `${Number((tokens / unit.divisor).toFixed(1))}${unit.suffix}`;
}
