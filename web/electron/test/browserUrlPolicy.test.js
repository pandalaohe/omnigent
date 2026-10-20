// Tests for the agent-navigation URL allowlist (src/browserUrlPolicy.js),
// run with `node --test`. Pure function — no Electron needed.
//
// The security property under test: a model-issued `browser_navigate` must not
// be able to reach file://, cloud-metadata / loopback / private-range hosts,
// or any non-http(s) scheme (else it screenshots the bytes back out = SSRF +
// local-file read + exfil). Normal public https must still be allowed.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const {
  checkAgentNavigation,
  isAgentConnectionAllowed,
  isAgentNavigationAllowed,
  isProxyAddress,
  proxyHosts,
} = require("../src/browserUrlPolicy");

// Private IPv4 fixtures are built from octets so no LAN address literal enters the source.
const ip = (...octets) => octets.join(".");

describe("browserUrlPolicy — agent navigation allowlist", () => {
  it("allows only canonical loopback destinations with Arca eligibility", () => {
    for (const host of ["localhost", "127.0.0.1", "[::1]"]) {
      assert.equal(
        isAgentNavigationAllowed(`http://${host}:5173/`, { allowLocalhost: true }).ok,
        true,
      );
      assert.equal(isAgentNavigationAllowed(`https://${host}/`, { allowLocalhost: true }).ok, true);
      assert.equal(isAgentNavigationAllowed(`http://${host}/`).ok, false);
    }
    for (const url of [
      "http://app.localhost",
      "http://[::ffff:127.0.0.1]:5173/",
      "http://[::127.0.0.1]/",
      "http://127.2.3.4",
      "http://0.0.0.0",
      "http://[::]",
      "http://169.254.169.254",
      "http://10.0.0.1",
      "http://172.16.0.1",
      "http://192.168.1.1",
      "file:///tmp/a",
      "data:text/html,test",
    ]) {
      assert.equal(isAgentNavigationAllowed(url, { allowLocalhost: true }).ok, false, url);
    }
  });
  it("allows ordinary public http(s) URLs", () => {
    for (const url of [
      "https://example.com/",
      "http://example.com/path?q=1",
      "https://sub.domain.example.org/a/b",
      "https://example.com:8443/x", // non-loopback host with a port is fine
    ]) {
      assert.equal(isAgentNavigationAllowed(url).ok, true, `expected allow: ${url}`);
    }
  });

  it("rejects non-http(s) schemes (file/chrome/devtools/data/blob/javascript/about)", () => {
    for (const url of [
      "file:///home/user/.ssh/id_rsa",
      "file:///etc/passwd",
      "chrome://settings",
      "devtools://devtools/bundled/inspector.html",
      "data:text/html,<script>alert(1)</script>",
      "blob:https://example.com/uuid",
      "javascript:alert(document.cookie)",
      "about:blank",
    ]) {
      const v = isAgentNavigationAllowed(url);
      assert.equal(v.ok, false, `expected reject: ${url}`);
      assert.match(v.error, /navigation blocked/);
    }
  });

  it("blocks cloud metadata + link-local (169.254.0.0/16)", () => {
    for (const url of [
      "http://169.254.169.254/latest/meta-data/",
      "http://169.254.169.254/",
      "http://169.254.0.1/",
      "https://169.254.169.254/latest/meta-data/iam/security-credentials/",
    ]) {
      assert.equal(isAgentNavigationAllowed(url).ok, false, `expected reject: ${url}`);
    }
  });

  it("blocks loopback: localhost, *.localhost, 127.0.0.0/8, ::1", () => {
    for (const url of [
      "http://localhost/",
      "http://localhost:6767/health",
      "http://app.localhost/",
      "http://localhost./",
      "http://app.localhost./",
      "http://127.0.0.1/",
      "http://127.0.0.1:8080/admin",
      "http://127.5.6.7/",
      "http://[::1]/",
    ]) {
      assert.equal(isAgentNavigationAllowed(url).ok, false, `expected reject: ${url}`);
    }
  });

  it("blocks RFC-1918 private ranges (10/8, 172.16/12, 192.168/16)", () => {
    for (const url of [
      "http://10.0.0.1/",
      "http://10.255.255.255/",
      "http://172.16.0.1/",
      "http://172.20.10.5/",
      "http://172.31.255.255/",
      "http://192.168.0.1/",
      "http://192.168.1.100/internal",
    ]) {
      assert.equal(isAgentNavigationAllowed(url).ok, false, `expected reject: ${url}`);
    }
  });

  it("allows the 172.x hosts that are NOT in the private /12 (172.15, 172.32)", () => {
    assert.equal(isAgentNavigationAllowed("http://172.15.0.1/").ok, true);
    assert.equal(isAgentNavigationAllowed("http://172.32.0.1/").ok, true);
  });

  it("canonicalizes obfuscated IPv4 forms before checking (integer/hex/octal)", () => {
    // WHATWG URL normalizes these to 127.0.0.1 — the allowlist must still block.
    for (const url of [
      "http://2130706433/", // 127.0.0.1 as a 32-bit int
      "http://0x7f000001/", // 127.0.0.1 as hex
      "http://0177.0.0.1/", // 127.0.0.1 with an octal first octet
    ]) {
      assert.equal(isAgentNavigationAllowed(url).ok, false, `expected reject: ${url}`);
    }
  });

  it("rejects empty / malformed / non-string input without throwing", () => {
    for (const bad of ["", "   ", "not a url", "://missing-scheme", null, undefined, 42]) {
      const v = isAgentNavigationAllowed(bad);
      assert.equal(v.ok, false);
    }
  });

  it("blocks IPv6 spellings of loopback, private, and metadata addresses", () => {
    for (const url of [
      "http://[::ffff:127.0.0.1]:8080/", // serialized as [::ffff:7f00:1]
      "http://[::ffff:7f00:1]/",
      "http://[::ffff:10.0.0.1]/",
      "http://[::ffff:192.168.1.1]/",
      "http://[::ffff:169.254.169.254]/latest/meta-data/",
      "http://[::ffff:100.100.100.200]/", // CGNAT-range metadata
      "http://[::127.0.0.1]/", // IPv4-compatible, serialized as [::7f00:1]
      "http://[::]/",
      "http://[0:0:0:0:0:0:0:1]/",
      "http://[64:ff9b::127.0.0.1]/", // NAT64
      "http://[2002:7f00:1::]/", // 6to4
      "http://[2001::1]/", // Teredo
      "http://[fc00::1]/", // unique-local
      "http://[fd12::1]/",
      "http://[fe80::1]/",
      "http://[febf::1]/", // top of fe80::/10
      "http://[fec0::1]/", // site-local
      "http://[ff02::1]/", // multicast
      "http://[2001:db8::1]/",
      "http://[2001:1ff:ffff::1]/", // top of 2001::/23
      "http://[3fff::1]/", // documentation; global to ssrf.py, blocked here on purpose
      "http://[3fff:fff:ffff::1]/",
    ]) {
      assert.equal(isAgentNavigationAllowed(url).ok, false, `expected reject: ${url}`);
    }
  });

  it("blocks shared, reserved, multicast and documentation IPv4 ranges", () => {
    for (const url of [
      "http://100.64.0.1/",
      "http://100.100.100.200/",
      "http://100.127.255.255/",
      "http://192.0.0.1/",
      "http://192.0.0.9/", // anycast; global to ssrf.py, blocked here on purpose
      "http://192.0.0.255/",
      "http://192.0.2.1/",
      "http://198.18.0.1/",
      "http://198.51.100.1/",
      "http://203.0.113.1/",
      "http://224.0.0.1/",
      "http://240.0.0.1/",
      "http://255.255.255.255/",
    ]) {
      assert.equal(isAgentNavigationAllowed(url).ok, false, `expected reject: ${url}`);
    }
  });

  it("allows public IPv4 and IPv6 literals next to the blocked ranges", () => {
    // Protocol-assignment exceptions remain conservatively excluded.
    for (const host of ["2001:3::1", "2001:4:112::1"]) {
      assert.equal(isAgentNavigationAllowed(`http://[${host}]/`).ok, false, host);
    }
    for (const url of [
      "http://8.8.8.8/",
      "http://100.63.255.255/",
      "http://100.128.0.1/",
      "http://169.253.0.1/",
      "http://192.0.1.1/",
      "http://198.20.0.1/",
      "http://223.255.255.254/",
      "http://[2606:4700:4700::1111]/",
      "https://[2001:4860:4860::8888]/",
      "http://[2001:200::1]/", // first address past 2001::/23
      "http://[3fff:1000::1]/", // first address past 3fff::/20
      "http://[::ffff:8.8.8.8]/",
      "http://[64:ff9b::8.8.8.8]/", // NAT64 to a public IPv4
    ]) {
      assert.equal(isAgentNavigationAllowed(url).ok, true, `expected allow: ${url}`);
    }
  });
});

