import en from "./locales/autoRotation.en.json";

export type AutoRotationTextKey = keyof typeof en;

export function t(key: AutoRotationTextKey, values?: Record<string, string | number>): string {
  const template = en[key];
  if (!values) return template;
  return template.replace(/\{(\w+)\}/g, (_match, name: string) =>
    Object.hasOwn(values, name) ? String(values[name]) : "",
  );
}
