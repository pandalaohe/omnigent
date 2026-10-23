import { endpoint, canonicalServer, originPermission } from "./server";
import { identityKey } from "./model";
import type { AuthView, Identity } from "./model";
import { record } from "./protocol";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    readonly captureIds: string[] = [],
  ) {
    super(`Server refused the request (${status}: ${code}).`);
  }
}
interface StoredAuth {
  phase: "pairing" | "ready" | "refreshing" | "unknown" | "reconnect";
  server: string;
  identity?: Identity;
  access?: string;
  refresh?: string;
  expiresAt: number;
  pending?: {
    device: string;
    code: string;
    url: string;
    interval: number;
    next: number;
    label: string;
  };
}
const text = (value: unknown, max = 200): string => {
  if (typeof value !== "string" || !value || value.length > max)
    throw new Error("Invalid server response.");
  return value;
};
const credential = (value: unknown): string => text(value, 16000);

/** Worker-only owner. Public views and ports never contain token/device-code values. */
export class Connection {
  private state: StoredAuth | undefined;
  private refreshing: Promise<void> | undefined;
  async initialize(): Promise<void> {
    await chrome.storage.local.setAccessLevel({ accessLevel: "TRUSTED_CONTEXTS" });
    const stored = await chrome.storage.local.get("annotationConnection");
    this.state = stored.annotationConnection as StoredAuth | undefined;
    if (this.state?.phase === "refreshing") {
      this.state.phase = "unknown";
      delete this.state.access;
      delete this.state.refresh;
      await this.persist();
    }
  }
  view(): AuthView {
    const state = this.state;
    if (!state) return { status: "disconnected" };
    return {
      status:
        state.phase === "ready"
          ? "connected"
          : state.phase === "refreshing"
            ? "unknown"
            : state.phase,
      identity: state.identity,
      server: state.server,
      ...(state.pending
        ? {
            code: state.pending.code,
            consentUrl: state.pending.url,
            pollAfter: state.pending.next,
            expiresAt: state.expiresAt,
            label: state.pending.label,
          }
        : {}),
    };
  }
  identity(): Identity | undefined {
    return this.state?.identity;
  }
  private async persist(): Promise<void> {
    if (this.state) await chrome.storage.local.set({ annotationConnection: this.state });
    else await chrome.storage.local.remove("annotationConnection");
  }
  private async exchange(server: string, route: string, init: RequestInit): Promise<unknown> {
    if (!(await chrome.permissions.contains({ origins: [originPermission(server)] })))
      throw new Error("Server permission was removed. Connect again.");
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(endpoint(server, route), {
        ...init,
        credentials: "omit",
        redirect: "error",
        signal: controller.signal,
      });
      if (response.redirected || (response.url && response.url !== endpoint(server, route)))
        throw new Error("Server redirects are refused.");
      let body: unknown;
      try {
        body = response.status === 204 ? {} : await response.json();
      } catch {
        if (!response.ok) throw new ApiError(response.status, "request_rejected");
        throw new Error("Invalid server response.");
      }
      if (!response.ok) {
        const object = record(body);
        const detail = typeof object.detail === "object" ? record(object.detail) : object;
        const raw = detail.code ?? detail.error ?? object.error;
        const code =
          typeof raw === "string" && /^[a-z_]{1,80}$/.test(raw) ? raw : "request_rejected";
        const ids = Array.isArray(detail.capture_ids)
          ? detail.capture_ids.filter(
              (id): id is string => typeof id === "string" && /^[0-9a-f]{32}$/.test(id),
            )
          : [];
        throw new ApiError(response.status, code, ids);
      }
      return body;
    } catch (error) {
      if (error instanceof ApiError) throw error;
      throw new Error(
        "Server response unavailable. Check the connection; no automatic retry was made.",
      );
    } finally {
      clearTimeout(timer);
    }
  }
  async begin(input: string, label: string): Promise<void> {
    if (this.state) throw new Error("Disconnect the current connection before pairing again.");
    const server = canonicalServer(input);
    const browserLabel = label.trim();
    if (
      !browserLabel ||
      browserLabel.length > 80 ||
      Array.from(browserLabel).some((char) => char.charCodeAt(0) < 32 || char.charCodeAt(0) === 127)
    )
      throw new Error("Enter a browser label of 1–80 characters without control characters.");
    let response: unknown;
    try {
      response = await this.exchange(server, "oauth/annotations/authorize", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ label: browserLabel }),
      });
    } catch (error) {
      if (error instanceof ApiError && [404, 503].includes(error.status))
        throw new Error(
          "Browser pairing is unavailable on this server. Check accounts mode and annotation-grant configuration.",
        );
      throw error;
    }
    const result = record(response);
    const url = new URL(text(result.verification_uri_complete, 4000));
    const code = text(result.user_code, 32);
    const consent = endpoint(server, "oauth/device");
    if (
      url.origin !== new URL(server).origin ||
      url.username ||
      url.password ||
      url.hash ||
      url.pathname !== new URL(consent).pathname ||
      Array.from(url.searchParams.keys()).some((key) => key !== "user_code") ||
      url.searchParams.getAll("user_code").length !== 1 ||
      url.searchParams.get("user_code") !== code
    )
      throw new Error("Consent page does not match the chosen server and pairing code.");
    if (result.expires_in !== 600 || result.interval !== 5)
      throw new Error("Unsupported pairing protocol.");
    this.state = {
      phase: "pairing",
      server,
      expiresAt: Date.now() + 600000,
      pending: {
        device: credential(result.device_code),
        code,
        url: url.href,
        interval: 5000,
        next: Date.now() + 5000,
        label: browserLabel,
      },
    };
    await this.persist();
  }
  async poll(): Promise<boolean> {
    const state = this.state;
    if (!state || state.phase !== "pairing" || !state.pending)
      throw new Error("Start a new connection first.");
    if (Date.now() >= state.expiresAt) {
      this.state = undefined;
      await this.persist();
      throw new Error("Pairing code expired. Connect again.");
    }
    if (Date.now() < state.pending.next) return false;
    state.pending.next = Date.now() + state.pending.interval;
    await this.persist();
    try {
      const result = record(
        await this.exchange(state.server, "oauth/annotations/token", {
          method: "POST",
          body: new URLSearchParams({
            grant_type: "urn:ietf:params:oauth:grant-type:device_code",
            device_code: state.pending.device,
          }),
        }),
      );
      if (result.protocol_version !== 1 || result.expires_in !== 3600)
        throw new Error("Unsupported annotation protocol.");
      const identity: Identity = {
        server: state.server,
        userId: text(result.user_id),
        grantId: text(result.grant_id),
        display: text(result.display_identity),
      };
      this.state = {
        phase: "ready",
        server: state.server,
        identity,
        access: credential(result.access_token),
        refresh: credential(result.refresh_token),
        expiresAt: Date.now() + 3600000,
      };
      await this.persist();
      return true;
    } catch (error) {
      if (
        error instanceof ApiError &&
        ["authorization_pending", "slow_down"].includes(error.code)
      ) {
        if (error.code === "slow_down") {
          state.pending.interval += 5000;
          state.pending.next = Date.now() + state.pending.interval;
          await this.persist();
        }
        return false;
      }
      // Redemption may have succeeded. Never replay an ambiguous device exchange.
      if (this.state) {
        this.state.phase = "unknown";
        delete this.state.pending;
        delete this.state.access;
        delete this.state.refresh;
        await this.persist().catch(() => undefined);
      }
      throw error;
    }
  }
  private async token(identity: Identity): Promise<string> {
    const current = this.state;
    if (!current?.identity || identityKey(current.identity) !== identityKey(identity))
      throw new Error("Connection identity changed. Review this tray again.");
    if (current.phase === "refreshing" && this.refreshing) await this.refreshing;
    else if (current.phase !== "ready")
      throw new Error("Reconnect required. Local notes are retained.");
    else if (current.expiresAt <= Date.now() + 30000) {
      this.refreshing = this.refresh(current);
      try {
        await this.refreshing;
      } finally {
        this.refreshing = undefined;
      }
    }
    if (this.state !== current || current.phase !== "ready" || !current.access)
      throw new Error("Reconnect required.");
    return current.access;
  }
  private async refresh(state: StoredAuth): Promise<void> {
    state.phase = "refreshing";
    // Persist intent before consuming the rotating refresh token.
    await this.persist();
    try {
      const result = record(
        await this.exchange(state.server, "oauth/token", {
          method: "POST",
          body: new URLSearchParams({
            grant_type: "refresh_token",
            refresh_token: state.refresh ?? "",
          }),
        }),
      );
      state.access = credential(result.access_token);
      state.refresh = credential(result.refresh_token);
      if (result.expires_in !== 3600) throw new Error("Unsupported access lifetime.");
      state.expiresAt = Date.now() + 3600000;
      state.phase = "ready";
      await this.persist();
    } catch (error) {
      state.phase = "unknown";
      delete state.access;
      delete state.refresh;
      await this.persist().catch(() => undefined);
      throw error;
    }
  }
  async request(identity: Identity, route: string, init: RequestInit): Promise<unknown> {
    const token = await this.token(identity);
    try {
      return await this.exchange(identity.server, route, {
        ...init,
        headers: { ...init.headers, Authorization: `Bearer ${token}` },
      });
    } catch (error) {
      if (error instanceof ApiError && error.status === 401 && this.state) {
        this.state.phase = "reconnect";
        await this.persist();
      }
      throw error;
    }
  }
  async disconnect(localOnly: boolean): Promise<{ identity?: Identity; closed: boolean }> {
    const current = this.state;
    if (!current) return { closed: false };
    let closed = false;
    if (!localOnly) {
      if (current.phase !== "ready" || !current.refresh)
        throw new Error(
          "Revoke this app in Omnigent settings, or explicitly disconnect locally while keeping notes.",
        );
      await this.exchange(current.server, "oauth/revoke", {
        method: "POST",
        body: new URLSearchParams({ token: current.refresh }),
      });
      closed = true;
    }
    this.state = undefined;
    try {
      await this.persist();
    } catch (error) {
      this.state = current;
      if (closed) current.phase = "reconnect";
      throw error;
    }
    return { identity: current.identity, closed };
  }
}
