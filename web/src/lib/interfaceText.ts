import english from "./locales/interface.en.json";
import chinese from "./locales/interface.zh-CN.json";
import { uiLanguage } from "./uiLanguage";

type Key = keyof typeof english;
const zh: Record<Key, string> = chinese;

export function interfaceText(key: Key, values: Record<string, string | number> = {}): string {
  const template = uiLanguage === "zh-CN" ? zh[key] : english[key];
  return template.replace(/\{\{(\w+)\}\}/g, (match, name: string) => String(values[name] ?? match));
}