/** A resolver stub that records lookups and answers from a fixed table. */
function fakeResolver(table) {
  const lookups = [];
  const resolveHost = async (hostname) => {
    lookups.push(hostname);
    const answer = table[hostname];
    if (answer instanceof Error) throw answer;
    return answer ?? [];
  };
  return { resolveHost, lookups };
}

describe("browserUrlPolicy — DNS-aware agent navigation", () => {
  const { resolveHost, lookups } = fakeResolver({
    "lvh.me": ["127.0.0.1"],
    "127.0.0.1.nip.io": ["127.0.0.1"],
    "metadata.example": ["169.254.169.254"],
    "intranet.example": ["10.0.0.5"],
    "mapped.example": ["::ffff:127.0.0.1"],
    "ula.example": ["fd00::5"],
    "split.example": ["93.184.215.14", "192.168.1.10"],
    "example.com": ["93.184.215.14", "2606:2800:21f:cb07:6820:80da:af6b:8b2c"],
    "missing.example": new Error("ENOTFOUND"),
  });

  const verdictsFor = (hosts) =>
    Promise.all(hosts.map((host) => checkAgentNavigation(`http://${host}:8899/`, { resolveHost })));

  it("blocks a hostname if any resolved address is internal", async () => {
    const hosts = [
      "lvh.me",
      "127.0.0.1.nip.io",
      "metadata.example",
      "intranet.example",
      "mapped.example",
      "ula.example",
      "split.example",
    ];
    for (const [i, verdict] of (await verdictsFor(hosts)).entries()) {
      assert.equal(verdict.ok, false, hosts[i]);
      assert.match(verdict.error, /does not resolve to a public address/);
    }
  });

  it("fails closed when a hostname does not resolve or resolves to nothing", async () => {
    const verdicts = await verdictsFor(["missing.example", "empty.example"]);
    assert.deepEqual(
      verdicts.map((verdict) => verdict.ok),
      [false, false],
    );
  });

  it("allows a hostname whose addresses are all public", async () => {
    assert.deepEqual(await checkAgentNavigation("https://example.com/", { resolveHost }), {
      ok: true,
    });
  });

  it("does not resolve literals, rejected URLs, or the exact Arca loopback hosts", async () => {
    lookups.length = 0;
    assert.equal((await checkAgentNavigation("http://8.8.8.8/", { resolveHost })).ok, true);
    assert.equal((await checkAgentNavigation("file:///etc/passwd", { resolveHost })).ok, false);
    assert.equal((await checkAgentNavigation("http://[fd12::1]/", { resolveHost })).ok, false);
    const arca = { resolveHost, allowLocalhost: true };
    assert.equal((await checkAgentNavigation("http://localhost:5173/", arca)).ok, true);
    assert.deepEqual(lookups, []);
    // A DNS alias for loopback is not part of the Arca exception.
    assert.equal((await checkAgentNavigation("http://lvh.me:5173/", arca)).ok, false);
  });
});

