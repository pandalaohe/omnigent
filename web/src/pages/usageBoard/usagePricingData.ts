import { useQuery } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";
import { usageJson } from "./usagePriceSourceData";

export type Currency = "CNY" | "USD" | "HKD";
export const CURRENCIES: Currency[] = ["CNY", "USD", "HKD"];
export const RATE_KEYS = ["read", "write", "cache_read", "cache_write"] as const;
export interface PricingCard {
  id: string;
  name: string;
  retail_amount: string;
  purchase_currency: Currency;
  allowance_usd: string;
  period_kind: "week" | "month" | "month_weekly_reset";
  starts_on: string;
  timezone: string;
  target_kind: "account" | "pool";
  target_id: string;
  parent_account_id: string | null;
  rates_usd_per_million:
    (Record<(typeof RATE_KEYS)[number], string> & { model?: string | null }) | null;
  discount_override: string | null;
}
export interface PricingBaseline {
  revision: number;
  defaults: {
    schema_version: 1;
    default_discount: string;
    display_currency: Currency;
    fx_to_display: Partial<Record<Currency, string>>;
  };
  cards: PricingCard[];
}
export const PRICING_QUERY_KEY = ["usageBoard", "pricingBaseline"];
export function usePricingBaseline() {
  return useQuery({
    queryKey: PRICING_QUERY_KEY,
    queryFn: async () => {
      const value = await usageJson<PricingBaseline>("pricing-baseline");
      if (!value.defaults || !Array.isArray(value.cards)) throw new Error("Pricing is unavailable");
      return value;
    },
    retry: false,
  });
}

export class PricingSaveError extends Error {
  status: number;
  fields: Record<string, string>;
  constructor(message: string, status: number, fields: Record<string, string> = {}) {
    super(message);
    this.status = status;
    this.fields = fields;
  }
}
export async function savePricingBaseline(value: PricingBaseline): Promise<PricingBaseline> {
  const response = await authenticatedFetch("/v1/me/usage/pricing-baseline", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      revision: value.revision,
      defaults: value.defaults,
      cards: value.cards,
    }),
  });
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const fields: Record<string, string> = {};
    if (Array.isArray(body?.detail))
      for (const item of body.detail) {
        const loc = item.loc?.slice(1) ?? [];
        if (loc[0] === "cards" && typeof loc[1] === "number")
          loc[1] = value.cards[loc[1]]?.id ?? loc[1];
        fields[loc.join(".")] = item.msg;
      }
    throw new PricingSaveError(
      typeof body?.detail === "string"
        ? body.detail
        : `Pricing could not be saved (${response.status})`,
      response.status,
      fields,
    );
  }
  return body as PricingBaseline;
}

export function validDecimal(value: string): boolean {
  if (!/^(0|[1-9]\d*)(\.\d+)?$/.test(value)) return false;
  const [whole, part = ""] = value.split(".");
  return whole.length <= 20 && part.length <= 18 && whole.length + part.length <= 38;
}
// Fixed-point strings use native integers, so the draft preview never passes
// through binary floating point or display-rounded input.
export function multiplyDecimal(...values: string[]): string | null {
  if (values.some((value) => value.length > 256 || !/^(0|[1-9]\d*)(\.\d+)?$/.test(value)))
    return null;
  let product = 1n,
    scale = 0;
  for (const value of values) {
    product *= BigInt(value.replace(".", ""));
    scale += value.split(".")[1]?.length ?? 0;
  }
  const digits = product.toString().padStart(scale + 1, "0");
  return scale ? `${digits.slice(0, -scale)}.${digits.slice(-scale)}` : digits;
}
export function decimalDisplay(value: string | null, places = 2, trim = false): string {
  if (value === null || !/^(0|[1-9]\d*)(\.\d+)?$/.test(value)) return "Unknown";
  const [whole, fraction = ""] = value.split(".");
  const kept = fraction.padEnd(places, "0").slice(0, places);
  let units = BigInt(whole + kept);
  const tail = fraction.slice(places);
  if (
    tail &&
    (tail[0] > "5" || (tail[0] === "5" && (/[1-9]/.test(tail.slice(1)) || units % 2n === 1n)))
  )
    units++;
  const digits = units.toString().padStart(places + 1, "0");
  const result = `${digits.slice(0, -places)}.${digits.slice(-places)}`;
  return trim ? result.replace(/\.?0+$/, "") : result;
}

export function validatePricing(value: PricingBaseline): Record<string, string> {
  const errors: Record<string, string> = {};
  const decimal = (path: string, text: string, positive = false, discount = false) => {
    if (!validDecimal(text))
      errors[path] = "Use a decimal: up to 20 whole and 18 fractional digits.";
    else if (positive && !/[1-9]/.test(text)) errors[path] = "Must be greater than zero.";
    else if (discount && !/^0(?:\.\d+)?$|^1(?:\.0+)?$/.test(text))
      errors[path] = "Use a discount from 0 to 1.";
  };
  decimal("defaults.default_discount", value.defaults.default_discount, false, true);
  for (const [currency, rate] of Object.entries(value.defaults.fx_to_display))
    decimal(`defaults.fx_to_display.${currency}`, rate, true);
  for (const card of value.cards) {
    const prefix = `cards.${card.id}.`;
    if (!card.name.trim()) errors[prefix + "name"] = "Enter a card name.";
    if (!card.target_id) errors[prefix + "target_id"] = "Choose an account or pool.";
    if (!/^\d{4}-\d{2}-\d{2}$/.test(card.starts_on) || !Number.isFinite(Date.parse(card.starts_on)))
      errors[prefix + "starts_on"] = "Enter the purchase start date.";
    try {
      new Intl.DateTimeFormat("en", { timeZone: card.timezone }).resolvedOptions();
    } catch {
      errors[prefix + "timezone"] = "Use an IANA timezone.";
    }
    decimal(prefix + "retail_amount", card.retail_amount);
    decimal(prefix + "allowance_usd", card.allowance_usd, true);
    if (card.discount_override !== null && card.discount_override !== "")
      decimal(prefix + "discount_override", card.discount_override, false, true);
    if (card.rates_usd_per_million)
      for (const key of RATE_KEYS)
        decimal(prefix + "rates_usd_per_million." + key, card.rates_usd_per_million[key]);
  }
  return errors;
}

/** Reapply edits to the refreshed revision while keeping concurrent new cards. */
export function reapplyPricing(
  original: PricingBaseline,
  edit: PricingBaseline,
  latest: PricingBaseline,
): PricingBaseline {
  const changed = edit.cards.filter(
    (card) =>
      JSON.stringify(card) !== JSON.stringify(original.cards.find((old) => old.id === card.id)),
  );
  const cards = new Map(latest.cards.map((card) => [card.id, card]));
  for (const card of changed) cards.set(card.id, card);
  return {
    revision: latest.revision,
    defaults:
      JSON.stringify(edit.defaults) === JSON.stringify(original.defaults)
        ? latest.defaults
        : edit.defaults,
    cards: [...cards.values()],
  };
}
