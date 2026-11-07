// Keep this namespace portable to the app-wide translator when one is available.
import english from "./locales/en.json";

export function t(key: keyof typeof english, vars: Record<string, string | number> = {}): string {
  return english[key].replace(/\{\{(\w+)\}\}/g, (match, name: string) =>
    String(vars[name] ?? match),
  );
}
