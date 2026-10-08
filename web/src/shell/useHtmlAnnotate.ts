// Parent side of the in-frame element-annotation channel (design §2.5): owns
// the annotate MessagePort, ships the runtime parts when the page needs them,
// mirrors the mode state, saves picked annotations and sends batches.
//
// The channel lifetime mirrors the comment bridge: one fresh MessageChannel per
// iframe document (init on load). The frame is opaque-origin and its traffic is
// untrusted, so every inbound message goes through `parseAnnotateMessage`.

import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { showToast } from "@/components/ui/toast";
import { useOptionalCommentSender } from "@/hooks/CommentSenderContext";
import { commentsQueryKey, type Comment } from "@/hooks/useComments";
import { uploadFile } from "@/lib/filesApi";
import { authenticatedFetch } from "@/lib/identity";
import {
  KEYBOARD_SHORTCUTS_CHANGED_EVENT,
  eventMatchesShortcutAction,
  resolvedShortcutChords,
} from "@/lib/keyboardShortcutPreferences";
import {
  type ElementAnchorScreenshot,
  type ElementAnchorV1,
  decodeElementAnchor,
  encodeElementAnchor,
  elementAnchorRange,
  isElementAnchor,
} from "./annotationAnchor";
import { loadRuntimeSources, type AnnotateRuntimePart } from "./annotate/runtimeSources";
import {
  ANNOTATE_MSG,
  ANNOTATE_SOURCE,
  type AnnotatePicked,
  type AnnotatePickedScreenshot,
  type AnnotateViewportRect,
  parseAnnotateMessage,
} from "./htmlAnnotateBridge";

export interface UseHtmlAnnotateArgs {
  iframe: HTMLIFrameElement | null;
  nonce: string | null;
  path: string;
  sessionId: string;
  comments: Comment[];
  onSelectComment: (commentId: string) => void;
}

/** A picked annotation waiting for the owner composer's note. */
export interface PendingPick {
  anchor: ElementAnchorV1;
  screenshot: AnnotatePickedScreenshot | null;
  /** The picked rect in the frame's viewport, for positioning the composer. */
  viewportRect: AnnotateViewportRect;
  label: string;
}

export interface UseHtmlAnnotateResult {
  /** The frame's annotate stub answered and the channel is live. */
  available: boolean;
  /** Annotation mode is on inside the frame. */
  on: boolean;
  toggle: () => void;
  /** Comment ids whose saved anchor no longer resolves on this page. */
  orphanIds: ReadonlySet<string>;
  /** Set after a pick; the composer edits this pick's note. */
  pendingPick: PendingPick | null;
  /** Save the pending pick, then send its batch when action is "send". */
  submitPick: (note: string, action: "stack" | "send") => void;
  /** Drop the pending pick without saving it. */
  cancelPick: () => void;
}

const EMPTY_ORPHANS: ReadonlySet<string> = new Set();

const UNLOADABLE_TOAST = "Annotation is unavailable on this page";

/** The note (comment body) cap; the owner composer may leave it empty. */
const MAX_NOTE_CHARS = 4000;

const JPEG_DATA_URL_PREFIX = "data:image/jpeg;base64,";
const PNG_DATA_URL_PREFIX = "data:image/png;base64,";

/** The data URL's declared image type, or null for anything else (T10). */
function screenshotMime(dataUrl: string): "image/jpeg" | "image/png" | null {
  if (dataUrl.startsWith(JPEG_DATA_URL_PREFIX)) return "image/jpeg";
  if (dataUrl.startsWith(PNG_DATA_URL_PREFIX)) return "image/png";
  return null;
}

/** JPEG starts FF D8 FF; PNG starts 89 50 4E 47. */
function hasImageMagic(bytes: Uint8Array, mime: "image/jpeg" | "image/png"): boolean {
  if (mime === "image/jpeg") return bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff;
  return bytes[0] === 0x89 && bytes[1] === 0x50 && bytes[2] === 0x4e && bytes[3] === 0x47;
}

