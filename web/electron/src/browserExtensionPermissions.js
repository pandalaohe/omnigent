"use strict";

function matchesHost(pattern, rawUrl) {
  try {
    const url = new URL(rawUrl);
    if (!["http:", "https:"].includes(url.protocol)) return false;
    if (pattern === "<all_urls>") return true;
    const match = /^(\*|https?):\/\/([^/]+)\//.exec(pattern);
    if (!match || (match[1] !== "*" && `${match[1]}:` !== url.protocol)) return false;
    const authority = /^(\[[^\]]+\]|[^:]+)(?::(\*|\d+))?$/.exec(match[2]);
    if (!authority) return false;
    const host = authority[1].toLowerCase();
    return (
      (host === "*" ||
        host === url.hostname ||
        (host.startsWith("*.") &&
          (url.hostname === host.slice(2) || url.hostname.endsWith(host.slice(1))))) &&
      (!authority[2] ||
        authority[2] === "*" ||
        authority[2] === (url.port || (url.protocol === "https:" ? "443" : "80")))
    );
  } catch {
    return false;
  }
}
function extensionAccess(host, extension, permission, url) {
  if (!extension) return false;
  const granted = host.api.permissions?.getAll({ extension });
  const permissions = granted?.permissions || extension.manifest?.permissions || [];
  const origins = granted?.origins || extension.manifest?.host_permissions || [];
  return (
    permissions.includes(permission) &&
    (url === undefined || origins.some((pattern) => matchesHost(pattern, url)))
  );
}
function cookieAccess(host, extension, cookie) {
  const authority = cookie.domain.replace(/^\./, "");
  return (
    extensionAccess(host, extension, "cookies", `https://${authority}${cookie.path || "/"}`) ||
    (!cookie.secure &&
      extensionAccess(host, extension, "cookies", `http://${authority}${cookie.path || "/"}`))
  );
}
function guardExtensionCookies(host, extensions) {
  const router = host.ctx.router;
  for (const name of ["get", "getAll", "set", "remove", "getAllCookieStores"]) {
    const handler = router.handlers?.get(`cookies.${name}`);
    if (!handler) continue;
    const original = handler.callback;
    handler.callback = async (event, details = {}) => {
      if (["get", "set", "remove"].includes(name) && typeof details.url !== "string")
        throw new Error("A cookie URL is required.");
      const senderUrl = event.sender?.scope || event.sender?.getURL?.() || "";
      if (
        !senderUrl.startsWith(`chrome-extension://${event.extension?.id}/`) ||
        !extensionAccess(host, event.extension, "cookies", details.url)
      )
        throw new Error("Cookie access is not granted for this extension and site.");
      const result = await original(event, details);
      if (name === "getAll")
        return result.filter((cookie) => cookieAccess(host, event.extension, cookie));
      return result;
    };
  }
  const send = router.sendEvent.bind(router);
  router.sendEvent = (target, name, ...args) => {
    if (name !== "cookies.onChanged") return send(target, name, ...args);
    for (const extension of extensions())
      if ((!target || target === extension.id) && cookieAccess(host, extension, args[0].cookie))
        send(extension.id, name, ...args);
  };
}
module.exports = { matchesHost, extensionAccess, guardExtensionCookies };
