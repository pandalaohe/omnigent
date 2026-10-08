// Allowlist for AGENT-driven browser navigation (the URL-bar path
// stays permissive — it's a user gesture). An unguarded model-issued loadURL
// could point the view at file:// / cloud-metadata / loopback / private hosts
// and read the bytes back via screenshot (SSRF + local-file read + exfil).
// `opts.allowlist` can open named internal hosts/CIDRs, but link-local,
// metadata and unspecified addresses stay refused even when listed.
// Runs in the main process (gate holds regardless of caller); pure + dep-free
// so `node --test` can exercise it without Electron.

"use strict";

// Schemes the agent may navigate to; everything else (file:, chrome:, data:,
// javascript:, ...) is a privileged surface or code channel and is rejected.
const ALLOWED_SCHEMES = new Set(["http:", "https:"]);

// Allowlist entry shapes. Both are validated before any URL parsing so
// credentials, paths, schemes and CIDR ports can never widen the policy.
// Host: a bracketed IPv6 literal or a name/WHATWG-IPv4 form, optional port.
const ALLOWLIST_HOST_ENTRY = /^(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._-]+)(?::(\d{1,5}))?$/;
// CIDR: the base may only hold characters a WHATWG IPv4 form can contain.
const ALLOWLIST_IPV4_CIDR = /^([0-9A-Fa-fxX.]+)\/(\d+)$/;

/**
 * Parse a dotted-quad IPv4 host into four octets, or null if not IPv4. The
 * WHATWG URL parser already canonicalizes obfuscated forms (`0x7f000001`,
 * `2130706433`) to dotted-decimal, so `host` here is dotted-quad or a name.
 *
 * @param {string} host
 * @returns {[number, number, number, number] | null}
 */
function parseIpv4(host) {
  const parts = host.split(".");
  if (parts.length !== 4) return null;
  const octets = [];
  for (const part of parts) {
    if (!/^\d{1,3}$/.test(part)) return null;
    const n = Number(part);
    if (n < 0 || n > 255) return null;
    octets.push(n);
  }
  return /** @type {[number,number,number,number]} */ (octets);
}

/**
 * True if the octets are loopback / link-local / RFC-1918 private — the ranges
 * an SSRF payload targets (metadata 169.254.169.254, loopback, internal hosts).
 *
 * @param {[number, number, number, number]} octets
 */
function isBlockedIpv4(octets) {
  const [a, b] = octets;
  if (a === 127) return true; // 127.0.0.0/8   loopback
  if (a === 10) return true; // 10.0.0.0/8    private
  if (a === 169 && b === 254) return true; // 169.254.0.0/16 link-local (incl. 169.254.169.254 metadata)
  if (a === 172 && b >= 16 && b <= 31) return true; // 172.16.0.0/12 private
  if (a === 192 && b === 168) return true; // 192.168.0.0/16 private
  if (a === 0) return true; // 0.0.0.0/8     "this host"
  return false;
}

/**
 * Parse a bracketed IPv6 literal into eight 16-bit hextets, or null. The URL
 * parser serializes IPv6 hosts as lowercase hex with the longest zero run
 * compressed to `::` and never leaves a dotted quad inside the brackets, so
 * that subset is all this accepts.
 *
 * @param {string} hostname
 * @returns {number[] | null}
 */
function parseIpv6(hostname) {
  if (!hostname.startsWith("[") || !hostname.endsWith("]")) return null;
  const halves = hostname.slice(1, -1).split("::");
  if (halves.length > 2) return null;
  const head = halves[0] === "" ? [] : halves[0].split(":");
  let parts = head;
  if (halves.length === 2) {
    const tail = halves[1] === "" ? [] : halves[1].split(":");
    const zeros = 8 - head.length - tail.length;
    if (zeros < 1) return null;
    parts = [...head, ...Array(zeros).fill("0"), ...tail];
  }
  if (parts.length !== 8) return null;
  const hextets = [];
  for (const part of parts) {
    if (!/^[0-9a-f]{1,4}$/.test(part)) return null;
    hextets.push(parseInt(part, 16));
  }
  return hextets;
}

