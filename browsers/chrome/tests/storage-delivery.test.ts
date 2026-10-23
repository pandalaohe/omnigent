import { afterEach, describe, expect, it, vi } from "vitest";
import { Delivery, imageHash } from "../src/delivery";
import { TrayStore } from "../src/store";
import { identityKey } from "../src/model";
import { identity, item, key, owners, receipt, seed, snapshot } from "./owners.fixture";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("native IndexedDB tray transactions", () => {
  it("commits Blob bytes, rolls back failed edits without eviction, and partitions identities", async () => {
    const { store } = await owners();
    const image = new Blob([new Uint8Array([1, 2, 3])], { type: "image/png" });
    await store.change([key], (parts) => {
      parts[0]!.items.push({ ...item(), image, choice: "include" });
    });
    expect(
      Array.from(new Uint8Array(await (await store.all())[0]!.items[0]!.image!.arrayBuffer())),
    ).toEqual([1, 2, 3]);
    const before = (await store.all())[0]!;
    await expect(
      store.change([key], (parts) => {
        parts[0]!.items[0]!.wire.note = "x".repeat(4001);
      }),
    ).rejects.toThrow("4000");
    expect((await store.all())[0]).toEqual(before);
    await expect(
      store.change([key], (parts) => {
        parts[0]!.items = Array.from({ length: 11 }, () => item());
      }),
    ).rejects.toThrow("10 items");
    await expect(
      store.change([key], (parts) => {
        parts[0]!.items[0]!.image = new Blob([new Uint8Array(2 * 1024 * 1024 + 1)]);
      }),
    ).rejects.toThrow("2 MiB");
    await store.ensure({ ...identity, grantId: "grant-b" });
    await store.ensure({ ...identity, userId: "user-b" });
    await store.ensure({ ...identity, server: "https://other.example.test/base" });
    expect((await store.all()).map((part) => part.key)).toContain(
      identityKey({ ...identity, userId: "user-b" }),
    );
    expect(await store.all()).toHaveLength(4);
  });
  it("acknowledges only after native transaction completion and surfaces native write aborts", async () => {
    const { store } = await owners();
    let committed = false;
    const promise = store.change([key], (parts) => {
      parts[0]!.items.push(item());
      expect(committed).toBe(false);
    });
    promise.then(() => {
      committed = true;
    });
    expect(committed).toBe(false);
    await promise;
    expect(committed).toBe(true);
    // Exercise the actual IDB transaction abort boundary, rather than replacing the store owner.
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
    await expect(
      store.change([key], (parts) => {
        parts[0]!.items[0]!.wire.note = "new input";
      }),
    ).rejects.toThrow("not saved");
    fault.mockRestore();
    expect((await store.all())[0]!.items[0]!.wire.note).toBe("Make this readable");
  });
  it("restores drafts and marks interrupted submissions Unknown without redispatch; only confirmed metadata expires", async () => {
    const { store, requests } = await owners();
    const old = Date.now() - 31 * 86400000;
    await store.change([key], (parts) => {
      const part = parts[0]!;
      part.items = [item()];
      part.batches = [
        { ...snapshot("1".repeat(32)), state: "uploading", attempted: true },
        { ...snapshot("2".repeat(32)), state: "submitting", attempted: true },
        {
          ...snapshot("3".repeat(32)),
          state: "accepted",
          acceptedAt: old,
          receipt: receipt("3".repeat(32), "ready") as never,
          body: { version: 1, batch_id: "3".repeat(32), intent: "send", items: [item().wire] },
        },
        {
          ...snapshot("4".repeat(32)),
          state: "accepted",
          acceptedAt: old,
          receipt: receipt("4".repeat(32)) as never,
          items: [],
        },
      ];
    });
    await new TrayStore((store as unknown as { name: string }).name).restart();
    const part = (await store.all())[0]!;
    expect(part.items).toHaveLength(1);
    expect(part.batches.map((batch) => batch.state)).toEqual([
      "delivery_unknown",
      "delivery_unknown",
      "accepted",
    ]);
    expect(part.batches[2]!.body!.items[0]!.note).toBe("Make this readable");
    expect(requests).toHaveLength(0);
  });
});