describe("browserUrlPolicy — connected address check", () => {
  it("blocks a response served from an internal address", () => {
    for (const connectedAddress of [
      "127.0.0.1",
      "10.0.0.5",
      "169.254.169.254",
      "::1",
      "::ffff:7f00:1",
      "fd00::1",
    ]) {
      const verdict = isAgentConnectionAllowed("https://rebind.example/", connectedAddress);
      assert.equal(verdict.ok, false, connectedAddress);
      assert.match(verdict.error, /navigation blocked/);
    }
  });

  it("allows public addresses", () => {
    for (const connectedAddress of ["93.184.215.14", "2606:4700:4700::1111"]) {
      assert.equal(
        isAgentConnectionAllowed("https://example.com/", connectedAddress).ok,
        true,
        connectedAddress,
      );
    }
  });

  it("blocks a response that reports no connected address", () => {
    for (const connectedAddress of [undefined, ""]) {
      const verdict = isAgentConnectionAllowed("https://example.com/", connectedAddress);
      assert.equal(verdict.ok, false, String(connectedAddress));
      assert.match(verdict.error, /no connected address/);
    }
  });

  it("allows loopback only for the exact Arca hosts when eligible", () => {
    const eligible = { allowLocalhost: true };
    assert.equal(
      isAgentConnectionAllowed("http://localhost:5173/", "127.0.0.1", eligible).ok,
      true,
    );
    assert.equal(isAgentConnectionAllowed("http://[::1]:5173/", "::1", eligible).ok, true);
    assert.equal(isAgentConnectionAllowed("http://localhost:5173/", "127.0.0.1").ok, false);
    assert.equal(isAgentConnectionAllowed("http://lvh.me:5173/", "127.0.0.1", eligible).ok, false);
  });
});

