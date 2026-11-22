// Sends this tab hasn't seen answered, and queued messages, per session in sessionStorage.
// The server can hold a POST for minutes (a sandbox launch or wake) without persisting
// it, and the queue lives in memory, so a reload would otherwise lose the text.
import { readComposerDraft, type StoredReplyDraft } from "./replyDraft";

export interface UnsentSend {
  stableId: string;
  text: string;
  replyDraft?: StoredReplyDraft;
  /** The sender (`getCurrentUserId`), so another account in this tab can't recover it. */
  authorId: string;
}

/** A failed send the composer restored; a reload keeps the id its delivery proof names. */
export interface RestoredSend {
  stableId: string;
  text: string;
  replyDraft?: StoredReplyDraft;
  /** The sender, as on {@link UnsentSend}. */
  authorId: string;
}

const UNSENT_SENDS_KEY = "omnigent.unsentSends";
const QUEUED_SENDS_KEY = "omnigent.queuedSends";
const RESTORED_SENDS_KEY = "omnigent.restoredSends";

// A page that is going away aborts its open sends, and their cleanup still runs
// as it unloads; keep their records for the next page instead.
let pageHiding = false;
if (typeof window !== "undefined") {
  window.addEventListener("pagehide", () => {
    pageHiding = true;
  });
  window.addEventListener("pageshow", () => {
    pageHiding = false;
  });
}

function parseSend(entry: unknown): UnsentSend | null {
  const draft = readComposerDraft(entry);
  if (!draft?.text || typeof entry !== "object" || entry === null) return null;
  const { stableId, authorId } = entry as { stableId?: unknown; authorId?: unknown };
  // A record with no sender can't be matched to the viewer.
  if (typeof stableId !== "string" || typeof authorId !== "string") return null;
  return { ...draft, stableId, authorId };
}

function parseRestored(entry: unknown): RestoredSend | null {
  const draft = readComposerDraft(entry);
  if (!draft?.text || typeof entry !== "object" || entry === null) return null;
  const { stableId, authorId } = entry as { stableId?: unknown; authorId?: unknown };
  if (typeof stableId !== "string" || typeof authorId !== "string") return null;
  return { ...draft, stableId, authorId };
}

function parseQueue(entry: unknown): UnsentSend[] | null {
  const sends = Array.isArray(entry) ? entry.map(parseSend).filter((send) => send !== null) : [];
  return sends.length > 0 ? sends : null;
}

function read<T>(key: string, parse: (entry: unknown) => T | null): Record<string, T> {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.sessionStorage.getItem(key);
    const entries: unknown = raw ? JSON.parse(raw) : {};
    if (typeof entries !== "object" || entries === null || Array.isArray(entries)) return {};
    const records: Record<string, T> = {};
    for (const [conversationId, entry] of Object.entries(entries)) {
      const record = parse(entry);
      if (record !== null) records[conversationId] = record;
    }
    return records;
  } catch {
    return {};
  }
}

function write(records: Record<string, unknown>, key: string): void {
  if (typeof window === "undefined") return;
  try {
    if (Object.keys(records).length === 0) window.sessionStorage.removeItem(key);
    else window.sessionStorage.setItem(key, JSON.stringify(records));
  } catch {
    // Storage full or unavailable: a reload mid-send loses the text, as before.
  }
}

function parseUnsentSends(entry: unknown): UnsentSend[] | null {
  if (Array.isArray(entry)) return parseQueue(entry);
  const legacy = parseSend(entry);
  return legacy === null ? null : [legacy];
}

/** Record a message while it is uploading or awaiting its POST response. */
export function rememberUnsentSend(conversationId: string, send: UnsentSend): void {
  const records = read(UNSENT_SENDS_KEY, parseUnsentSends);
  const others = records[conversationId]?.filter((entry) => entry.stableId !== send.stableId) ?? [];
  write({ ...records, [conversationId]: [...others, send] }, UNSENT_SENDS_KEY);
}

/** Drop only the send that settled. Other messages may still be uploading. */
export function forgetUnsentSend(conversationId: string, stableId: string): void {
  if (pageHiding) return;
  const records = read(UNSENT_SENDS_KEY, parseUnsentSends);
  const sends = records[conversationId];
  if (!sends?.some((send) => send.stableId === stableId)) return;
  const remaining = sends.filter((send) => send.stableId !== stableId);
  const others = Object.entries(records).filter(([id]) => id !== conversationId);
  write(
    Object.fromEntries(remaining.length > 0 ? [...others, [conversationId, remaining]] : others),
    UNSENT_SENDS_KEY,
  );
}

/** The messages this tab left unanswered in a session, in submission order. */
export function peekUnsentSends(conversationId: string): UnsentSend[] {
  return read(UNSENT_SENDS_KEY, parseUnsentSends)[conversationId] ?? [];
}

/** Record the messages a session's queue holds, in order; an empty list clears it. */
export function rememberQueuedSends(conversationId: string, sends: UnsentSend[]): void {
  if (pageHiding) return;
  const queues = Object.entries(read(QUEUED_SENDS_KEY, parseQueue));
  const others = queues.filter(([id]) => id !== conversationId);
  write(
    Object.fromEntries(sends.length > 0 ? [...others, [conversationId, sends]] : others),
    QUEUED_SENDS_KEY,
  );
}

/** The messages kept for a session's queue, in order. */
export function peekQueuedSends(conversationId: string): UnsentSend[] {
  return read(QUEUED_SENDS_KEY, parseQueue)[conversationId] ?? [];
}

/** Track the failed send a session's composer now holds; the latest restore wins. */
export function rememberRestoredSend(conversationId: string, send: RestoredSend): void {
  write({ ...read(RESTORED_SENDS_KEY, parseRestored), [conversationId]: send }, RESTORED_SENDS_KEY);
}

/** Stop tracking a session's restored send once its composer no longer holds it. */
export function forgetRestoredSend(conversationId: string): void {
  const sends = read(RESTORED_SENDS_KEY, parseRestored);
  if (!(conversationId in sends)) return;
  const others = Object.entries(sends).filter(([id]) => id !== conversationId);
  write(Object.fromEntries(others), RESTORED_SENDS_KEY);
}

/** The failed send a previous page of this tab restored into a session's composer. */
export function peekRestoredSend(conversationId: string): RestoredSend | null {
  return read(RESTORED_SENDS_KEY, parseRestored)[conversationId] ?? null;
}

/** Clear every record, for isolated tests. */
export function clearUnsentSends(): void {
  write({}, UNSENT_SENDS_KEY);
  write({}, QUEUED_SENDS_KEY);
  write({}, RESTORED_SENDS_KEY);
}
