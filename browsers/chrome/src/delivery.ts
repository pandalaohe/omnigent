import { ApiError, Connection } from "./auth";
import { identityKey } from "./model";
import type { BatchV1, Identity, Partition, Receipt, Snapshot } from "./model";
import { record, serializeItem } from "./protocol";
import { TrayStore } from "./store";

export async function imageHash(image: Blob): Promise<string> {
  return Array.from(
    new Uint8Array(await crypto.subtle.digest("SHA-256", await image.arrayBuffer())),
    (byte) => byte.toString(16).padStart(2, "0"),
  ).join("");
}
function receiptValue(input: unknown, id: string, sid: string): Receipt {
  const value = record(input);
  if (
    value.batch_id !== id ||
    value.session_id !== sid ||
    !["accepted", "delivery_unknown"].includes(String(value.state)) ||
    (value.accepted_at !== undefined &&
      (typeof value.accepted_at !== "number" ||
        !Number.isSafeInteger(value.accepted_at) ||
        value.accepted_at < 0)) ||
    (value.dispatch_phase !== undefined &&
      !["ready", "dispatching", "uncertain", "confirmed"].includes(String(value.dispatch_phase)))
  )
    throw new Error("Receipt identity or state does not match this batch. Check delivery.");
  return {
    batch_id: id,
    session_id: sid,
    state: value.state as Receipt["state"],
    dispatch_phase: value.dispatch_phase as Receipt["dispatch_phase"],
    retry_required: value.retry_required === true,
    ...(typeof value.item_id === "string" ? { item_id: value.item_id } : {}),
    ...(typeof value.pending_id === "string" ? { pending_id: value.pending_id } : {}),
    ...(typeof value.accepted_at === "number" ? { accepted_at: value.accepted_at } : {}),
  };
}

