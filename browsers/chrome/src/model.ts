import type { ItemV1 } from "./protocol";

export interface Identity {
  server: string;
  userId: string;
  grantId: string;
  display: string;
}
export const identityKey = (identity: Identity): string =>
  JSON.stringify([identity.server, identity.userId, identity.grantId]);
export interface SessionRow {
  id: string;
  title: string;
  host_name: string | null;
  workspace: string | null;
  available: boolean;
  reason: string | null;
}
export interface TargetPage {
  sessions: SessionRow[];
  next_cursor: string | null;
  receiving_session_id: string | null;
  receiving_revision: number;
  receiving_session: SessionRow | null;
}
export interface DraftItem {
  wire: ItemV1;
  image?: Blob;
  hash?: string;
  choice: "include" | "without" | "pending";
  failure?: string;
}
export interface Receipt {
  batch_id: string;
  session_id: string;
  item_id?: string;
  pending_id?: string;
  accepted_at?: number;
  state: "accepted" | "delivery_unknown";
  dispatch_phase?: "ready" | "dispatching" | "uncertain" | "confirmed";
  retry_required?: boolean;
}
export interface BatchV1 {
  version: 1;
  batch_id: string;
  items: ItemV1[];
  intent: "send" | "retry_delivery";
}
export interface Snapshot {
  id: string;
  target: SessionRow;
  items: DraftItem[];
  state: "ready" | "uploading" | "submitting" | "error" | "delivery_unknown" | "accepted" | "moved";
  attempted: boolean;
  staged: Record<string, { file_id: string; sha256: string; expires_at: number }>;
  receipt?: Receipt;
  body?: BatchV1;
  error?: string;
  createdAt: number;
  acceptedAt?: number;
  notRecorded?: boolean;
}
export interface Partition {
  includeFullURL: boolean;
  key: string;
  identity: Identity;
  closed: boolean;
  revision: number;
  target: SessionRow | null;
  rememberedTarget?: { id: string; at: number };
  items: DraftItem[];
  batches: Snapshot[];
  migrations: { fromGrant: string; fromBatch: string; confirmation: string; at: number }[];
}
export const newPartition = (identity: Identity): Partition => ({
  includeFullURL: false,
  key: identityKey(identity),
  identity,
  closed: false,
  revision: 0,
  target: null,
  items: [],
  batches: [],
  migrations: [],
});
export interface ItemView {
  wire: ItemV1;
  image?: string;
  choice: DraftItem["choice"];
  failure?: string;
  bytes: number;
}
export interface PartitionView extends Omit<Partition, "items" | "batches"> {
  items: ItemView[];
  bytes: number;
  batches: (Omit<Snapshot, "items" | "staged" | "body"> & { items: ItemView[] })[];
}
export interface AuthView {
  status: "disconnected" | "pairing" | "connected" | "reconnect" | "unknown";
  identity?: Identity;
  server?: string;
  code?: string;
  consentUrl?: string;
  pollAfter?: number;
  expiresAt?: number;
  label?: string;
}
export interface AppView {
  auth: AuthView;
  current?: PartitionView;
  old: PartitionView[];
  targets: TargetPage;
  message: string;
  busy: string[];
}