/**
 * The IPv4 address embedded in a mapped (`::ffff:0:0/96`), compatible
 * (`::/96`, excluding `::` and `::1`), or NAT64 (`64:ff9b::/96`) IPv6
 * literal, or null for every other address.
 *
 * @param {number[] | null} hextets
 * @returns {[number, number, number, number] | null}
 */
function ipv4FromIpv6(hextets) {
  if (!hextets) return null;
  const [h0, h1, h2, h3, h4, h5, h6, h7] = hextets;
  const leadingZeros = h0 === 0 && h1 === 0 && h2 === 0 && h3 === 0 && h4 === 0;
  const mapped = leadingZeros && h5 === 0xffff;
  const compatible = leadingZeros && h5 === 0 && (h6 !== 0 || h7 > 1);
  const nat64 = h0 === 0x64 && h1 === 0xff9b && h2 === 0 && h3 === 0 && h4 === 0 && h5 === 0;
  if (!mapped && !compatible && !nat64) return null;
  return [(h6 >> 8) & 0xff, h6 & 0xff, (h7 >> 8) & 0xff, h7 & 0xff];
}

/**
 * @param {[number, number, number, number]} octets
 * @returns {number} the address as a 32-bit unsigned integer
 */
function ipv4ToUint32(octets) {
  const [a, b, c, d] = octets;
  return ((a << 24) | (b << 16) | (c << 8) | d) >>> 0;
}

/**
 * Parse one allowlist item into a CIDR or host entry; malformed items return
 * null and are ignored, so they can never widen the policy.
 *
 * @param {unknown} raw
 * @returns {{ kind: "cidr", network: number, mask: number }
 *   | { kind: "host", host: string, port: number | null,
 *        ipv4: [number, number, number, number] | null }
 *   | null}
 */
function parseAllowlistEntry(raw) {
  if (typeof raw !== "string") return null;
  const entry = raw.trim();
  if (entry === "") return null;

  const cidr = ALLOWLIST_IPV4_CIDR.exec(entry);
  if (cidr) {
    const bits = Number(cidr[2]);
    if (bits > 32) return null;
    let hostname;
    try {
      hostname = new URL(`http://${cidr[1]}/`).hostname;
    } catch {
      return null;
    }
    const ipv4 = parseIpv4(hostname);
    if (!ipv4) return null;
    // `>>> 32` is `>>> 0` in JS, so /0 gets an explicit zero mask.
    const mask = bits === 0 ? 0 : (0xffffffff << (32 - bits)) >>> 0;
    return { kind: "cidr", network: (ipv4ToUint32(ipv4) & mask) >>> 0, mask };
  }

  const host = ALLOWLIST_HOST_ENTRY.exec(entry);
  if (!host) return null;
  const port = host[2] === undefined ? null : Number(host[2]);
  if (port !== null && (port < 1 || port > 65535)) return null;
  let hostname;
  try {
    hostname = new URL(`http://${host[1]}/`).hostname;
  } catch {
    return null;
  }
  const canonicalHost = hostname.replace(/\.+$/, "");
  return {
    kind: "host",
    host: canonicalHost,
    port,
    ipv4: parseIpv4(canonicalHost) ?? ipv4FromIpv6(parseIpv6(canonicalHost)),
  };
}

/**
 * True when the URL's canonical host, canonical IPv4 or effective port matches
 * any allowlist entry. Host entries match by canonical name or equal canonical
 * IPv4, and by port only when they name one; CIDR entries match by prefix and
 * ignore ports.
 *
 * @param {string} host
 * @param {[number, number, number, number] | null} ipv4
 * @param {number} port
 * @param {unknown} allowlist
 */
function isAllowlisted(host, ipv4, port, allowlist) {
  if (!Array.isArray(allowlist)) return false;
  for (const raw of allowlist) {
    const entry = parseAllowlistEntry(raw);
    if (!entry) continue;
    if (entry.kind === "cidr") {
      if (ipv4 && (ipv4ToUint32(ipv4) & entry.mask) >>> 0 === entry.network) return true;
      continue;
    }
    const sameHost =
      entry.host === host ||
      (entry.ipv4 !== null && ipv4 !== null && ipv4ToUint32(entry.ipv4) === ipv4ToUint32(ipv4));
    if (sameHost && (entry.port === null || entry.port === port)) return true;
  }
  return false;
}