describe("browserUrlPolicy — proxy address matching", () => {
  it("lists the proxy hosts of a PAC-style result", () => {
    assert.deepEqual(
      proxyHosts("PROXY proxy.corp:3128;SOCKS5 [::1]:1080; HTTPS 10.0.0.2:443 ;DIRECT"),
      ["proxy.corp", "::1", "10.0.0.2"],
    );
    assert.deepEqual(proxyHosts("DIRECT"), []);
  });

  const resolveHost = async (hostname) => ({ "proxy.corp": ["10.0.0.2"] })[hostname] ?? [];

  it("accepts an internal address only when it is a listed proxy's IP", async () => {
    const pac = "PROXY proxy.corp:3128; DIRECT";
    // Address-only match: the proxy's IP counts even if the proxy didn't carry
    // the request (the documented limit on isProxyAddress).
    const [proxyIp, directFallback, mappedForm, noAddress] = await Promise.all([
      isProxyAddress("10.0.0.2", pac, resolveHost),
      isProxyAddress("127.0.0.1", pac, resolveHost), // DIRECT fallback to loopback
      isProxyAddress("::ffff:a00:2", "PROXY 10.0.0.2:3128", resolveHost),
      isProxyAddress(undefined, pac, resolveHost),
    ]);
    assert.deepEqual([proxyIp, directFallback, mappedForm, noAddress], [true, false, true, false]);
    assert.equal(await isProxyAddress("10.0.0.2", "DIRECT", resolveHost), false);
  });
});

describe("browserUrlPolicy — canonical host bypasses", () => {
  it("refuses IPv4-in-IPv6, internal IPv6 ranges and trailing-dot localhost", () => {
    const privateRange = [
      ["http://[::ffff:127.0.0.1]:8080/", "[::ffff:7f00:1]"],
      [`http://[::ffff:${ip(10, 0, 0, 1)}]/`, "[::ffff:a00:1]"],
      ["http://[::ffff:169.254.169.254]/", "[::ffff:a9fe:a9fe]"],
      ["http://[0:0:0:0:0:ffff:7f00:1]/", "[::ffff:7f00:1]"],
      ["http://[::127.0.0.1]/", "[::7f00:1]"],
      [`http://[64:ff9b::${ip(10, 0, 0, 1)}]/`, "[64:ff9b::a00:1]"],
      ["http://[fd12::1]/", "[fd12::1]"],
      ["http://[fc00::1]/", "[fc00::1]"],
    ];
    for (const [url, hostname] of privateRange) {
      const v = isAgentNavigationAllowed(url);
      assert.equal(v.ok, false, `expected reject: ${url}`);
      assert.equal(
        v.error,
        `navigation blocked: host "${hostname}" is a link-local/loopback/private-range address`,
      );
    }

    const loopbackOrInternal = [
      ["http://[fe9a::1]/", "[fe9a::1]"],
      ["http://[fe80::1]/", "[fe80::1]"],
      ["http://[febf::1]/", "[febf::1]"],
      ["http://localhost./", "localhost."],
      ["http://localhost.:5173/", "localhost."],
      ["http://app.localhost./", "app.localhost."],
    ];
    for (const [url, hostname] of loopbackOrInternal) {
      const v = isAgentNavigationAllowed(url);
      assert.equal(v.ok, false, `expected reject: ${url}`);
      assert.equal(v.error, `navigation blocked: host "${hostname}" is a loopback/internal host`);
    }

    assert.equal(isAgentNavigationAllowed("http://[fec0::1]/").ok, false);
  });

  it("still allows public IPv6, public mapped/NAT64 IPv4 and public trailing-dot names", () => {
    for (const url of [
      "http://[2606:4700:4700::1111]/",
      "http://[::ffff:8.8.8.8]/",
      "http://[64:ff9b::8.8.8.8]/",
      "https://example.com./",
    ]) {
      assert.equal(isAgentNavigationAllowed(url).ok, true, `expected allow: ${url}`);
    }
  });
});