/**
 * Upload the frame's crop and map it to the anchor's screenshot field. Any
 * failure, including malformed bytes, drops the screenshot (design §2.4,
 * Pseudocode 2) so the annotation still saves; nothing page-derived is logged.
 */
async function uploadScreenshot(
  sessionId: string,
  screenshot: AnnotatePickedScreenshot | null,
): Promise<ElementAnchorScreenshot | null> {
  if (!screenshot) return null;
  try {
    const mime = screenshotMime(screenshot.dataUrl);
    const comma = screenshot.dataUrl.indexOf(",");
    if (!mime || comma < 0) throw new Error("unsupported screenshot encoding");
    const binary = atob(screenshot.dataUrl.slice(comma + 1));
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
    if (!hasImageMagic(bytes, mime)) throw new Error("screenshot bytes are not an image");
    const extension = mime === "image/jpeg" ? "jpg" : "png";
    const file = new File([bytes], `annotation-${Date.now()}.${extension}`, { type: mime });
    const uploaded = await uploadFile(sessionId, file);
    return {
      file_id: uploaded.id,
      filename: uploaded.filename,
      width: screenshot.width,
      height: screenshot.height,
    };
  } catch (error) {
    console.warn(
      "[omni-annotate] screenshot upload failed:",
      error instanceof Error ? error.message : error,
    );
    return null;
  }
}

/**
 * POST a comment to the captured session. `useAddComment`'s mutation function
 * is re-pointed on every render, so its returned mutation cannot be held for a
 * save that resolves after a session switch.
 */
