import type { ChoiceLimitRule, SessionChoiceDocument } from "./sessionChoiceData";

export function choiceDocumentForSave(
  document: SessionChoiceDocument,
  baseline: SessionChoiceDocument | null,
): SessionChoiceDocument {
  return {
    ...document,
    limits: Object.fromEntries(
      Object.entries(document.limits).map(([id, value]) => [
        id,
        value.reserve &&
        value.reserve.pct == null &&
        value.reserve.stopPct == null &&
        JSON.stringify(value) !== JSON.stringify(baseline?.limits[id])
          ? { ...value, reserve: null }
          : value,
      ]),
    ),
  };
}

export function replaceLimitRules(
  document: SessionChoiceDocument,
  rules: ChoiceLimitRule[],
): SessionChoiceDocument {
  return { ...document, schemaVersion: 2, limitRules: rules };
}

export function moveLimitRule(document: SessionChoiceDocument, id: string, offset: number) {
  const rules = [...(document.limitRules ?? [])];
  const from = rules.findIndex((rule) => rule.id === id);
  const to = from + offset;
  if (from < 0 || to < 0 || to >= rules.length) return document;
  const [rule] = rules.splice(from, 1);
  rules.splice(to, 0, rule);
  return replaceLimitRules(document, rules);
}

export function limitRuleErrors(document: SessionChoiceDocument): string[] {
  const errors: string[] = [];
  for (const [index, rule] of (document.limitRules ?? []).entries()) {
    const prefix = `Rule ${index + 1}`;
    if (!rule.agent.trim()) errors.push(`${prefix}: choose an agent.`);
    if (rule.maxRunning != null && (!Number.isSafeInteger(rule.maxRunning) || rule.maxRunning < 1))
      errors.push(`${prefix}: max running must be a positive integer.`);
    for (const key of ["pct", "stopPct"] as const) {
      const value = rule.reserve?.[key];
      if (value != null && (!Number.isFinite(value) || value < 0 || value > 100))
        errors.push(`${prefix}: ${key === "pct" ? "warning" : "stop"} must be between 0 and 100%.`);
    }
  }
  return errors;
}
