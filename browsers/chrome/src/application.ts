import { Connection } from "./auth";
import { Delivery, imageHash } from "./delivery";
import { identityKey } from "./model";
import type {
  AppView,
  DraftItem,
  Identity,
  ItemView,
  Partition,
  PartitionView,
  SessionRow,
  Snapshot,
  TargetPage,
} from "./model";
import { pageUrl, randomId, record, serializeItem } from "./protocol";
import type { CapturePreview } from "./protocol";
import { TrayStore } from "./store";

const emptyTargets = (): TargetPage => ({
  sessions: [],
  next_cursor: null,
  receiving_session_id: null,
  receiving_revision: 0,
  receiving_session: null,
});
const string = (value: unknown, max = 2000): string => {
  if (typeof value !== "string" || value.length > max)
    throw new Error("Invalid extension command.");
  return value;
};
function row(input: unknown): SessionRow {
  const value = record(input);
  if (typeof value.available !== "boolean") throw new Error("Invalid session availability.");
  return {
    id: string(value.id, 200),
    title: string(value.title, 400),
    host_name: value.host_name === null ? null : string(value.host_name, 200),
    workspace: value.workspace === null ? null : string(value.workspace, 2000),
    available: value.available,
    reason: value.reason === null ? null : string(value.reason, 500),
  };
}
function fresh(items: DraftItem[]): DraftItem[] {
  return items.map((item) => {
    const { screenshot: _, ...wire } = item.wire;
    return { ...item, wire: { ...wire, capture_id: randomId() } };
  });
}
export async function blobUrl(blob: Blob): Promise<string> {
  const bytes = new Uint8Array(await blob.arrayBuffer());
  let raw = "";
  for (let offset = 0; offset < bytes.length; offset += 8192)
    raw += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
  return `data:${blob.type || "image/png"};base64,${btoa(raw)}`;
}
export function previewBlob(data: string): Blob {
  if (!data.startsWith("data:image/png;base64,")) throw new Error("Invalid capture image.");
  const bytes = Uint8Array.from(atob(data.slice(22)), (char) => char.charCodeAt(0));
  return new Blob([bytes], { type: "image/png" });
}

