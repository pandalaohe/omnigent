import english from "./locales/en.json";

type Variables = Record<string, string | number>;
type Pack = Record<string, unknown> & { _meta?: { locale?: string } };
const entries = english as Record<string, unknown>;

/** i18next v4 keys keep the board catalogue portable to a future app-wide namespace. */
export function createTaskBoardText(override: Pack = {}) {
  for (const key of Object.keys(override)) {
    if (!(key in entries)) throw new Error(`Unknown task board translation key: ${key}`);
  }
  const locale = override["_meta"]?.locale ?? english["_meta"].locale;
  const plurals = new Intl.PluralRules(locale);
  return {
    t(key: string, vars: Variables = {}): string {
      const plural = typeof vars.count === "number" ? `${key}_${plurals.select(vars.count)}` : key;
      const fallback =
        typeof vars.count === "number"
          ? `${key}_${new Intl.PluralRules("en").select(vars.count)}`
          : key;
      const template =
        override[plural] ?? override[key] ?? entries[fallback] ?? entries[key] ?? key;
      return String(template).replace(/\{\{(\w+)\}\}/g, (match, name: string) =>
        String(vars[name] ?? match),
      );
    },
    formatDate: (value: Date | number, options?: Intl.DateTimeFormatOptions) =>
      new Intl.DateTimeFormat(locale, options).format(value),
    formatNumber: (value: number, options?: Intl.NumberFormatOptions) =>
      new Intl.NumberFormat(locale, { maximumFractionDigits: 2, ...options }).format(value),
  };
}

const packs = import.meta.glob<Pack>("./locales/override.json", { eager: true, import: "default" });
export const { t, formatDate, formatNumber } = createTaskBoardText(Object.values(packs)[0]);