describe("browserUrlPolicy — allowlist", () => {
  it("never grants forbidden or malformed resolved endpoints to a listed hostname", async () => {
    const url = "http://intranet.example:8080/";
    const allowlist = ["intranet.example:8080"];
    const loopback = await checkAgentNavigation(url, {
      allowlist,
      resolveHost: async () => ["127.0.0.1"],
    });
    assert.equal(loopback.ok, true);
    await Promise.all(
      ["169.254.169.254", "0.0.0.0", "::", "fe80::1", "not-an-address"].map(async (address) => {
        const verdict = await checkAgentNavigation(url, {
          allowlist,
          resolveHost: async () => [address],
        });
        assert.equal(verdict.ok, false, address);
        assert.equal(verdict.grant, undefined, address);
        assert.equal(isAgentConnectionAllowed(url, address, { allowlist }).ok, false, address);
      }),
    );
    assert.equal(
      (await checkAgentNavigation(url, { allowlist, resolveHost: async () => [] })).ok,
      false,
    );
    assert.equal(isAgentConnectionAllowed(url, undefined, { allowlist }).ok, false);
  });

  it("allows a listed internal IPv4 on any port and refuses its neighbours", () => {
    const allowlist = [ip(192, 168, 1, 20)];
    assert.equal(
      isAgentNavigationAllowed(`http://${ip(192, 168, 1, 20)}/`, { allowlist }).ok,
      true,
    );
    assert.equal(
      isAgentNavigationAllowed(`http://${ip(192, 168, 1, 20)}:8080/`, { allowlist }).ok,
      true,
    );
    const v = isAgentNavigationAllowed(`http://${ip(192, 168, 1, 21)}/`, { allowlist });
    assert.equal(v.ok, false);
    assert.equal(
      v.error,
      `navigation blocked: host "${ip(192, 168, 1, 21)}" is a link-local/loopback/private-range address`,
    );
  });

  it("matches entry ports against the URL's effective port", () => {
    assert.equal(
      isAgentNavigationAllowed(`http://${ip(192, 168, 1, 20)}:8080/`, {
        allowlist: [`${ip(192, 168, 1, 20)}:8080`],
      }).ok,
      true,
    );
    assert.equal(
      isAgentNavigationAllowed(`https://${ip(192, 168, 1, 20)}:8080/`, {
        allowlist: [`${ip(192, 168, 1, 20)}:8080`],
      }).ok,
      true,
    );
    assert.equal(
      isAgentNavigationAllowed(`http://${ip(192, 168, 1, 20)}/`, {
        allowlist: [`${ip(192, 168, 1, 20)}:8080`],
      }).ok,
      false,
    );
    assert.equal(
      isAgentNavigationAllowed(`http://${ip(192, 168, 1, 20)}/`, {
        allowlist: [`${ip(192, 168, 1, 20)}:80`],
      }).ok,
      true,
    );
    assert.equal(
      isAgentNavigationAllowed(`https://${ip(192, 168, 1, 20)}/`, {
        allowlist: [`${ip(192, 168, 1, 20)}:80`],
      }).ok,
      false,
    );
  });

  it("masks host bits for IPv4 CIDR entries", () => {
    assert.equal(
      isAgentNavigationAllowed(`http://${ip(10, 1, 2, 3)}:3000/`, {
        allowlist: [`${ip(10, 0, 0, 0)}/8`],
      }).ok,
      true,
    );
    assert.equal(
      isAgentNavigationAllowed(`http://${ip(172, 16, 0, 1)}/`, {
        allowlist: [`${ip(10, 0, 0, 0)}/8`],
      }).ok,
      false,
    );
    assert.equal(
      isAgentNavigationAllowed(`http://${ip(192, 168, 1, 5)}/`, {
        allowlist: [`${ip(192, 168, 1, 5)}/32`],
      }).ok,
      true,
    );
    assert.equal(
      isAgentNavigationAllowed(`http://${ip(192, 168, 1, 6)}/`, {
        allowlist: [`${ip(192, 168, 1, 5)}/32`],
      }).ok,
      false,
    );
    assert.equal(
      isAgentNavigationAllowed(`http://${ip(192, 168, 1, 5)}/`, {
        allowlist: [`${ip(192, 168, 1, 77)}/24`],
      }).ok,
      true,
    );
    assert.equal(
      isAgentNavigationAllowed(`http://${ip(192, 168, 2, 5)}/`, {
        allowlist: [`${ip(192, 168, 1, 77)}/24`],
      }).ok,
      false,
    );
  });

  it("treats /0 as the whole IPv4 space but never unlocks link-local/unspecified", () => {
    const allowlist = ["0.0.0.0/0"];
    assert.equal(isAgentNavigationAllowed(`http://${ip(10, 0, 0, 1)}/`, { allowlist }).ok, true);
    assert.equal(isAgentNavigationAllowed("http://127.0.0.1/", { allowlist }).ok, true);
    assert.equal(isAgentNavigationAllowed("http://169.254.169.254/", { allowlist }).ok, false);
    assert.equal(isAgentNavigationAllowed("http://0.0.0.0/", { allowlist }).ok, false);
  });

  it("matches localhost entries by name and port, ignoring case and trailing dots", () => {
    const allowlist = ["localhost:5173"];
    assert.equal(isAgentNavigationAllowed("http://localhost:5173/", { allowlist }).ok, true);
    assert.equal(isAgentNavigationAllowed("http://LOCALHOST:5173/", { allowlist }).ok, true);
    assert.equal(isAgentNavigationAllowed("http://localhost.:5173/", { allowlist }).ok, true);
    const v = isAgentNavigationAllowed("http://localhost:5174/", { allowlist });
    assert.equal(v.ok, false);
    assert.equal(v.error, 'navigation blocked: host "localhost" is a loopback/internal host');
    assert.equal(isAgentNavigationAllowed("http://127.0.0.1:5173/", { allowlist }).ok, false);
  });

  it("matches IPv4 entries against every canonical form of the same address", () => {
    const allowlist = ["127.0.0.1"];
    for (const url of [
      "http://0x7f000001/",
      "http://2130706433/",
      "http://127.1/",
      "http://[::ffff:127.0.0.1]/",
    ]) {
      assert.equal(isAgentNavigationAllowed(url, { allowlist }).ok, true, url);
    }
    assert.equal(isAgentNavigationAllowed("http://127.0.0.2/", { allowlist }).ok, false);
    assert.equal(
      isAgentNavigationAllowed("http://127.0.0.1:3000/", { allowlist: ["0x7f000001:3000"] }).ok,
      true,
    );
  });

  it("matches IPv6 entries by canonical literal and by embedded IPv4", () => {
    assert.equal(
      isAgentNavigationAllowed("http://[::1]:8080/", { allowlist: ["[::1]:8080"] }).ok,
      true,
    );
    assert.equal(
      isAgentNavigationAllowed("http://[::1]/", { allowlist: ["[::1]:8080"] }).ok,
      false,
    );
    assert.equal(
      isAgentNavigationAllowed("http://[fd12::1]/", { allowlist: ["[FD12:0:0::1]"] }).ok,
      true,
    );
    assert.equal(
      isAgentNavigationAllowed("http://[fd12::2]/", { allowlist: ["[FD12:0:0::1]"] }).ok,
      false,
    );
    assert.equal(
      isAgentNavigationAllowed(`http://${ip(10, 0, 0, 1)}/`, {
        allowlist: [`[::ffff:${ip(10, 0, 0, 1)}]`],
      }).ok,
      true,
    );
  });

  it("keeps never-allowable addresses refused even when the allowlist names them", () => {
    const allowlist = [
      "169.254.169.254",
      "169.254.0.0/16",
      "0.0.0.0/0",
      "0.0.0.0",
      "[fe80::1]",
      "[::]",
    ];
    for (const url of [
      "http://169.254.169.254/",
      "http://[::ffff:169.254.169.254]/",
      "http://0.0.0.0/",
      "http://[fe80::1]/",
      "http://[::]/",
    ]) {
      assert.equal(isAgentNavigationAllowed(url, { allowlist }).ok, false, url);
    }
  });

  it("refuses non-http(s) schemes even when the allowlist names the host", () => {
    const allowlist = ["localhost", ip(192, 168, 1, 20)];
    for (const url of [
      "ftp://localhost/",
      `ws://${ip(192, 168, 1, 20)}/`,
      "file:///etc/hosts",
      "data:text/html,x",
    ]) {
      assert.equal(isAgentNavigationAllowed(url, { allowlist }).ok, false, url);
    }
  });

  it("ignores malformed entries without widening the policy", () => {
    const allowlist = [
      null,
      42,
      {},
      [],
      "",
      "   ",
      `${ip(192, 168, 1, 20)}:0`,
      `${ip(192, 168, 1, 20)}:65536`,
      `${ip(192, 168, 1, 20)}:8a`,
      `${ip(10, 0, 0, 0)}/33`,
      `${ip(10, 0, 0, 0)}/-1`,
      `${ip(10, 0, 0, 0)}/8:80`,
      "[fd12::]/64",
      `user@${ip(192, 168, 1, 20)}`,
      `${ip(192, 168, 1, 20)}/path`,
      `http://${ip(192, 168, 1, 20)}`,
      "*.lan",
      "999.1.1.1",
      `${ip(192, 168, 1, 20)}:8080:1`,
      "[fd12::1",
    ];
    for (const url of [
      `http://${ip(192, 168, 1, 20)}/`,
      `http://${ip(192, 168, 1, 20)}:8080/`,
      `http://${ip(10, 0, 0, 1)}/`,
      "http://[fd12::1]/",
    ]) {
      assert.equal(isAgentNavigationAllowed(url, { allowlist }).ok, false, url);
    }
  });

  it("treats a non-array allowlist as no entries", () => {
    for (const allowlist of [ip(192, 168, 1, 20), { [ip(192, 168, 1, 20)]: true }, null]) {
      assert.equal(
        isAgentNavigationAllowed(`http://${ip(192, 168, 1, 20)}/`, { allowlist }).ok,
        false,
      );
    }
  });

  it("trims entries and leaves public URLs unaffected", () => {
    assert.equal(
      isAgentNavigationAllowed(`http://${ip(192, 168, 1, 20)}/`, {
        allowlist: [`  ${ip(192, 168, 1, 20)}  `],
      }).ok,
      true,
    );
    assert.equal(
      isAgentNavigationAllowed("https://example.com/", { allowlist: [ip(192, 168, 1, 20)] }).ok,
      true,
    );
  });

  it("leaves the Arca allowLocalhost path unchanged", () => {
    assert.equal(
      isAgentNavigationAllowed("http://localhost:5173/", { allowLocalhost: true, allowlist: [] })
        .ok,
      true,
    );
    assert.equal(
      isAgentNavigationAllowed("http://[::ffff:127.0.0.1]/", { allowLocalhost: true }).ok,
      false,
    );
  });
});