export class AnnotationApp {
  private commands: Promise<unknown> = Promise.resolve();
  readonly delivery: Delivery;
  private targets = emptyTargets();
  private known = new Map<string, SessionRow>();
  private message = "Connect to Omnigent, then pick page evidence.";
  private fullURL = false;
  private recapture: { key: string; id: string } | undefined;
  constructor(
    readonly store: TrayStore,
    readonly connection: Connection,
    private changed: (view: AppView) => void,
  ) {
    this.delivery = new Delivery(store, connection, () => this.publish());
  }
  async initialize(): Promise<void> {
    await this.connection.initialize();
    await this.store.restart();
    const identity = this.connection.identity();
    if (identity) this.fullURL = (await this.store.ensure(identity)).includeFullURL;
  }
  identity(): Identity | undefined {
    return this.connection.view().status === "connected" ? this.connection.identity() : undefined;
  }
  private currentKey(): string {
    const identity = this.identity();
    if (!identity) throw new Error("Connect or reconnect before changing this tray.");
    return identityKey(identity);
  }
  fullUrlEnabled(): boolean {
    return !!this.identity() && this.fullURL;
  }
  private guard(part: Partition | undefined, command: Record<string, unknown>): Partition {
    if (
      !part ||
      part.closed ||
      part.key !== this.currentKey() ||
      part.key !== command.key ||
      part.revision !== command.revision ||
      (part.target?.id ?? null) !== command.expectedTarget
    )
      throw new Error(
        "Tray or target changed. Review the latest panel before continuing. Your typed input is retained.",
      );
    return part;
  }
  async view(): Promise<AppView> {
    const identity = this.connection.identity();
    const all = await this.store.all();
    const visible = identity
      ? all.filter(
          (part) =>
            part.identity.server === identity.server && part.identity.userId === identity.userId,
        )
      : [];
    const convert = async (part: Partition): Promise<PartitionView> => {
      const item = async (item: DraftItem): Promise<ItemView> => ({
        wire: item.wire,
        choice: item.choice,
        failure: item.failure,
        bytes: item.image?.size ?? 0,
        ...(item.image ? { image: await blobUrl(item.image) } : {}),
      });
      const batches = await Promise.all(
        part.batches.map(async (batch) => {
          const { body: _, staged: __, items, ...metadata } = batch;
          return { ...metadata, items: await Promise.all(items.map(item)) };
        }),
      );
      return {
        ...part,
        items: await Promise.all(part.items.map(item)),
        batches,
        bytes: [...part.items, ...part.batches.flatMap((batch) => batch.items)].reduce(
          (sum, item) => sum + (item.image?.size ?? 0),
          0,
        ),
      };
    };
    const views = await Promise.all(visible.map(convert));
    const key = identity ? identityKey(identity) : "";
    return {
      auth: this.connection.view(),
      current: views.find((part) => part.key === key),
      old: views.filter((part) => part.key !== key),
      targets: this.targets,
      message: this.message,
      busy: Array.from(this.delivery.busy),
    };
  }
  async publish(message?: string): Promise<void> {
    if (message) this.message = message;
    this.changed(await this.view());
  }
  async capture(preview: CapturePreview, owner: Identity | undefined): Promise<void> {
    if (!owner || identityKey(owner) !== this.currentKey())
      throw new Error(
        "Capture remains temporary. Connect with the same account and save it explicitly.",
      );
    const key = identityKey(owner);
    if (preview.ownerKey && preview.ownerKey !== key)
      throw new Error("This temporary capture belongs to another connection. Capture it again.");
    const full = preview.selection.page.include_query_fragment && preview.urlConsentKey === key;
    const image = preview.image ? previewBlob(preview.image) : undefined;
    const hash = image ? await imageHash(image) : undefined;
    const item: DraftItem = {
      wire: {
        capture_id: preview.capture_id,
        captured_at: preview.captured_at,
        kind: preview.selection.kind,
        note: "",
        page: {
          ...preview.selection.page,
          url: pageUrl(preview.selection.page.url, full),
          include_query_fragment: full,
        },
        target: preview.selection.target,
        geometry: preview.geometry ?? {
          rect: preview.selection.rect,
          viewport: {
            width: preview.selection.metrics.width,
            height: preview.selection.metrics.height,
          },
          bitmap_scale: null,
        },
      },
      ...(image ? { image, hash } : {}),
      choice: image ? "include" : "pending",
      failure: preview.screenshotFailure,
    };
    await this.store.change([key], (parts) => {
      const part = parts[0];
      if (!part || part.closed || key !== this.currentKey())
        throw new Error("Connection changed during capture.");
      if (
        [...part.items, ...part.batches.flatMap((batch) => batch.items)].some(
          (saved) => saved.wire.capture_id === item.wire.capture_id,
        )
      )
        throw new Error("This capture is already saved. Review the tray.");
      if (this.recapture?.key === key) {
        const index = part.items.findIndex((item) => item.wire.capture_id === this.recapture?.id);
        if (index < 0) throw new Error("Original note changed. Capture a new item instead.");
        if (!image)
          throw new Error("Recapture failed. The original note and screenshot remain saved.");
        item.wire.note = part.items[index]!.wire.note;
        part.items[index] = item;
      } else part.items.push(item);
    });
    this.recapture = undefined;
    await this.publish(
      image
        ? "Saved locally. Review the crop and add a note."
        : "Saved with a screenshot failure. Explicitly choose Send without screenshot or recapture.",
    );
  }
  private async list(query: string, cursor: string): Promise<void> {
    const key = this.currentKey();
    const identity = this.identity()!;
    const result = record(
      await this.connection.request(
        identity,
        `v1/browser-annotations/sessions?${new URLSearchParams({ query, cursor, limit: "50" })}`,
        { method: "POST" },
      ),
    );
    if (this.currentKey() !== key) throw new Error("Connection changed while refreshing targets.");
    if (
      !Array.isArray(result.sessions) ||
      result.sessions.length > 50 ||
      typeof result.receiving_revision !== "number"
    )
      throw new Error("Invalid target list.");
    this.targets = {
      sessions: result.sessions.map(row),
      next_cursor: result.next_cursor === null ? null : string(result.next_cursor),
      receiving_session_id:
        result.receiving_session_id === null ? null : string(result.receiving_session_id),
      receiving_revision: result.receiving_revision,
      receiving_session: result.receiving_session === null ? null : row(result.receiving_session),
    };
    for (const session of [
      ...this.targets.sessions,
      ...(this.targets.receiving_session ? [this.targets.receiving_session] : []),
    ])
      this.known.set(session.id, session);
    const remembered = (await this.store.all())
      .filter(
        (part) =>
          part.identity.server === identity.server && part.identity.userId === identity.userId,
      )
      .map((part) => part.rememberedTarget)
      .filter((choice) => choice && this.known.get(choice.id)?.available)
      .sort((a, b) => b!.at - a!.at)[0];
    await this.store.change([key], (parts) => {
      const part = parts[0]!;
      if (!part.target && part.items.length === 0)
        part.target =
          this.targets.receiving_session ?? (remembered ? this.known.get(remembered.id)! : null);
      else if (part.target && this.known.has(part.target.id))
        part.target = this.known.get(part.target.id)!;
    });
  }
  private async edit(
    command: Record<string, unknown>,
    mutate: (part: Partition) => void,
  ): Promise<void> {
    const key = this.currentKey();
    await this.store.change([key], (parts) => mutate(this.guard(parts[0], command)));
  }
  handle(input: unknown): Promise<void> {
    const result = this.commands.then(() => this.perform(input));
    this.commands = result.catch(() => undefined);
    return result;
  }
  private async perform(input: unknown): Promise<void> {
    const command = record(input);
    const type = string(command.type, 40);
    if (type === "connect") {
      await this.connection.begin(string(command.server), string(command.label, 80));
      this.known.clear();
      this.targets = emptyTargets();
      await chrome.tabs.create({ url: this.connection.view().consentUrl! });
    } else if (type === "poll") {
      if (await this.connection.poll()) {
        this.fullURL = (await this.store.ensure(this.identity()!)).includeFullURL;
        await this.list("", "");
      }
    } else if (type === "list") await this.list(string(command.query, 200), string(command.cursor));
    else if (type === "target")
      await this.edit(command, (part) => {
        const target = this.known.get(string(command.sessionId, 200));
        if (!target) throw new Error("Refresh the target list and choose a listed session.");
        part.target = target;
        part.rememberedTarget = { id: target.id, at: Date.now() };
      });
    else if (type === "fullUrl") {
      if (typeof command.enabled !== "boolean") throw new Error("Invalid URL inclusion choice.");
      await this.edit(command, (part) => {
        part.includeFullURL = command.enabled as boolean;
      });
      this.fullURL = command.enabled;
    } else if (["note", "delete", "move", "without", "recapture", "stripUrl"].includes(type)) {
      await this.edit(command, (part) => {
        const index = part.items.findIndex((item) => item.wire.capture_id === command.id);
        if (index < 0) throw new Error("This item is frozen or no longer editable.");
        const item = part.items[index]!;
        if (type === "note") item.wire.note = string(command.note, 4000);
        if (type === "stripUrl")
          item.wire.page = {
            ...item.wire.page,
            url: pageUrl(item.wire.page.url),
            include_query_fragment: false,
          };
        if (type === "delete") part.items.splice(index, 1);
        if (type === "without") {
          item.choice = "without";
          delete item.image;
          delete item.hash;
        }
        if (type === "move") {
          const delta = command.delta;
          if (delta !== 1 && delta !== -1) throw new Error("Invalid move.");
          const destination = index + delta;
          if (destination >= 0 && destination < part.items.length) {
            part.items.splice(index, 1);
            part.items.splice(destination, 0, item);
          }
        }
      });
      if (type === "recapture")
        this.recapture = { key: this.currentKey(), id: string(command.id, 32) };
    } else if (type === "send") {
      const id = randomId();
      const key = this.currentKey();
      await this.edit(command, (part) => {
        if (!part.target?.available || !part.items.length)
          throw new Error("Choose an available target and add a note.");
        for (const item of part.items) {
          if (item.choice === "pending")
            throw new Error("Resolve each screenshot failure explicitly.");
          serializeItem(item.wire);
          if (
            item.choice === "include" &&
            (!item.image || !item.hash || !item.wire.geometry.bitmap_scale)
          )
            throw new Error(
              "Screenshot evidence is incomplete. Recapture or remove it explicitly.",
            );
        }
        const snapshot: Snapshot = {
          id,
          target: part.target,
          items: part.items,
          state: "ready",
          attempted: false,
          staged: {},
          createdAt: Date.now(),
        };
        part.batches.push(snapshot);
        part.items = [];
      });
      void this.delivery
        .run(key, id)
        .catch((error) =>
          this.publish(error instanceof Error ? error.message : "Delivery stopped.").catch(
            () => undefined,
          ),
        );
    } else if (["check", "retry", "retryDelivery"].includes(type)) {
      const key = this.currentKey();
      if (command.key !== key) throw new Error("Connection changed.");
      const batch = (await this.store.all())
        .find((part) => part.key === key)
        ?.batches.find((batch) => batch.id === command.batchId);
      if (!batch || command.targetId !== batch.target.id)
        throw new Error("Frozen target does not match the displayed action.");
      if (type === "check") await this.delivery.check(key, batch.id);
      else
        void this.delivery
          .run(key, batch.id, type === "retryDelivery")
          .catch((error) =>
            this.publish(error instanceof Error ? error.message : "Delivery stopped.").catch(
              () => undefined,
            ),
          );
    } else if (type === "disconnect") {
      const identity = this.connection.identity();
      const key = identity ? identityKey(identity) : "";
      const mode = string(command.mode, 30);
      if (
        !["revoke_keep", "local_keep", "revoke_delete"].includes(mode) ||
        command.confirm !== `DISCONNECT ${mode}`
      )
        throw new Error("Confirm the disconnect and local-data choice explicitly.");
      if (command.key !== key) throw new Error("Connection changed.");
      const result = await this.connection.disconnect(mode === "local_keep");
      if (key)
        await this.store.change([key], (parts) => {
          if (mode === "revoke_delete") parts[0] = undefined;
          else if (parts[0]) parts[0].closed = result.closed;
        });
      this.targets = emptyTargets();
      this.known.clear();
      this.recapture = undefined;
      this.fullURL = false;
      this.message = result.closed
        ? "Disconnected. Server revocation acknowledged."
        : "Disconnected locally. Server revocation is unconfirmed; revoke this app in Omnigent settings. Notes were kept.";
    } else if (type === "recover" || type === "copy" || type === "closed" || type === "discard")
      await this.recovery(type, command);
    else throw new Error("Unknown extension command.");
    await this.publish(
      type === "note"
        ? "Note saved locally."
        : type === "recapture"
          ? "Original note stays saved. Invoke the Chrome toolbar action or shortcut and pick its replacement."
          : undefined,
    );
  }
  private async recovery(type: string, command: Record<string, unknown>): Promise<void> {
    const key = this.currentKey();
    const oldKey = string(command.oldKey, 4000);
    const identity = this.identity()!;
    const keys = Array.from(new Set([key, oldKey]));
    await this.store.change(keys, (parts) => {
      const destination = parts.find((part) => part?.key === key)!;
      const source = parts.find((part) => part?.key === oldKey);
      if (
        !source ||
        source.identity.server !== identity.server ||
        source.identity.userId !== identity.userId
      )
        throw new Error("Recovery requires the same verified server and original account.");
      if (type === "closed") {
        if (command.confirm !== `CLOSED ${source.identity.grantId}`)
          throw new Error("Confirm the old grant was closed in signed-in Omnigent settings.");
        source.closed = true;
        return;
      }
      const batchId = string(command.batchId, 200);
      const batch = source.batches.find((batch) => batch.id === batchId);
      if (this.delivery.busy.has(batchId)) throw new Error("This batch is already in flight.");
      if (type === "discard") {
        if (command.confirm !== `DISCARD ${batchId}` || !batch || this.delivery.busy.has(batchId))
          throw new Error("Confirm discarding this local batch. It cannot unsend a message.");
        source.batches = source.batches.filter((item) => item.id !== batchId);
        return;
      }
      this.guard(destination, command);
      const items = batchId === "draft" ? source.items : batch?.items;
      if (!items?.length || (source.key === destination.key && batchId === "draft"))
        throw new Error("No retained evidence is available to move.");
      if (type === "copy") {
        if (command.confirm !== `COPY ${batchId}`)
          throw new Error("Confirm sending another copy may duplicate an admitted message.");
      } else if (batch?.attempted) {
        if (
          !source.closed ||
          batch.receipt ||
          command.confirm !== `NOT ADMITTED ${source.identity.grantId} ${batch.id}`
        )
          throw new Error(
            "Close the old grant and explicitly confirm the exact first-party not-admitted result. Unknown is not that result.",
          );
      } else if (command.confirm !== `MOVE ${source.identity.grantId}`)
        throw new Error("Confirm moving this never-attempted draft.");
      destination.items.push(...fresh(items));
      destination.migrations.push({
        fromGrant: source.identity.grantId,
        fromBatch: batchId,
        confirmation: string(command.confirm, 1000),
        at: Date.now(),
      });
      if (type !== "copy") {
        if (batchId === "draft") source.items = [];
        else if (batch) {
          batch.items = [];
          batch.state = "moved";
          batch.staged = {};
          batch.body = undefined;
        }
      }
    });
  }
}
