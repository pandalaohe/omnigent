import { stripBasePath } from "./basePath";

export function isFramePath(path = window.location.pathname): boolean {
  return stripBasePath(path).startsWith("/frame/");
}

export function boardPath(path: string): string {
  return isFramePath() ? `/frame${path}` : path;
}

export function boardExitProps(): { target?: "_blank"; rel?: "noopener" } {
  return isFramePath() ? { target: "_blank", rel: "noopener" } : {};
}
