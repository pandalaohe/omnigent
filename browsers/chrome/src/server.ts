export function canonicalServer(input: string): string {
  if (typeof input !== "string" || input.length > 2000 || input.includes("\\"))
    throw new Error("Enter an HTTPS server URL.");
  const url = new URL(input.trim());
  if (
    url.protocol !== "https:" ||
    url.username ||
    url.password ||
    url.search ||
    url.hash ||
    /%2f|%5c/i.test(url.pathname)
  )
    throw new Error("Use HTTPS without credentials, query or fragment.");
  return url.origin + url.pathname.replace(/\/+$/, "");
}
export function endpoint(server: string, route: string): string {
  return `${canonicalServer(server)}/${route}`;
}
export function originPermission(server: string): string {
  return `${new URL(canonicalServer(server)).origin}/*`;
}
export function sessionLink(server: string, id: string): string {
  return endpoint(server, `c/${encodeURIComponent(id)}`);
}
export function deliveryCheckLink(server: string): string {
  return endpoint(server, "settings/account#connected-apps-heading");
}
