import { identityKey, newPartition } from "./model";
import type { Identity, Partition } from "./model";

function limits(partition: Partition): void {
  const items = [...partition.items, ...partition.batches.flatMap((batch) => batch.items)];
  if (items.length > 10)
    throw new Error("Local tray limit: 10 items. Resolve or discard a batch first.");
  let bytes = 0;
  for (const item of items) {
    if (item.wire.note.length > 4000) throw new Error("Notes are limited to 4000 characters.");
    if (item.image && item.image.size > 2 * 1024 * 1024)
      throw new Error("Screenshot exceeds 2 MiB.");
    bytes += item.image?.size ?? 0;
  }
  if (bytes > 20 * 1024 * 1024) throw new Error("Local tray exceeds 20 MiB. Nothing was evicted.");
}

/** All changed partitions commit together; callers never acknowledge a request before oncomplete. */
export class TrayStore {
  private database: Promise<IDBDatabase> | undefined;
  constructor(private name = "omnigent-annotations") {}
  private open(): Promise<IDBDatabase> {
    this.database ??= new Promise((resolve, reject) => {
      const request = indexedDB.open(this.name, 1);
      request.onupgradeneeded = () =>
        request.result.createObjectStore("partitions", { keyPath: "key" });
      request.onsuccess = () => {
        request.result.onversionchange = () => request.result.close();
        resolve(request.result);
      };
      request.onerror = () =>
        reject(new Error("Local storage could not open. Your input is not saved."));
      request.onblocked = () =>
        reject(new Error("Close other extension panels to upgrade local storage."));
    });
    return this.database;
  }
  async all(): Promise<Partition[]> {
    const db = await this.open();
    return new Promise((resolve, reject) => {
      const transaction = db.transaction("partitions", "readonly");
      const request = transaction.objectStore("partitions").getAll();
      transaction.oncomplete = () => resolve(request.result as Partition[]);
      transaction.onabort = () => reject(new Error("Local storage read failed."));
    });
  }
  async ensure(identity: Identity): Promise<Partition> {
    const key = identityKey(identity);
    const existing = (await this.all()).find((partition) => partition.key === key);
    if (existing) return existing;
    await this.change([key], (parts) => {
      parts[0] ??= newPartition(identity);
    });
    return (await this.all()).find((partition) => partition.key === key)!;
  }
  async change(
    keys: string[],
    mutate: (partitions: (Partition | undefined)[]) => void,
  ): Promise<void> {
    const db = await this.open();
    return new Promise((resolve, reject) => {
      const transaction = db.transaction("partitions", "readwrite");
      const store = transaction.objectStore("partitions");
      const parts: (Partition | undefined)[] = [];
      let pending = keys.length;
      let cause: unknown;
      transaction.oncomplete = () => resolve();
      transaction.onabort = () =>
        reject(
          cause instanceof Error
            ? cause
            : new Error("Local storage write failed. Your input is not saved."),
        );
      for (const [index, key] of keys.entries()) {
        const request = store.get(key);
        request.onsuccess = () => {
          parts[index] = request.result as Partition | undefined;
          if (--pending) return;
          try {
            mutate(parts);
            for (const [i, part] of parts.entries()) {
              if (part) {
                limits(part);
                part.revision++;
                store.put(part);
              } else store.delete(keys[i]!);
            }
          } catch (error) {
            cause = error;
            transaction.abort();
          }
        };
      }
    });
  }
  async restart(): Promise<void> {
    const all = await this.all();
    if (!all.length) return;
    await this.change(
      all.map((part) => part.key),
      (parts) => {
        for (const part of parts)
          if (part) {
            part.batches = part.batches.filter(
              (batch) =>
                batch.state !== "accepted" ||
                batch.receipt?.dispatch_phase !== "confirmed" ||
                !batch.acceptedAt ||
                batch.acceptedAt > Date.now() - 30 * 86400000,
            );
            for (const batch of part.batches)
              if (batch.state === "uploading" || batch.state === "submitting") {
                batch.state = "delivery_unknown";
                batch.error = "Worker restarted. Check delivery; nothing was retried.";
              }
          }
      },
    );
  }
}
