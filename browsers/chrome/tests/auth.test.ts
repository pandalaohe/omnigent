import { afterEach, describe, expect, it, vi } from "vitest";
import { Connection } from "../src/auth";
import { canonicalServer, deliveryCheckLink, originPermission, sessionLink } from "../src/server";
import { browser, identity } from "./owners.fixture";

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});
const authorization = (url = `${identity.server}/oauth/device?user_code=ABCD`) => ({
  device_code: "synthetic-device",
  user_code: "ABCD",
  verification_uri_complete: url,
  expires_in: 600,
  interval: 5,
});
const token = () => ({
  access_token: "synthetic-access-next",
  refresh_token: "synthetic-refresh-next",
  expires_in: 3600,
  grant_id: identity.grantId,
  user_id: identity.userId,
  display_identity: identity.display,
  protocol_version: 1,
});

describe("chosen HTTPS server", () => {
  it("keeps a normalized basepath and constructs independent first-party links", () => {
    expect(canonicalServer("https://OMNI.example.test:443/base/")).toBe(identity.server);
    expect(originPermission(identity.server)).toBe("https://omni.example.test/*");
    expect(sessionLink(identity.server, "a/b")).toBe("https://omni.example.test/base/c/a%2Fb");
    expect(deliveryCheckLink(identity.server)).toBe(
      "https://omni.example.test/base/settings/account#connected-apps-heading",
    );
  });
  it.each([
    "http://example.test",
    "https://u:p@example.test",
    "https://example.test/?x=1",
    "https://example.test/#x",
    "https://example.test/a%2fb",
  ])("rejects ambiguous server %s", (url) => expect(() => canonicalServer(url)).toThrow());
});
describe("worker-only connection owner", () => {
  it("matches the server's label bounds and explains disabled pairing without a fallback", async () => {
    const f = browser(false);
    const c = new Connection();
    await c.initialize();
    await expect(c.begin(identity.server, "x".repeat(81))).rejects.toThrow("1–80");
    await expect(c.begin(identity.server, "browser\nlabel")).rejects.toThrow("control");
    expect(f.requests).toHaveLength(0);
    f.replies.push(() => new Response("Not found", { status: 404 }));
    await expect(c.begin(identity.server, "Browser")).rejects.toThrow("pairing is unavailable");
    expect(c.view().status).toBe("disconnected");
  });
  it("restricts storage first and pairs through exact basepath consent with bounded polling", async () => {
    vi.useFakeTimers();
    const f = browser(false);
    const c = new Connection();
    await c.initialize();
    expect(f.order).toEqual(["trusted", "get"]);
    f.replies.push(() => authorization());
    await c.begin(identity.server, " Browser ");
    expect(JSON.parse(String(f.requests[0]!.init.body))).toEqual({ label: "Browser" });
    expect(c.view()).toMatchObject({
      status: "pairing",
      code: "ABCD",
      consentUrl: `${identity.server}/oauth/device?user_code=ABCD`,
    });
    expect(JSON.stringify(c.view())).not.toContain("synthetic-device");
    expect(await c.poll()).toBe(false);
    expect(f.requests).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(5000);
    f.replies.push(() => Response.json({ error: "slow_down" }, { status: 400 }));
    expect(await c.poll()).toBe(false);
    expect(c.view().pollAfter).toBe(Date.now() + 10000);
    await vi.advanceTimersByTimeAsync(10000);
    f.replies.push(() => token());
    expect(await c.poll()).toBe(true);
    expect(c.identity()).toEqual(identity);
    const form = f.requests[2]!.init.body as URLSearchParams;
    expect(Array.from(form.entries())).toEqual([
      ["grant_type", "urn:ietf:params:oauth:grant-type:device_code"],
      ["device_code", "synthetic-device"],
    ]);
    for (const request of f.requests)
      expect(request.init).toMatchObject({ credentials: "omit", redirect: "error" });
    expect(JSON.stringify(c.view())).not.toContain("synthetic-access");
  });
  it.each([
    "https://evil.test/base/oauth/device?user_code=ABCD",
    `${identity.server}/settings?user_code=ABCD`,
    `${identity.server}/oauth/device?user_code=WRONG`,
    `${identity.server}/oauth/device?user_code=ABCD&extra=1`,
    `${identity.server}/oauth/device?user_code=ABCD#x`,
  ])("refuses a mismatched consent destination %s", async (url) => {
    const f = browser(false);
    const c = new Connection();
    await c.initialize();
    f.replies.push(() => authorization(url));
    await expect(c.begin(identity.server, "Browser")).rejects.toThrow("Consent");
    expect(c.view().status).toBe("disconnected");
  });
  it("expires device pairing without a token request", async () => {
    vi.useFakeTimers();
    const f = browser(false);
    const c = new Connection();
    await c.initialize();
    f.replies.push(() => authorization());
    await c.begin(identity.server, "Browser");
    await vi.advanceTimersByTimeAsync(600000);
    await expect(c.poll()).rejects.toThrow("expired");
    expect(f.requests).toHaveLength(1);
    expect(c.view().status).toBe("disconnected");
  });
  it("persists refresh intent before rotation, shares it, and retains verified identity", async () => {
    const f = browser(true, 0);
    const c = new Connection();
    await c.initialize();
    f.replies.push(
      () => {
        expect((f.stored().annotationConnection as { phase: string }).phase).toBe("refreshing");
        return {
          access_token: "synthetic-rotated-access",
          refresh_token: "synthetic-rotated-refresh",
          expires_in: 3600,
        };
      },
      () => ({}),
      () => ({}),
    );
    await Promise.all([
      c.request(identity, "one", { method: "POST" }),
      c.request(identity, "two", { method: "POST" }),
    ]);
    expect(f.requests.filter((r) => r.url.endsWith("/oauth/token"))).toHaveLength(1);
    expect(c.identity()).toEqual(identity);
    expect(c.view().status).toBe("connected");
  });
  it("never reuses a rotating refresh after a lost acknowledgement or worker restart", async () => {
    const f = browser(true, 0);
    const c = new Connection();
    await c.initialize();
    f.replies.push(() => {
      throw new Error("lost ACK");
    });
    await expect(c.request(identity, "one", {})).rejects.toThrow();
    expect(c.view().status).toBe("unknown");
    const restarted = new Connection();
    await restarted.initialize();
    await expect(restarted.request(identity, "one", {})).rejects.toThrow("Reconnect");
    expect(f.requests).toHaveLength(1);
  });
  it("treats a durable in-flight refresh marker as Unknown on a fresh worker", async () => {
    const f = browser(true, 0);
    const c = new Connection();
    await c.initialize();
    let reject!: (error: Error) => void;
    f.replies.push(
      () =>
        new Promise((_resolve, fail) => {
          reject = fail;
        }),
    );
    const pending = expect(c.request(identity, "one", {})).rejects.toThrow();
    await vi.waitFor(() => expect(f.requests).toHaveLength(1));
    const restarted = new Connection();
    await restarted.initialize();
    expect(restarted.view().status).toBe("unknown");
    await expect(restarted.request(identity, "one", {})).rejects.toThrow("Reconnect");
    reject(new Error("terminated worker"));
    await pending;
    expect(f.requests).toHaveLength(1);
  });
  it("does not report local disconnect when removing trusted credentials fails", async () => {
    const f = browser();
    const c = new Connection();
    await c.initialize();
    f.local.remove.mockRejectedValueOnce(new Error("storage unavailable"));
    await expect(c.disconnect(true)).rejects.toThrow("storage unavailable");
    expect(c.view().status).toBe("connected");
    expect(f.requests).toHaveLength(0);
  });
  it("does not request anything when the refresh-intent commit fails", async () => {
    const f = browser(true, 0);
    const c = new Connection();
    await c.initialize();
    f.local.set.mockRejectedValueOnce(new Error("quota"));
    await expect(c.request(identity, "one", {})).rejects.toThrow("quota");
    expect(f.requests).toHaveLength(0);
    await expect(c.request(identity, "one", {})).rejects.toThrow();
  });
  it("refuses removed optional permission, marks revoked auth, and accepts explicit revoke ACK", async () => {
    const f = browser();
    const c = new Connection();
    await c.initialize();
    f.chrome.permissions.contains.mockResolvedValueOnce(false);
    await expect(c.request(identity, "one", {})).rejects.toThrow("permission");
    expect(f.requests).toHaveLength(0);
    f.replies.push(() => Response.json({ detail: { code: "grant_revoked" } }, { status: 401 }));
    await expect(c.request(identity, "one", {})).rejects.toThrow("401");
    expect(c.view().status).toBe("reconnect");
    await expect(c.disconnect(false)).rejects.toThrow("settings");
    expect((await c.disconnect(true)).closed).toBe(false);
    const f2 = browser();
    const c2 = new Connection();
    await c2.initialize();
    f2.replies.push(() => ({ revoked: true }));
    expect((await c2.disconnect(false)).closed).toBe(true);
    expect(c2.view().status).toBe("disconnected");
    expect(f2.requests[0]!.url).toBe(`${identity.server}/oauth/revoke`);
  });
  it("rejects redirect responses and aborts requests after fifteen seconds", async () => {
    vi.useFakeTimers();
    const f = browser();
    const c = new Connection();
    await c.initialize();
    f.replies.push(() => {
      const response = Response.json({});
      Object.defineProperty(response, "redirected", { value: true });
      return response;
    });
    await expect(c.request(identity, "one", {})).rejects.toThrow("unavailable");
    f.replies.push(
      (_url, init) =>
        new Promise((_resolve, reject) =>
          init.signal!.addEventListener("abort", () => reject(new Error("aborted"))),
        ),
    );
    const pending = expect(c.request(identity, "two", {})).rejects.toThrow("unavailable");
    await vi.advanceTimersByTimeAsync(15000);
    await pending;
  });
});