describe("explicit frozen-batch delivery", () => {
  it("hashes the independent SHA-256 abc example", async () => {
    expect(await imageHash(new Blob(["abc"]))).toBe(
      "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
    );
  });
  it("uploads persisted bytes once, sends exact ordered IDs, ignores receipt links and atomically drops confirmed evidence", async () => {
    vi.spyOn(Date, "now").mockReturnValue(1800000000000);
    const f = await owners();
    const batch = snapshot();
    const image = new Blob([new Uint8Array([1, 2, 3])], { type: "image/png" });
    const hash = await imageHash(image);
    batch.items[0] = {
      ...item(),
      image,
      hash,
      choice: "include",
      wire: {
        ...item().wire,
        geometry: { ...item().wire.geometry, bitmap_scale: { x: 1.5, y: 1.25 } },
      },
    };
    batch.items.push(item("c".repeat(32)));
    await seed(f.store, batch);
    f.replies.push(
      async (_url, init) => {
        const persisted = (await f.store.all())[0]!.batches[0]!;
        expect(persisted.state).toBe("uploading");
        expect(persisted.attempted).toBe(true);
        const form = init.body as FormData;
        expect(Array.from(form.keys())).toEqual(["batch_id", "capture_id", "sha256", "file"]);
        expect(form.get("capture_id")).toBe("a".repeat(32));
        expect(Array.from(new Uint8Array(await (form.get("file") as Blob).arrayBuffer()))).toEqual([
          1, 2, 3,
        ]);
        return {
          file_id: "d".repeat(32),
          sha256: hash,
          expires_at: 1800086400,
        };
      },
      async (_url, init) => {
        const body = JSON.parse(String(init.body));
        expect(body.items.map((i: { capture_id: string }) => i.capture_id)).toEqual([
          "a".repeat(32),
          "c".repeat(32),
        ]);
        expect(body.items[0].screenshot).toEqual({ file_id: "d".repeat(32), sha256: hash });
        expect((await f.store.all())[0]!.batches[0]!.body).toEqual(body);
        return receipt();
      },
    );
    const delivery = new Delivery(f.store, f.connection, async () => {});
    await delivery.run(key, batch.id);
    const saved = (await f.store.all())[0]!.batches[0]!;
    expect(saved.state).toBe("accepted");
    expect(saved.items).toEqual([]);
    expect(saved.body).toBeUndefined();
    expect(saved.receipt).not.toHaveProperty("session_path");
    expect(saved.receipt!.accepted_at).toBe(1800000000);
    expect(f.requests).toHaveLength(2);
  });
  it.each(["2027-01-16T08:00:00.000Z", 1800086400.5, null])(
    "rejects non-integer-epoch screenshot expiry %s without a batch request",
    async (expires_at) => {
      vi.spyOn(Date, "now").mockReturnValue(1800000000000);
      const f = await owners();
      const batch = snapshot();
      const image = new Blob(["abc"]);
      const hash = await imageHash(image);
      batch.items[0] = {
        ...item(),
        image,
        hash,
        choice: "include",
        wire: {
          ...item().wire,
          geometry: { ...item().wire.geometry, bitmap_scale: { x: 1, y: 1 } },
        },
      };
      await seed(f.store, batch);
      f.replies.push(() => ({ file_id: "d".repeat(32), sha256: hash, expires_at }));
      const delivery = new Delivery(f.store, f.connection, async () => {});
      await expect(delivery.run(key, batch.id)).rejects.toThrow(
        "Invalid screenshot acknowledgement",
      );
      expect(f.requests).toHaveLength(1);
      expect((await f.store.all())[0]!.batches[0]!.items[0]!.hash).toBe(hash);
    },
  );
  it.each(["2027-01-15T08:00:00.000Z", 1800000000.5])(
    "rejects a non-integer-epoch receipt time %s rather than omitting it",
    async (accepted_at) => {
      const f = await owners();
      await seed(f.store);
      f.replies.push(() => ({ ...receipt(), accepted_at }));
      const delivery = new Delivery(f.store, f.connection, async () => {});
      await expect(delivery.run(key, snapshot().id)).rejects.toThrow("Receipt identity or state");
      expect((await f.store.all())[0]!.batches[0]!.state).toBe("delivery_unknown");
      expect((await f.store.all())[0]!.batches[0]!.items).toHaveLength(1);
    },
  );
  it("reuploads expired numeric staging only on explicit retry, keeping bytes, hash and IDs", async () => {
    const now = vi.spyOn(Date, "now").mockReturnValue(1800000000000);
    const f = await owners();
    const batch = snapshot();
    const image = new Blob(["abc"]);
    const hash = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad";
    batch.items[0] = {
      ...item(),
      image,
      hash,
      choice: "include",
      wire: { ...item().wire, geometry: { ...item().wire.geometry, bitmap_scale: { x: 1, y: 1 } } },
    };
    batch.staged["a".repeat(32)] = {
      file_id: "d".repeat(32),
      sha256: hash,
      expires_at: 1800000010,
    };
    await seed(f.store, batch);
    f.replies.push(() => Response.json({ error: "target_unavailable" }, { status: 503 }));
    const delivery = new Delivery(f.store, f.connection, async () => {});
    await expect(delivery.run(key, batch.id)).rejects.toThrow("503");
    expect(f.requests[0]!.url).toBe(
      `${identity.server}/v1/browser-annotations/sessions/session-a/batches`,
    );
    now.mockReturnValue(1800000011000);
    expect(f.requests).toHaveLength(1);
    f.replies.push(
      async (_url, init) => {
        const form = init.body as FormData;
        expect(form.get("batch_id")).toBe("b".repeat(32));
        expect(form.get("capture_id")).toBe("a".repeat(32));
        expect(form.get("sha256")).toBe(hash);
        expect(await (form.get("file") as Blob).text()).toBe("abc");
        return { file_id: "e".repeat(32), sha256: hash, expires_at: 1800086411 };
      },
      () => receipt(),
    );
    await delivery.run(key, batch.id);
    const body = JSON.parse(String(f.requests[2]!.init.body));
    expect(body.batch_id).toBe("b".repeat(32));
    expect(body.items[0].capture_id).toBe("a".repeat(32));
    expect(body.items[0].screenshot).toEqual({ file_id: "e".repeat(32), sha256: hash });
    expect((await f.store.all())[0]!.batches[0]!.state).toBe("accepted");
  });
  it("keeps upload failure bytes and requires an explicit retry using the same namespace", async () => {
    const f = await owners();
    const batch = snapshot();
    const image = new Blob(["png"]);
    batch.items[0]!.image = image;
    batch.items[0]!.hash = await imageHash(image);
    batch.items[0]!.choice = "include";
    batch.items[0]!.wire.geometry.bitmap_scale = { x: 2, y: 2 };
    await seed(f.store, batch);
    f.replies.push(() => {
      throw new Error("upload lost ACK");
    });
    const delivery = new Delivery(f.store, f.connection, async () => {});
    await expect(delivery.run(key, batch.id)).rejects.toThrow();
    expect((await f.store.all())[0]!.batches[0]!.items[0]!.image!.size).toBe(3);
    expect(f.requests).toHaveLength(1);
    const hash = batch.items[0]!.hash;
    f.replies.push(
      () => ({
        file_id: "e".repeat(32),
        sha256: hash,
        expires_at: Math.floor(Date.now() / 1000) + 86400,
      }),
      () => receipt(),
    );
    await delivery.run(key, batch.id);
    expect((f.requests[1]!.init.body as FormData).get("batch_id")).toBe(batch.id);
    expect((f.requests[1]!.init.body as FormData).get("capture_id")).toBe("a".repeat(32));
  });
  it("keeps lost-submit ACK Unknown, checks read-only, and permits only an explicit same-ID provisional retry", async () => {
    const f = await owners();
    await seed(f.store);
    const delivery = new Delivery(f.store, f.connection, async () => {});
    f.replies.push(() => {
      throw new Error("submit lost ACK");
    });
    await expect(delivery.run(key, snapshot().id)).rejects.toThrow();
    const frozen = (await f.store.all())[0]!.batches[0]!;
    expect(frozen.state).toBe("delivery_unknown");
    expect(frozen.body!.items[0]!.note).toBe("Make this readable");
    await expect(delivery.run(key, frozen.id)).rejects.toThrow("Check delivery");
    expect(f.requests).toHaveLength(1);
    f.replies.push(() => Response.json({ detail: { code: "not_recorded" } }, { status: 404 }));
    await delivery.check(key, frozen.id);
    expect(f.requests[1]!.url).toBe(
      `${identity.server}/v1/browser-annotations/batches/${frozen.id}`,
    );
    expect(f.requests[1]!.init.method).toBe("POST");
    expect(f.requests[1]!.init.body).toBeUndefined();
    expect((await f.store.all())[0]!.batches[0]!.notRecorded).toBe(true);
    f.replies.push(() => receipt());
    await delivery.run(key, frozen.id);
    expect(f.requests[2]!.init.body).toBe(f.requests[0]!.init.body);
  });
  it("retains the complete body for unconfirmed receipts and retries native delivery without image reupload", async () => {
    const f = await owners();
    await seed(f.store);
    const delivery = new Delivery(f.store, f.connection, async () => {});
    f.replies.push(() => ({ ...receipt(undefined, "dispatching"), retry_required: false }));
    await delivery.run(key, snapshot().id);
    const initialBody = JSON.parse(String(f.requests[0]!.init.body));
    expect((await f.store.all())[0]!.batches[0]!.body).toEqual(initialBody);
    expect((await f.store.all())[0]!.batches[0]!.items).toEqual([]);
    f.replies.push(() => receipt(undefined, "ready"));
    await delivery.check(key, snapshot().id);
    await expect(delivery.run(key, snapshot().id)).rejects.toThrow("Check delivery");
    f.replies.push(() => receipt());
    await delivery.run(key, snapshot().id, true);
    expect(JSON.parse(String(f.requests[2]!.init.body))).toEqual({
      ...initialBody,
      intent: "retry_delivery",
    });
    expect(f.requests).toHaveLength(3);
  });
  it("reserializes explicitly expired staging with identical crop bytes, hash and capture IDs", async () => {
    const f = await owners();
    const batch = snapshot();
    const image = new Blob(["original"]);
    const hash = await imageHash(image);
    batch.items[0] = {
      ...item(),
      image,
      hash,
      choice: "include",
      wire: { ...item().wire, geometry: { ...item().wire.geometry, bitmap_scale: { x: 1, y: 1 } } },
    };
    batch.staged["a".repeat(32)] = {
      file_id: "d".repeat(32),
      sha256: hash,
      expires_at: Math.floor(Date.now() / 1000) + 86400,
    };
    await seed(f.store, batch);
    f.replies.push(() =>
      Response.json(
        { detail: { code: "screenshot_staging_expired", capture_ids: ["a".repeat(32)] } },
        { status: 410 },
      ),
    );
    const delivery = new Delivery(f.store, f.connection, async () => {});
    await expect(delivery.run(key, batch.id)).rejects.toThrow("410");
    expect((await f.store.all())[0]!.batches[0]!.staged).toEqual({});
    f.replies.push(
      async (_url, init) => {
        const form = init.body as FormData;
        expect(form.get("sha256")).toBe(hash);
        expect(await (form.get("file") as Blob).text()).toBe("original");
        return {
          file_id: "e".repeat(32),
          sha256: hash,
          expires_at: Math.floor(Date.now() / 1000) + 86400,
        };
      },
      () => receipt(),
    );
    await delivery.run(key, batch.id);
    const body = JSON.parse(String(f.requests[2]!.init.body));
    expect(body.batch_id).toBe(batch.id);
    expect(body.items[0].capture_id).toBe("a".repeat(32));
    expect(body.items[0].screenshot.file_id).toBe("e".repeat(32));
  });
  it("blocks concurrent Check/Retry and refuses all old-grant receipt requests", async () => {
    const f = await owners();
    await seed(f.store);
    const delivery = new Delivery(f.store, f.connection, async () => {});
    let resolve!: (value: unknown) => void;
    f.replies.push(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    const pending = delivery.check(key, snapshot().id);
    await vi.waitFor(() => expect(f.requests).toHaveLength(1));
    await expect(delivery.run(key, snapshot().id)).rejects.toThrow("in flight");
    resolve(receipt());
    await pending;
    const old = { ...identity, grantId: "old" };
    await f.store.ensure(old);
    await f.store.change([identityKey(old)], (parts) => {
      parts[0]!.batches.push(snapshot());
    });
    await expect(delivery.check(identityKey(old), snapshot().id)).rejects.toThrow("old-grant");
    expect(f.requests).toHaveLength(1);
  });
});
