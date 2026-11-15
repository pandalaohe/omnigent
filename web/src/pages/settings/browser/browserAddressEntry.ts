/** Mirrors the desktop allowlist grammar; the native save validates again. */
export function validAddressEntry(raw: string): boolean {
  const entry = raw.trim();
  const cidr = /^([0-9A-Fa-fxX.]+)\/(\d+)$/.exec(entry);
  if (cidr) {
    if (Number(cidr[2]) > 32) return false;
    try {
      return /^\d+\.\d+\.\d+\.\d+$/.test(new URL(`http://${cidr[1]}/`).hostname);
    } catch {
      return false;
    }
  }
  if (entry.startsWith("*.")) {
    const suffix = entry.slice(2);
    if (!validAddressEntry(suffix) || suffix.startsWith("*.") || suffix.includes("/")) return false;
    try {
      const host = new URL(`http://${suffix}/`).hostname;
      return !host.startsWith("[") && !/^\d+\.\d+\.\d+\.\d+$/.test(host);
    } catch {
      return false;
    }
  }
  const host = /^(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._-]+)(?::(\d{1,5}))?$/.exec(entry);
  if (!host) return false;
  if (host[2] !== undefined && (Number(host[2]) < 1 || Number(host[2]) > 65535)) return false;
  try {
    return new URL(`http://${host[1]}/`).protocol === "http:";
  } catch {
    return false;
  }
}
