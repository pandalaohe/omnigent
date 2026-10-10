import type { NativeModelOption } from "@/lib/types";

export interface SpeedOption {
  value: string;
  label: string;
}

function rowForModel(models: readonly NativeModelOption[], model: string | null | undefined) {
  return model
    ? models.find((option) => option.id === model || option.model === model)
    : models.find((option) => option.isDefault);
}

export function speedOptionsForModel(
  models: readonly NativeModelOption[],
  model: string | null | undefined,
): SpeedOption[] {
  const row = rowForModel(models, model);
  const options: SpeedOption[] = [{ value: "standard", label: "Standard" }];
  for (const tier of row?.serviceTiers ?? []) {
    if (!tier.id || tier.id === "default" || tier.id === "standard") continue;
    const value = tier.id === "priority" ? "fast" : tier.id;
    if (!options.some((option) => option.value === value)) {
      options.push({ value, label: tier.name || tier.id });
    }
  }
  return options;
}

/** Display the provider default without turning it into an explicit override. */
export function defaultSpeedForModel(
  models: readonly NativeModelOption[],
  model: string | null | undefined,
): string {
  const tier = rowForModel(models, model)?.defaultServiceTier;
  const value = tier === "priority" ? "fast" : tier === "default" || !tier ? "standard" : tier;
  return speedOptionsForModel(models, model).some((option) => option.value === value)
    ? value
    : "standard";
}

/** Unsupported saved tiers clear to Default so callers never submit them. */
export function reconcileSpeed(
  value: string | null | undefined,
  models: readonly NativeModelOption[],
  model: string | null | undefined,
): string | null {
  if (!value) return null;
  const canonical = value === "priority" ? "fast" : value;
  return speedOptionsForModel(models, model).some((option) => option.value === canonical)
    ? canonical
    : null;
}