/**
 * Decide whether an AGENT-issued navigation to `url` is allowed: `{ ok: true }`
 * for an http(s) URL to a non-internal host (or eligible Arca loopback, or a
 * host/CIDR named in `opts.allowlist`), else `{ ok: false, error }`. Never
 * throws — an unparseable URL is a rejection.
 *
 * A refusal of the INTERNAL class — the loopback/private ranges an allowlist
 * entry can lift — also carries `grant`, a stable `"<canonical host>:<port>"`
 * the UI may remember back into `opts.allowlist`. Never-allowable refusals
 * (link-local/metadata, `0.0.0.0`/`::`, bad schemes) carry no grant.
 *
 * @param {string} url
 * @param {{ allowLocalhost?: boolean, allowlist?: unknown }} [opts]
 * @returns {{ ok: true } | { ok: false, error: string, grant?: string }}
 */
function isAgentNavigationAllowed(url, { allowLocalhost = false, allowlist } = {}) {
  if (typeof url !== "string" || url.trim() === "") {
    return { ok: false, error: "navigation blocked: empty url" };
  }
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return { ok: false, error: `navigation blocked: not a valid absolute URL: ${url}` };
  }
  if (!ALLOWED_SCHEMES.has(parsed.protocol)) {
    return {
      ok: false,
      error: `navigation blocked: scheme "${parsed.protocol}" is not allowed for agent navigation (only http/https)`,
    };
  }
  const hostname = parsed.hostname;
  if (allowLocalhost && ["localhost", "127.0.0.1", "[::1]"].includes(hostname)) {
    return { ok: true };
  }

  // Canonical host: trailing dots resolve away, and the canonical IPv4 is the
  // dotted quad itself or the address embedded in a mapped/compatible/NAT64
  // IPv6 literal (`[::ffff:7f00:1]` → 127.0.0.1).
  const host = hostname.replace(/\.+$/, "");
  const hextets = parseIpv6(host);
  const ipv4 = parseIpv4(host) ?? ipv4FromIpv6(hextets);

  // Link-local/metadata and "this host" addresses are never allowable, even
  // when the allowlist names them.
  if (ipv4 && (ipv4[0] === 0 || (ipv4[0] === 169 && ipv4[1] === 254))) {
    return {
      ok: false,
      error: `navigation blocked: host "${hostname}" is a link-local/loopback/private-range address`,
    };
  }
  if (hextets && (hextets.every((h) => h === 0) || (hextets[0] & 0xffc0) === 0xfe80)) {
    return {
      ok: false,
      error: `navigation blocked: host "${hostname}" is a loopback/internal host`,
    };
  }

  const port = parsed.port === "" ? (parsed.protocol === "https:" ? 443 : 80) : Number(parsed.port);
  if (isAllowlisted(host, ipv4, port, allowlist)) return { ok: true };

  // The link-local/metadata and unspecified refusals above returned already, so
  // every internal-class refusal below sits in a range an allowlist can lift.
  // Canonical IPv4 wins, else the trailing-dot-stripped host (IPv6 bracketed).
  const grant = `${ipv4 ? ipv4.join(".") : host}:${port}`;
  if (host === "localhost" || host.endsWith(".localhost") || host === "[::1]") {
    return {
      ok: false,
      error: `navigation blocked: host "${hostname}" is a loopback/internal host`,
      grant,
    };
  }
  if (ipv4 && isBlockedIpv4(ipv4)) {
    return {
      ok: false,
      error: `navigation blocked: host "${hostname}" is a link-local/loopback/private-range address`,
      grant,
    };
  }
  if (hextets && (hextets[0] & 0xfe00) === 0xfc00) {
    return {
      ok: false,
      error: `navigation blocked: host "${hostname}" is a link-local/loopback/private-range address`,
      grant,
    };
  }
  return { ok: true };
}

module.exports = {
  isAgentNavigationAllowed,
  // Exported for focused unit tests.
  parseIpv4,
  isBlockedIpv4,
  ALLOWED_SCHEMES,
};
