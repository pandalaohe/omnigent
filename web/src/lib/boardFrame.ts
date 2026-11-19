import { createPath, parsePath } from "react-router-dom";
import { stripBasePath } from "./basePath";
import { useLocation } from "./routing";

export function isFramePath(path = window.location.pathname): boolean {
  return stripBasePath(path).startsWith("/frame/");
}

export function boardPath(path: string, pathname?: string): string {
  return isFramePath(pathname) ? `/frame${path}` : path;
}

export function boardExitProps(): { target?: "_blank"; rel?: "noopener" } {
  return isFramePath() ? { target: "_blank", rel: "noopener" } : {};
}

export function usageBoardTheme(search: URLSearchParams): "dark" | "light" | undefined {
  const theme = search.get("theme");
  return theme === "dark" || theme === "light" ? theme : undefined;
}

export function useBoardPath() {
  const { pathname, search } = useLocation();
  const theme = usageBoardTheme(new URLSearchParams(search));
  return (path: string) => {
    const destination = boardPath(path, pathname);
    if (!theme) return destination;
    const target = parsePath(destination);
    const next = new URLSearchParams(target.search);
    next.set("theme", theme);
    return createPath({ ...target, search: next.toString() });
  };
}

export function usageAccountDetailPath(accountId: string, focus?: string | null): string {
  const search = new URLSearchParams({ account: accountId, view: "details" });
  if (focus) search.set("focus", focus);
  return `/usage/board?${search}`;
}

export function usageAccountSubpagePath(
  accountId: string,
  page: "settings" | "issues" | "models",
  focus?: string | null,
): string {
  const path = `/usage/board/${encodeURIComponent(accountId)}/${page}`;
  return focus ? `${path}?${new URLSearchParams({ focus })}` : path;
}
