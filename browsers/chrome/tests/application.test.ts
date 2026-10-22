import { afterEach, describe, expect, it, vi } from "vitest";
import { AnnotationApp } from "../src/application";
import { identityKey } from "../src/model";
import type { CapturePreview } from "../src/protocol";
import {
  identity,
  item,
  key,
  owners,
  receipt,
  seed,
  snapshot,
  target,
  targets,
} from "./owners.fixture";
import { selection } from "./fixtures";

afterEach(() => vi.unstubAllGlobals());
async function fixture() {
  const f = await owners();
  const changed = vi.fn();
  const app = new AnnotationApp(f.store, f.connection, changed);
  await app.initialize();
  const context = async () => {
    const part = (await app.view()).current!;
    return { key: part.key, revision: part.revision, expectedTarget: part.target?.id ?? null };
  };
  const edit = async (command: Record<string, unknown>) =>
    app.handle({ ...(await context()), ...command });
  const list = async (result = targets()) => {
    f.replies.push(() => result);
    await app.handle({ type: "list", query: "", cursor: "" });
  };
  return { ...f, app, changed, context, edit, list };
}
function preview(id = "a".repeat(32)): CapturePreview {
  return {
    capture_id: id,
    captured_at: "2026-10-22T01:02:03.000Z",
    selection,
    screenshotFailure: "Synthetic capture unavailable",
  };
}