async function postComment(
  sessionId: string,
  payload: {
    path: string;
    start_index: number;
    end_index: number;
    body: string;
    anchor_content?: string | null;
  },
): Promise<Comment> {
  const res = await authenticatedFetch(`/v1/sessions/${encodeURIComponent(sessionId)}/comments`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return (await res.json()) as Comment;
}

/** A row saved by this hook that the comments list has not shown yet. */
interface SavedRow {
  sessionId: string;
  path: string;
  start: number;
}

/**
 * This path's unsent element annotations in start order: the listed drafts
 * plus this hook's saves the list has not caught up to yet.
 */
function draftElementCommentIds(
  comments: Comment[],
  sessionId: string,
  path: string,
  savedRows: ReadonlyMap<string, SavedRow>,
): string[] {
  const entries = comments
    .filter((c) => c.path === path && c.status === "draft" && isElementAnchor(c.anchor_content))
    .map((c) => ({ id: c.id, start: c.start_index }));
  const shown = new Set(comments.map((c) => c.id));
  for (const [id, row] of savedRows) {
    if (row.sessionId === sessionId && row.path === path && !shown.has(id)) {
      entries.push({ id, start: row.start });
    }
  }
  entries.sort((a, b) => a.start - b.start);
  return entries.map((entry) => entry.id);
}

export function useHtmlAnnotate({
  iframe,
  nonce,
  path,
  sessionId,
  comments,
  onSelectComment,
}: UseHtmlAnnotateArgs): UseHtmlAnnotateResult {
  const [available, setAvailable] = useState(false);
  const [on, setOn] = useState(false);
  const [orphanIds, setOrphanIds] = useState<ReadonlySet<string>>(EMPTY_ORPHANS);
  const [pendingPick, setPendingPick] = useState<PendingPick | null>(null);

  const sender = useOptionalCommentSender();
  const queryClient = useQueryClient();

  // Latest args and sender for message handlers, which outlive a render.
  const nonceRef = useRef(nonce);
  nonceRef.current = nonce;
  const pathRef = useRef(path);
  pathRef.current = path;
  const commentsRef = useRef(comments);
  commentsRef.current = comments;
  const onSelectCommentRef = useRef(onSelectComment);
  onSelectCommentRef.current = onSelectComment;
  const iframeRef = useRef(iframe);
  iframeRef.current = iframe;
  const sessionIdRef = useRef(sessionId);
  sessionIdRef.current = sessionId;
  const senderRef = useRef(sender);
  senderRef.current = sender;

  // Channel state; every field resets when a new document loads.
  const portRef = useRef<MessagePort | null>(null);
  const generationRef = useRef(0);
  const readyRef = useRef(false);
  const availableRef = useRef(false);
  const onRef = useRef(false);
  const loadedRef = useRef<Record<AnnotateRuntimePart, boolean>>({ core: false, pick: false });
  const loadingRef = useRef<Record<AnnotateRuntimePart, Promise<boolean> | null>>({
    core: null,
    pick: null,
  });
  const waitersRef = useRef<Record<AnnotateRuntimePart, ((ok: boolean) => void) | null>>({
    core: null,
    pick: null,
  });

  // Picks are processed one at a time so a burst of submissions cannot build
  // overlapping batches. `savedRows` keeps this hook's saves the list has not
  // shown yet; `deliveredIds` excludes per session the ids a successful send
  // already took. Chain and maps live for the hook's lifetime: switching
  // sessions must not let old queued work resend or run concurrently.
  const pickedChainRef = useRef<Promise<void>>(Promise.resolve());
  const savedRowsRef = useRef<Map<string, SavedRow>>(new Map());
  const deliveredIdsRef = useRef<Map<string, Set<string>>>(new Map());
  // The pick the composer is editing; the ref lets submit/cancel read it
  // without re-creating their callbacks on every render.
  const pendingPickRef = useRef<PendingPick | null>(null);
  // The session and path the open pick was made in; it dies when either moves.
  const pendingPickContextRef = useRef<{ sessionId: string; path: string } | null>(null);

  // Element-anchored comments of this page, in reading order; `n` is the marker
  // number the frame paints. A row whose anchor fails to decode is a text comment.
  const elementItems = useMemo(() => {
    const entries: { id: string; start: number; anchor: ElementAnchorV1 }[] = [];
    for (const c of comments) {
      if (c.path !== path || !isElementAnchor(c.anchor_content)) continue;
      const anchor = decodeElementAnchor(c.anchor_content);
      if (anchor) entries.push({ id: c.id, start: c.start_index, anchor });
    }
    entries.sort((a, b) => a.start - b.start);
    return entries.map((entry, index) => ({ id: entry.id, n: index + 1, anchor: entry.anchor }));
  }, [comments, path]);
  const elementItemsRef = useRef(elementItems);
  elementItemsRef.current = elementItems;

  const setAvailableState = useCallback((value: boolean) => {
    availableRef.current = value;
    setAvailable(value);
  }, []);

  const applyOn = useCallback((value: boolean) => {
    onRef.current = value;
    setOn(value);
  }, []);

  const applyPendingPick = useCallback((value: PendingPick | null) => {
    pendingPickRef.current = value;
    setPendingPick(value);
  }, []);

  const postToFrame = useCallback((message: Record<string, unknown>) => {
    const port = portRef.current;
    const currentNonce = nonceRef.current;
    if (!port || !currentNonce) return;
    try {
      port.postMessage({ source: ANNOTATE_SOURCE, nonce: currentNonce, ...message });
    } catch {
      // The parent may close the port between messages; dropping is safe.
    }
  }, []);

  /**
   * Drop the open pick without saving it and release the frame's selection.
   * `pickDone` goes out while the recorded port is still listening, so callers
   * that close or clear the port must call this first.
   */
  const dismissPendingPick = useCallback((): void => {
    if (!pendingPickRef.current) return;
    applyPendingPick(null);
    pendingPickContextRef.current = null;
    postToFrame({ type: ANNOTATE_MSG.pickDone });
  }, [applyPendingPick, postToFrame]);

  const postShortcut = useCallback(() => {
    postToFrame({
      type: ANNOTATE_MSG.setShortcut,
      bindings: resolvedShortcutChords("toggleAnnotationMode"),
    });
  }, [postToFrame]);

  const ensurePart = useCallback(
    (part: AnnotateRuntimePart): Promise<boolean> => {
      if (loadedRef.current[part]) return Promise.resolve(true);
      const inFlight = loadingRef.current[part];
      if (inFlight) return inFlight;
      const generation = generationRef.current;
      const promise = loadRuntimeSources(part)
        .then((sources): Promise<boolean> => {
          if (generation !== generationRef.current) return Promise.resolve(false);
          return new Promise<boolean>((resolve) => {
            waitersRef.current[part] = resolve;
            postToFrame({ type: ANNOTATE_MSG.loadRuntime, part, sources });
          });
        })
        .catch(() => {
          showToast(UNLOADABLE_TOAST);
          return false;
        })
        .finally(() => {
          if (loadingRef.current[part] === promise) loadingRef.current[part] = null;
        });
      loadingRef.current[part] = promise;
      return promise;
    },
    [postToFrame],
  );

  const syncAnnotations = useCallback(async (): Promise<void> => {
    if (!readyRef.current) return;
    // A reload during the runtime load must not push the old document's list
    // onto the new document's port.
    const generation = generationRef.current;
    const items = elementItemsRef.current;
    if (items.length === 0) {
      postToFrame({ type: ANNOTATE_MSG.setAnnotations, items: [] });
      return;
    }
    if (!loadedRef.current.core && !(await ensurePart("core"))) return;
    if (generation !== generationRef.current || !readyRef.current) return;
    postToFrame({ type: ANNOTATE_MSG.setAnnotations, items });
  }, [ensurePart, postToFrame]);

  const handleRuntimeLoaded = useCallback(
    (part: AnnotateRuntimePart, ok: boolean) => {
      if (ok) {
        loadedRef.current[part] = true;
      } else {
        showToast(UNLOADABLE_TOAST);
        applyOn(false);
      }
      const waiter = waitersRef.current[part];
      waitersRef.current[part] = null;
      waiter?.(ok);
    },
    [applyOn],
  );

  const toggleMode = useCallback(async (): Promise<void> => {
    if (!readyRef.current) return;
    if (onRef.current) {
      postToFrame({ type: ANNOTATE_MSG.setMode, on: false });
      // The picker only echoes in-frame changes, so a parent-driven setMode is
      // mirrored here: without it `on` never flips and the mode cannot be left.
      applyOn(false);
      // Leaving the mode drops the open pick without saving it.
      dismissPendingPick();
      return;
    }
    // Core covers anchoring and markers; pick adds freeze, the picker and the
    // composer. Both must load before the mode can turn on.
    if (!(await ensurePart("core")) || !readyRef.current) return;
    if (!(await ensurePart("pick")) || !readyRef.current) return;
    postToFrame({ type: ANNOTATE_MSG.setMode, on: true });
    applyOn(true);
  }, [applyOn, dismissPendingPick, ensurePart, postToFrame]);

  const toggle = useCallback((): void => {
    void toggleMode();
  }, [toggleMode]);

  // A pick is accepted only while the parent itself holds the mode on (page JS
  // shares the frame's realm and can forge traffic), and only one at a time:
  // the open composer owns the pending pick.
  const handlePicked = useCallback(
    (message: AnnotatePicked): void => {
      if (pendingPickRef.current) return;
      pendingPickContextRef.current = { sessionId: sessionIdRef.current, path: pathRef.current };
      applyPendingPick({
        anchor: message.anchor,
        screenshot: message.screenshot,
        viewportRect: message.viewportRect,
        label: message.anchor.target.label,
      });
    },
    [applyPendingPick],
  );

  const submitPick = useCallback(
    (note: string, action: "stack" | "send"): void => {
      const picked = pendingPickRef.current;
      if (!picked) return;
      // Capture the submission's context before the first await: the upload and
      // the save resolve later, by which time the hook may have moved on. The
      // save posts to the session the user picked in, even if the panel has
      // already switched away.
      const pickedSessionId = sessionIdRef.current;
      const pickedPath = pathRef.current;
      const pickedSender = senderRef.current;
      const body = note.trim().slice(0, MAX_NOTE_CHARS);
      dismissPendingPick();

      const run = async (): Promise<void> => {
        const anchor: ElementAnchorV1 = {
          ...picked.anchor,
          screenshot: await uploadScreenshot(pickedSessionId, picked.screenshot),
        };
        let saved: Comment;
        try {
          saved = await postComment(pickedSessionId, {
            path: pickedPath,
            body,
            ...elementAnchorRange(anchor),
            anchor_content: encodeElementAnchor(anchor),
          });
        } catch {
          showToast("Couldn't save annotation");
          return;
        }
        void queryClient.invalidateQueries({ queryKey: commentsQueryKey(pickedSessionId) });
        void queryClient.invalidateQueries({
          queryKey: commentsQueryKey(pickedSessionId, pickedPath),
        });
        savedRowsRef.current.set(saved.id, {
          sessionId: pickedSessionId,
          path: pickedPath,
          start: saved.start_index,
        });
        if (action !== "send") return;
        if (sessionIdRef.current !== pickedSessionId || pathRef.current !== pickedPath) {
          showToast("Return to this session before sending.");
          return;
        }
        if (!pickedSender) {
          showToast("No agent bound to this session yet.");
          return;
        }
        // Build the batch now, from the current list plus this hook's not-yet-
        // listed saves, so an earlier queued pick's row is included. Delivery
        // memory is per session and survives switches.
        const delivered = deliveredIdsRef.current.get(pickedSessionId);
        const ids = draftElementCommentIds(
          commentsRef.current,
          pickedSessionId,
          pickedPath,
          savedRowsRef.current,
        ).filter((id) => !delivered?.has(id));
        if (ids.length === 0) return;
        try {
          const result = await pickedSender.mutateAsync({ comment_ids: ids, respectQueue: true });
          if (result?.delivered !== false) {
            let sentIds = deliveredIdsRef.current.get(pickedSessionId);
            if (!sentIds) {
              sentIds = new Set();
              deliveredIdsRef.current.set(pickedSessionId, sentIds);
            }
            for (const id of ids) sentIds.add(id);
          }
        } catch (error) {
          showToast(error instanceof Error ? error.message : "Couldn't send annotations");
        }
      };

      // A failed pick must not stall the picks queued behind it.
      pickedChainRef.current = pickedChainRef.current.then(run, run);
    },
    [dismissPendingPick, queryClient],
  );

  const cancelPick = useCallback((): void => {
    dismissPendingPick();
  }, [dismissPendingPick]);

  // Establish the annotate channel on every iframe document load. The bridge
  // effect and this one attach their own `load` listeners; the reset makes a
  // stale document's ready/state unable to leak into the next one.
  useEffect(() => {
    if (!iframe || !nonce) return;
    let channel: MessageChannel | null = null;

    const resetDocument = () => {
      generationRef.current += 1;
      readyRef.current = false;
      loadedRef.current = { core: false, pick: false };
      loadingRef.current = { core: null, pick: null };
      // Picks queued by the old document keep running: they are real user
      // input and must still save into their originating session.
      for (const part of ["core", "pick"] as const) {
        const waiter = waitersRef.current[part];
        waitersRef.current[part] = null;
        waiter?.(false);
      }
      setAvailableState(false);
      applyOn(false);
      dismissPendingPick();
      setOrphanIds(EMPTY_ORPHANS);
    };

    const handleMessage = (data: unknown) => {
      const message = parseAnnotateMessage(data, nonce);
      if (!message) return;
      switch (message.type) {
        case ANNOTATE_MSG.ready:
          readyRef.current = true;
          setAvailableState(true);
          postShortcut();
          void syncAnnotations();
          break;
        case ANNOTATE_MSG.runtimeLoaded:
          handleRuntimeLoaded(message.part, message.ok);
          break;
        case ANNOTATE_MSG.modeChanged:
          // Only the parent may turn the mode on; page JS shares the frame's
          // realm and can forge `on:true`, so it is ignored. A frame leave is
          // always honest because turning off is safe.
          if (!message.on) {
            applyOn(false);
            dismissPendingPick();
          }
          break;
        case ANNOTATE_MSG.toggleRequested:
          // Page JS shares the frame's realm and can forge this message. Only a
          // real keydown/click in the child frame grants transient activation
          // to this window; page scripts cannot set it. An absent activation
          // API fails closed. Turning off needs no activation.
          if (!onRef.current && navigator.userActivation?.isActive !== true) break;
          void toggleMode();
          break;
        case ANNOTATE_MSG.resolved:
          setOrphanIds(new Set(message.items.filter((item) => !item.found).map((item) => item.id)));
          break;
        case ANNOTATE_MSG.markerClick:
          onSelectCommentRef.current(message.id);
          break;
        case ANNOTATE_MSG.picked:
          // Picks arrive as untrusted page-realm traffic: only one made while
          // the parent itself holds the mode on may save or send.
          if (onRef.current) handlePicked(message);
          break;
      }
    };

    const onLoad = () => {
      resetDocument();
      const win = iframe.contentWindow;
      if (!win) return;
      channel?.port1.close();
      channel = new MessageChannel();
      channel.port1.onmessage = (event) => handleMessage(event.data);
      portRef.current = channel.port1;
      // targetOrigin "*" is required: the sandboxed frame has an opaque origin.
      win.postMessage({ source: ANNOTATE_SOURCE, nonce, type: ANNOTATE_MSG.init }, "*", [
        channel.port2,
      ]);
    };

    iframe.addEventListener("load", onLoad);
    return () => {
      iframe.removeEventListener("load", onLoad);
      // The frame still listens; release the open pick before the port closes.
      resetDocument();
      channel?.port1.close();
      portRef.current = null;
    };
  }, [
    iframe,
    nonce,
    applyOn,
    dismissPendingPick,
    handlePicked,
    handleRuntimeLoaded,
    postShortcut,
    setAvailableState,
    syncAnnotations,
    toggleMode,
  ]);

  // Push this path's element annotations whenever the list changes (a save, a
  // delete, a refetch) or the channel first becomes ready.
  useEffect(() => {
    void syncAnnotations();
  }, [syncAnnotations, elementItems]);

  // A save this hook made is its memory only until the list has shown it; from
  // then on the current list decides, so a row deleted after listing cannot be
  // resurrected by the not-yet-listed fallback.
  useEffect(() => {
    if (savedRowsRef.current.size === 0) return;
    const shown = new Set(comments.map((c) => c.id));
    for (const id of savedRowsRef.current.keys()) {
      if (shown.has(id)) savedRowsRef.current.delete(id);
    }
  }, [comments]);

  // An open pick belongs to the session and path it was made in; either moving
  // dismisses it. The frame may already have reloaded, but as long as this port
  // listens the release goes out first.
  useEffect(() => {
    const picked = pendingPickContextRef.current;
    if (!picked) return;
    if (picked.sessionId !== sessionId || picked.path !== path) dismissPendingPick();
  }, [sessionId, path, dismissPendingPick]);

  // Re-send the resolved chords on rebinding, so a live change applies without
  // a frame reload (T11).
  useEffect(() => {
    const onShortcutsChanged = () => {
      if (readyRef.current) postShortcut();
    };
    window.addEventListener(KEYBOARD_SHORTCUTS_CHANGED_EVENT, onShortcutsChanged);
    return () => window.removeEventListener(KEYBOARD_SHORTCUTS_CHANGED_EVENT, onShortcutsChanged);
  }, [postShortcut]);

  // Parent-focus shortcut. The frame routes its own chord as toggleRequested;
  // this only fires while the frame is the visible one for this viewer.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.repeat || !availableRef.current) return;
      if (!eventMatchesShortcutAction(event, "toggleAnnotationMode")) return;
      const frame = iframeRef.current;
      if (!frame || frame.getClientRects().length === 0) return;
      event.preventDefault();
      void toggleMode();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [toggleMode]);

  return { available, on, toggle, orphanIds, pendingPick, submitPick, cancelPick };
}