describe("browserUrlPolicy — grant on internal-class refusals", () => {
  it("carries the canonical host:port a remember-me allowlist entry would use", () => {
    const cases = [
      [`http://${ip(192, 168, 1, 20)}/`, `${ip(192, 168, 1, 20)}:80`],
      [`https://[::ffff:${ip(10, 0, 0, 1)}]:8443/x`, `${ip(10, 0, 0, 1)}:8443`],
      ["http://LOCALHOST.:5173/", "localhost:5173"],
      ["https://[fd12::1]/", "[fd12::1]:443"],
      ["http://0x7f000001:3000/", "127.0.0.1:3000"],
      ["http://[::1]:8080/", "[::1]:8080"],
    ];
    for (const [url, grant] of cases) {
      const v = isAgentNavigationAllowed(url);
      assert.equal(v.ok, false, `expected reject: ${url}`);
      assert.equal(v.grant, grant, url);
      // The grant is a working allowlist entry: the same URL passes when listed.
      assert.equal(isAgentNavigationAllowed(url, { allowlist: [v.grant] }).ok, true, url);
    }
  });

  it("omits grant from never-allowable, malformed and allowed results", () => {
    for (const url of [
      "http://169.254.169.254/",
      "http://[fe80::1]/",
      "http://0.0.0.0/",
      "ftp://localhost/",
      "not a url",
    ]) {
      const v = isAgentNavigationAllowed(url);
      assert.equal(v.ok, false, `expected reject: ${url}`);
      assert.equal("grant" in v, false, `expected no grant: ${url}`);
    }
    const allowed = isAgentNavigationAllowed("https://example.com/");
    assert.equal(allowed.ok, true);
    assert.equal("grant" in allowed, false);
  });
});