describe("account-bound annotation application", () => {
  it("keeps nullable metadata rows and the unavailable pinned receiver in a mixed target page", async () => {
    const f = await fixture();
    const noHost = {
      ...target,
      id: "hostless",
      host_name: null,
      available: false,
      reason: "host_missing",
    };
    const noWorkspace = { ...target, id: "no-workspace", workspace: null };
    const pinned = {
      ...target,
      id: "receiver",
      host_name: null,
      workspace: null,
      available: false,
      reason: "offline",
    };
    await f.list(targets(pinned, [noHost, noWorkspace, target]));
    const view = await f.app.view();
    expect(view.targets.sessions).toEqual([noHost, noWorkspace, target]);
    expect(view.targets.receiving_session).toEqual(pinned);
    expect(view.current!.target).toEqual(pinned);
    await f.app.capture(preview(), identity);
    await f.list(targets(target, [noHost, noWorkspace, target]));
    expect((await f.app.view()).current!.target).toEqual(pinned);
    await f.edit({ type: "target", sessionId: noWorkspace.id });
    expect((await f.app.view()).current!.target).toEqual(noWorkspace);
  });
  it("uses only a valid explicitly remembered same-account target when no receiver is set", async () => {
    const f = await fixture();
    await f.list(targets(null));
    expect((await f.app.view()).current!.target).toBeNull();
    const old = { ...identity, grantId: "old-grant" };
    await f.store.ensure(old);
    await f.store.change([identityKey(old)], (parts) => {
      parts[0]!.rememberedTarget = { id: target.id, at: 1 };
    });
    f.replies.push(() => targets(null));
    await f.app.handle({ type: "list", query: "Demo", cursor: "page-two" });
    expect((await f.app.view()).current!.target!.id).toBe(target.id);
    expect(f.requests[1]!.url).toBe(
      "https://omni.example.test/base/v1/browser-annotations/sessions?query=Demo&cursor=page-two&limit=50",
    );
  });
  it("pins an offline receiver without fallback and keeps a nonempty tray target across receiving revisions", async () => {
    const f = await fixture();
    const offline = { ...target, available: false, reason: "offline" };
    await f.list(targets(offline));
    expect((await f.app.view()).current!.target).toEqual(offline);
    await f.app.capture(preview(), identity);
    await f.edit({ type: "note", id: "a".repeat(32), note: "change" });
    await f.edit({ type: "without", id: "a".repeat(32) });
    await expect(f.edit({ type: "send" })).rejects.toThrow("available");
    const next = { ...target, id: "session-b", title: "New receiver" };
    await f.list(targets(next, [target, next]));
    expect((await f.app.view()).current!.target!.id).toBe(target.id);
    expect((await f.app.view()).targets.receiving_session_id).toBe(next.id);
    await f.edit({ type: "target", sessionId: next.id });
    expect((await f.app.view()).current!.target!.id).toBe(next.id);
    expect(f.requests.every((r) => r.init.method === "POST" && r.url.includes("/sessions?"))).toBe(
      true,
    );
  });
  it("requires explicit screenshot omission and nonempty saved notes, freezes the displayed target, and keeps new captures separate", async () => {
    const f = await fixture();
    await f.list();
    await f.app.capture(preview(), identity);
    await expect(f.edit({ type: "send" })).rejects.toThrow("screenshot failure");
    await f.edit({ type: "without", id: "a".repeat(32) });
    await expect(f.edit({ type: "send" })).rejects.toThrow("item");
    const stale = await f.context();
    await f.edit({ type: "note", id: "a".repeat(32), note: "Make the label clear" });
    await expect(f.app.handle({ ...stale, type: "send" })).rejects.toThrow("changed");
    await expect(
      f.app.handle({ ...(await f.context()), expectedTarget: "wrong", type: "send" }),
    ).rejects.toThrow("changed");
    let resolve!: (value: unknown) => void;
    f.replies.push(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    await f.edit({ type: "send" });
    await vi.waitFor(() => expect(f.requests).toHaveLength(2));
    const frozen = (await f.store.all())[0]!.batches[0]!;
    expect(frozen.target.id).toBe(target.id);
    expect(frozen.body!.items[0]!.geometry.bitmap_scale).toBeNull();
    await f.app.capture(preview("c".repeat(32)), identity);
    expect((await f.app.view()).current!.items).toHaveLength(1);
    expect((await f.app.view()).current!.batches[0]!.items[0]!.wire.note).toBe(
      "Make the label clear",
    );
    resolve(receipt(frozen.id));
    await vi.waitFor(async () =>
      expect((await f.store.all())[0]!.batches[0]!.state).toBe("accepted"),
    );
    expect((await f.app.view()).current!.items[0]!.wire.capture_id).toBe("c".repeat(32));
  });
  it("keeps capture input visible on failed native storage and acknowledges Saved only after commit", async () => {
    const f = await fixture();
    const prototype = IDBObjectStore.prototype;
    const original = prototype.put;
    const fault = vi.spyOn(prototype, "put").mockImplementation(function (
      this: IDBObjectStore,
      ...args: Parameters<IDBObjectStore["put"]>
    ) {
      const request = original.apply(this, args);
      this.transaction.abort();
      return request;
    });
    await expect(f.app.capture(preview(), identity)).rejects.toThrow("not saved");
    fault.mockRestore();
    expect((await f.app.view()).current!.items).toEqual([]);
    expect(f.changed).not.toHaveBeenCalled();
    await f.app.capture(preview(), identity);
    expect(f.changed.mock.calls.at(-1)![0].message).toContain("Saved");
    await expect(f.app.capture(preview(), identity)).rejects.toThrow("already saved");
  });
  it("edits, reorders, deletes and replaces a capture while preserving the saved note on failed recapture", async () => {
    const f = await fixture();
    await f.app.capture(preview(), identity);
    await f.app.capture(preview("c".repeat(32)), identity);
    await f.edit({ type: "note", id: "a".repeat(32), note: "Keep original note" });
    await f.edit({ type: "move", id: "a".repeat(32), delta: 1 });
    expect((await f.app.view()).current!.items.map((i) => i.wire.capture_id)).toEqual([
      "c".repeat(32),
      "a".repeat(32),
    ]);
    await f.edit({ type: "recapture", id: "a".repeat(32) });
    await expect(f.app.capture(preview("d".repeat(32)), identity)).rejects.toThrow("original note");
    expect((await f.app.view()).current!.items[1]!.wire.note).toBe("Keep original note");
    await f.app.capture(
      {
        ...preview("e".repeat(32)),
        image: "data:image/png;base64,AQID",
        geometry: { ...item().wire.geometry, bitmap_scale: { x: 1.5, y: 1.25 } },
        screenshotFailure: undefined,
      },
      identity,
    );
    expect((await f.app.view()).current!.items[1]!.wire.note).toBe("Keep original note");
    await f.edit({ type: "without", id: "e".repeat(32) });
    expect((await f.app.view()).current!.items[1]!.wire.geometry.bitmap_scale).toEqual({
      x: 1.5,
      y: 1.25,
    });
    await f.edit({ type: "delete", id: "c".repeat(32) });
    expect((await f.app.view()).current!.items).toHaveLength(1);
  });
  it("strips default URLs, binds trusted full-URL consent to the partition and preserves the preview in a snapshot", async () => {
    const f = await fixture();
    await f.list();
    const full = {
      ...preview(),
      selection: {
        ...selection,
        page: {
          ...selection.page,
          url: "https://user:pass@example.test/page?filter=active#detail",
          include_query_fragment: true,
        },
      },
    };
    await f.app.capture(full, identity);
    expect((await f.app.view()).current!.items[0]!.wire.page).toMatchObject({
      url: "https://example.test/page",
      include_query_fragment: false,
    });
    await f.edit({ type: "delete", id: "a".repeat(32) });
    await f.edit({ type: "fullUrl", enabled: true });
    expect(f.app.fullUrlEnabled()).toBe(true);
    await f.app.capture({ ...full, urlConsentKey: key, ownerKey: key }, identity);
    expect((await f.app.view()).current!.items[0]!.wire.page.url).toBe(
      "https://example.test/page?filter=active#detail",
    );
    await f.edit({ type: "note", id: "a".repeat(32), note: "Keep filtering context" });
    await f.edit({ type: "without", id: "a".repeat(32) });
    f.replies.push(() => {
      throw new Error("lost ACK");
    });
    await f.edit({ type: "send" });
    await vi.waitFor(async () =>
      expect((await f.store.all())[0]!.batches[0]!.state).toBe("delivery_unknown"),
    );
    expect((await f.store.all())[0]!.batches[0]!.body!.items[0]!.page).toMatchObject({
      url: "https://example.test/page?filter=active#detail",
      include_query_fragment: true,
    });
    await f.app.capture(
      { ...full, capture_id: "c".repeat(32), urlConsentKey: "other-partition" },
      identity,
    );
    expect((await f.app.view()).current!.items[0]!.wire.page.url).toBe("https://example.test/page");
    await f.app.capture({ ...full, capture_id: "d".repeat(32), urlConsentKey: key }, identity);
    await f.edit({ type: "stripUrl", id: "d".repeat(32) });
    expect((await f.app.view()).current!.items[1]!.wire.page.url).toBe("https://example.test/page");
    await expect(f.app.capture({ ...full, ownerKey: "other-account" }, identity)).rejects.toThrow(
      "another connection",
    );
  });
  it("moves only explicitly confirmed same-account drafts with fresh capture IDs and no staged reference reuse", async () => {
    const f = await fixture();
    const old = { ...identity, grantId: "old-grant" };
    const oldKey = identityKey(old);
    await f.store.ensure(old);
    await f.store.change([oldKey], (parts) => {
      parts[0]!.items = [item()];
      parts[0]!.items[0]!.wire.screenshot = { file_id: "d".repeat(32), sha256: "0".repeat(64) };
    });
    await expect(
      f.edit({ type: "recover", oldKey, batchId: "draft", confirm: "wrong" }),
    ).rejects.toThrow("Confirm moving");
    await f.edit({ type: "recover", oldKey, batchId: "draft", confirm: "MOVE old-grant" });
    const view = await f.app.view();
    expect(view.current!.items[0]!.wire.capture_id).not.toBe("a".repeat(32));
    expect(view.current!.items[0]!.wire).not.toHaveProperty("screenshot");
    expect(view.old[0]!.items).toEqual([]);
    expect(f.requests).toHaveLength(0);
    const alien = { ...old, userId: "another-user" };
    await f.store.ensure(alien);
    await f.store.change([identityKey(alien)], (parts) => {
      parts[0]!.items = [item()];
    });
    await expect(
      f.edit({
        type: "recover",
        oldKey: identityKey(alien),
        batchId: "draft",
        confirm: "MOVE old-grant",
      }),
    ).rejects.toThrow("same verified");
    expect((await f.app.view()).old.every((part) => part.identity.userId === identity.userId)).toBe(
      true,
    );
  });
  it("requires closed-old-grant exact not-admitted confirmation and distinguishes explicit possible-duplicate copies", async () => {
    const f = await fixture();
    const old = { ...identity, grantId: "old-grant" };
    const oldKey = identityKey(old);
    await f.store.ensure(old);
    const batch = { ...snapshot(), state: "delivery_unknown" as const, attempted: true };
    await f.store.change([oldKey], (parts) => {
      parts[0]!.batches = [batch];
    });
    const command = {
      type: "recover",
      oldKey,
      batchId: batch.id,
      confirm: `NOT ADMITTED old-grant ${batch.id}`,
    };
    await expect(f.edit(command)).rejects.toThrow("Close the old grant");
    await f.app.handle({ type: "closed", oldKey, confirm: "CLOSED old-grant" });
    await expect(f.edit({ ...command, confirm: "unknown" })).rejects.toThrow("not-admitted");
    await f.edit(command);
    expect((await f.app.view()).old[0]!.batches[0]!.state).toBe("moved");
    await seed(f.store);
    await expect(
      f.edit({ type: "copy", oldKey: key, batchId: snapshot().id, confirm: "wrong" }),
    ).rejects.toThrow("duplicate");
    await f.edit({
      type: "copy",
      oldKey: key,
      batchId: snapshot().id,
      confirm: `COPY ${snapshot().id}`,
    });
    const ids = (await f.app.view()).current!.items.map((i) => i.wire.capture_id);
    expect(new Set(ids).size).toBe(2);
    expect(ids).not.toContain("a".repeat(32));
    expect(f.requests).toHaveLength(0);
  });
  it("requires explicit disconnect data choices, keeps unsent notes by default, and resets URL consent for a new grant", async () => {
    const f = await fixture();
    await f.app.capture(preview(), identity);
    await f.edit({ type: "fullUrl", enabled: true });
    await expect(
      f.app.handle({ type: "disconnect", key, mode: "revoke_delete", confirm: "wrong" }),
    ).rejects.toThrow("Confirm");
    f.replies.push(() => {
      throw new Error("offline");
    });
    await expect(
      f.app.handle({
        type: "disconnect",
        key,
        mode: "revoke_keep",
        confirm: "DISCONNECT revoke_keep",
      }),
    ).rejects.toThrow();
    expect((await f.app.view()).current!.items).toHaveLength(1);
    await f.app.handle({
      type: "disconnect",
      key,
      mode: "local_keep",
      confirm: "DISCONNECT local_keep",
    });
    expect(f.app.fullUrlEnabled()).toBe(false);
    expect((await f.store.all())[0]!.items).toHaveLength(1);
    expect((await f.store.all())[0]!.closed).toBe(false);
    expect((await f.app.view()).message).toContain("unconfirmed");
  });
  it("rejects stale account/target batch actions and JSON-roundtrips every public panel payload", async () => {
    const f = await fixture();
    await seed(f.store);
    await expect(
      f.app.handle({ type: "retry", key: "stale", batchId: snapshot().id, targetId: target.id }),
    ).rejects.toThrow("Connection changed");
    await expect(
      f.app.handle({ type: "check", key, batchId: snapshot().id, targetId: "wrong" }),
    ).rejects.toThrow("Frozen target");
    const view = await f.app.view();
    expect(JSON.parse(JSON.stringify(view))).toEqual(view);
    expect(JSON.stringify(view)).not.toContain("synthetic-access");
    expect(JSON.stringify(view)).not.toContain("synthetic-refresh");
    expect(f.requests).toHaveLength(0);
  });
});
