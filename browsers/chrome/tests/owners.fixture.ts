import "fake-indexeddb/auto";
import { vi } from "vitest";
import { Connection } from "../src/auth";
import { TrayStore } from "../src/store";
import { identityKey } from "../src/model";
import type { DraftItem, Identity, SessionRow, Snapshot } from "../src/model";
import { selection } from "./fixtures";

export const identity: Identity = {
  server: "https://omni.example.test/base",
  userId: "user-a",
  grantId: "grant-a",
  display: "Demo account",
};
export const target: SessionRow = {
  id: "session-a",
  title: "Demo",
  host_name: "Workstation",
  workspace: "/opt/work/demo",
  available: true,
  reason: null,
};
export const key = identityKey(identity);
export function item(id = "a".repeat(32)): DraftItem {
  return {
    choice: "without",
    wire: {
      capture_id: id,
      captured_at: "2026-10-22T01:02:03.000Z",
      kind: "element",
      note: "Make this readable",
      page: selection.page,
      target: selection.target,
      geometry: {
        rect: selection.rect,
        viewport: { width: 1000, height: 800 },
        bitmap_scale: null,
      },
    },
  };
}
export function snapshot(id = "b".repeat(32)): Snapshot {
  return {
    id,
    target,
    items: [item()],
    state: "ready",
    attempted: false,
    staged: {},
    createdAt: Date.now(),
  };
}
export function receipt(batch = "b".repeat(32), phase = "confirmed") {
  return {
    batch_id: batch,
    session_id: target.id,
    state: "accepted",
    dispatch_phase: phase,
    retry_required: phase === "ready",
    accepted_at: 1800000000,
    session_path: "https://attacker.test/",
  };
}
export function targets(receiver: SessionRow | null = target, rows = [target]) {
  return {
    sessions: rows,
    next_cursor: null,
    receiving_session_id: receiver?.id ?? null,
    receiving_revision: 1,
    receiving_session: receiver,
  };
}
/** Synthetic credentials only; no host auth/config is read. */
export function browser(connected = true, expiresAt = Date.now() + 3600000) {
  let stored: Record<string, unknown> = connected
    ? {
        annotationConnection: {
          phase: "ready",
          server: identity.server,
          identity,
          access: "synthetic-access",
          refresh: "synthetic-refresh",
          expiresAt,
        },
      }
    : {};
  const order: string[] = [];
  const local = {
    setAccessLevel: vi.fn(async () => {
      order.push("trusted");
    }),
    get: vi.fn(async () => {
      order.push("get");
      return structuredClone(stored);
    }),
    set: vi.fn(async (value: Record<string, unknown>) => {
      order.push("set");
      stored = { ...stored, ...structuredClone(value) };
    }),
    remove: vi.fn(async (name: string) => {
      delete stored[name];
    }),
  };
  const event = () => ({ addListener: vi.fn(), removeListener: vi.fn() });
  const chrome = {
    storage: { local },
    permissions: { contains: vi.fn(async () => true), request: vi.fn(async () => true) },
    tabs: {
      create: vi.fn(async () => ({})),
      onActivated: event(),
      onUpdated: event(),
      onRemoved: event(),
      query: vi.fn(async () => [{ id: 7 }]),
      sendMessage: vi.fn(async (_tab: number, _message: { generation: string }) => ({ ok: true })),
      captureVisibleTab: vi.fn(),
    },
    runtime: {
      id: "extension",
      getURL: (path: string) => `chrome-extension://extension/${path}`,
      onMessage: event(),
      onConnect: event(),
    },
    action: { onClicked: event() },
    scripting: { executeScript: vi.fn(async () => [{ frameId: 0, documentId: "doc-a" }]) },
    sidePanel: { open: vi.fn(async () => {}) },
    windows: {
      onFocusChanged: event(),
      get: vi.fn(async () => ({ focused: true })),
      update: vi.fn(),
    },
  };
  vi.stubGlobal("chrome", chrome);
  const requests: { url: string; init: RequestInit }[] = [];
  const replies: ((url: string, init: RequestInit) => unknown | Promise<unknown>)[] = [];
  const fetcher = vi.fn(async (input: string, init: RequestInit) => {
    requests.push({ url: input, init });
    const next = replies.shift();
    if (!next) throw new Error("Unexpected synthetic request");
    const body = await next(input, init);
    return body instanceof Response ? body : Response.json(body);
  });
  vi.stubGlobal("fetch", fetcher);
  return {
    chrome,
    local,
    order,
    requests,
    replies,
    fetcher,
    stored: () => structuredClone(stored),
  };
}
export async function owners() {
  const fake = browser();
  const connection = new Connection();
  await connection.initialize();
  const store = new TrayStore(`test-${crypto.randomUUID()}`);
  await store.ensure(identity);
  return { ...fake, connection, store };
}
export async function seed(store: TrayStore, batch = snapshot()) {
  await store.change([key], (parts) => {
    parts[0]!.target = target;
    parts[0]!.batches.push(batch);
  });
}