export class Delivery {
  readonly busy = new Set<string>();
  constructor(
    private store: TrayStore,
    private connection: Connection,
    private changed: () => Promise<void>,
  ) {}
  private async owner(
    key: string,
    id: string,
  ): Promise<{ part: Partition; batch: Snapshot; identity: Identity }> {
    const identity = this.connection.identity();
    const part = (await this.store.all()).find((part) => part.key === key);
    const batch = part?.batches.find((batch) => batch.id === id);
    if (!part || !batch || part.closed || !identity || identityKey(identity) !== key)
      throw new Error(
        "This is a recovery tray. Reconnect with the original account; no old-grant request was made.",
      );
    return { part, batch, identity };
  }
  private async update(key: string, id: string, mutate: (batch: Snapshot) => void): Promise<void> {
    await this.store.change([key], (parts) => {
      const batch = parts[0]?.batches.find((batch) => batch.id === id);
      if (!batch) throw new Error("Batch no longer exists.");
      mutate(batch);
    });
    await this.changed();
  }
  private async accept(key: string, id: string, receipt: Receipt): Promise<void> {
    await this.update(key, id, (batch) => {
      batch.receipt = receipt;
      batch.notRecorded = false;
      batch.state = "accepted";
      batch.acceptedAt = Date.now();
      batch.error = undefined;
      // A later native rejection can make any unconfirmed receipt retryable.
      if (receipt.dispatch_phase === "confirmed") batch.body = undefined;
      batch.items = [];
      batch.staged = {};
    });
  }
  async check(key: string, id: string): Promise<void> {
    if (this.busy.has(id)) throw new Error("This batch is already in flight.");
    this.busy.add(id);
    try {
      const { identity, batch } = await this.owner(key, id);
      const response = await this.connection.request(
        identity,
        `v1/browser-annotations/batches/${encodeURIComponent(id)}`,
        { method: "POST" },
      );
      await this.accept(key, id, receiptValue(response, id, batch.target.id));
    } catch (error) {
      if (error instanceof ApiError && error.status === 404 && error.code === "not_recorded") {
        await this.update(key, id, (snapshot) => {
          snapshot.state = "delivery_unknown";
          snapshot.notRecorded = true;
          snapshot.error =
            "No receipt recorded at this check. This live-grant absence is provisional. Explicit Retry keeps the same batch and target.";
        });
        return;
      }
      throw error;
    } finally {
      this.busy.delete(id);
      await this.changed();
    }
  }
  async run(key: string, id: string, retryDelivery = false): Promise<void> {
    if (this.busy.has(id)) throw new Error("This batch is already in flight.");
    this.busy.add(id);
    let submitting = false;
    let started = false;
    try {
      let { batch, identity } = await this.owner(key, id);
      if (retryDelivery) {
        if (
          batch.receipt?.dispatch_phase !== "ready" ||
          !batch.receipt.retry_required ||
          !batch.body
        )
          throw new Error(
            "First dispatch is not known to be retryable. Check delivery or open the session.",
          );
      } else if (
        batch.receipt ||
        batch.state === "accepted" ||
        batch.state === "moved" ||
        (batch.state === "delivery_unknown" && !batch.notRecorded)
      )
        throw new Error("Check delivery before retrying this Unknown batch.");
      await this.update(key, id, (snapshot) => {
        snapshot.state = "uploading";
        snapshot.attempted = true;
        snapshot.error = undefined;
        snapshot.notRecorded = false;
      });
      started = true;
      let body: BatchV1;
      if (retryDelivery) body = { ...batch.body!, intent: "retry_delivery" };
      else {
        for (const item of batch.items) {
          if (item.choice !== "include") continue;
          if (!item.image || !item.hash || (await imageHash(item.image)) !== item.hash)
            throw new Error(
              "Local screenshot bytes are missing or changed. Nothing was regenerated.",
            );
          const staged = batch.staged[item.wire.capture_id];
          if (staged && staged.expires_at * 1000 > Date.now() + 1000) continue;
          const form = new FormData();
          form.set("batch_id", id);
          form.set("capture_id", item.wire.capture_id);
          form.set("sha256", item.hash);
          form.set("file", item.image, `annotation-${item.wire.capture_id}.png`);
          const response = record(
            await this.connection.request(
              identity,
              `v1/browser-annotations/sessions/${encodeURIComponent(batch.target.id)}/screenshots`,
              { method: "POST", body: form },
            ),
          );
          if (
            typeof response.file_id !== "string" ||
            !/^[0-9a-f]{32}$/.test(response.file_id) ||
            response.sha256 !== item.hash ||
            typeof response.expires_at !== "number" ||
            !Number.isSafeInteger(response.expires_at) ||
            !(response.expires_at * 1000 > Date.now())
          )
            throw new Error("Invalid screenshot acknowledgement.");
          const accepted = {
            file_id: response.file_id,
            sha256: item.hash,
            expires_at: response.expires_at,
          };
          await this.update(key, id, (snapshot) => {
            snapshot.staged[item.wire.capture_id] = accepted;
          });
          batch.staged[item.wire.capture_id] = accepted;
        }
        body = {
          version: 1,
          batch_id: id,
          intent: "send",
          items: batch.items.map((item) => {
            const reference =
              item.choice === "include" ? batch.staged[item.wire.capture_id] : undefined;
            const wire = {
              ...item.wire,
              ...(reference
                ? { screenshot: { file_id: reference.file_id, sha256: reference.sha256 } }
                : {}),
            };
            return JSON.parse(serializeItem(wire));
          }),
        };
      }
      await this.update(key, id, (snapshot) => {
        snapshot.body = body;
        snapshot.state = "submitting";
      });
      submitting = true;
      const response = await this.connection.request(
        identity,
        `v1/browser-annotations/sessions/${encodeURIComponent(batch.target.id)}/batches`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
      );
      await this.accept(key, id, receiptValue(response, id, batch.target.id));
    } catch (error) {
      if (!started) throw error;
      const uncertain = submitting && !(error instanceof ApiError);
      await this.update(key, id, (batch) => {
        batch.state = uncertain ? "delivery_unknown" : "error";
        batch.error = uncertain
          ? "Delivery Unknown. Check delivery before any retry."
          : error instanceof Error
            ? error.message
            : "Delivery stopped. Local evidence is retained.";
        if (
          error instanceof ApiError &&
          error.status === 410 &&
          error.code === "screenshot_staging_expired"
        ) {
          for (const captureId of error.captureIds) delete batch.staged[captureId];
        }
      }).catch(() => undefined);
      throw error;
    } finally {
      this.busy.delete(id);
      await this.changed();
    }
  }
}
