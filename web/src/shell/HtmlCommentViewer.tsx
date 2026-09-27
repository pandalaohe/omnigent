// Comment-enabled HTML preview: renders agent-generated HTML in the same
// sandboxed iframe as the read-only preview, but injects a bridge script so
// users can select rendered text and attach review comments — parity with the
// Markdown (TipTap) and code (Monaco/Shiki) comment surfaces.
//
// The iframe stays sandboxed WITHOUT `allow-same-origin` (see HTML_PREVIEW_SANDBOX),
// so the parent can't touch its DOM directly. All selection capture and
// highlight painting happens inside the iframe via the injected bridge, relayed
// over a private MessageChannel. See htmlCommentBridge.ts for the protocol and
// trust model.
//
// Standalone the frame loads the artifact URL as `src`, so relative resources
// and in-bundle links resolve inside the bundle and the server inlines the
// bridge into every panel page. Embed mode (a host fetcher is configured, which
// cannot carry an iframe `src`) keeps the client-injected srcdoc.

import { createPortal } from "react-dom";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { MessageSquarePlusIcon } from "lucide-react";
import type { Comment } from "@/hooks/useComments";
import { useCanEdit } from "@/hooks/usePermissions";
import { fetchArtifactSource, useArtifactEntry } from "@/hooks/useArtifactLink";
import { getEmbedRoot, hasOmnigentHostFetcher } from "@/lib/host";
import { withBasePath } from "@/lib/basePath";
import { randomUUID } from "@/lib/randomUUID";
import { type ActiveSelection, HTML_PREVIEW_SANDBOX } from "./codeViewerHelpers";
import {
  anchorOccurrence,
  BRIDGE_MSG,
  BRIDGE_SOURCE,
  findAnchorInSource,
  injectCommentBridge,
  parseBridgeMessage,
} from "./htmlCommentBridge";
import { TruncatedBanner } from "./TruncatedBanner";

interface HtmlCommentViewerProps {
  conversationId: string;
  /** Opened file's path: workspace-relative, or host-absolute with a leading "/". */
  path: string;
  /** Raw HTML source — the embed (srcdoc) fallback's document and comment source. */
  content: string;
  truncated: boolean;
  comments: Comment[];
  activeSelection: ActiveSelection | null;
  onSetActiveSelection: (sel: ActiveSelection | null) => void;
  /** Lifts the page the frame currently displays (null once it unmounts). */
  onFrameChange?: (frame: { path: string; source: string } | null) => void;
}

/** Floating "Add comment" button position + the resolved selection it commits. */
interface FloatingAnchor {
  x: number;
  y: number;
  start_index: number;
  end_index: number;
  anchor_content: string;
}

function genNonce(): string {
  return randomUUID();
}

/** Bridge payload for one comment: its id, anchor text, and which occurrence of
 * that text (by source offset) it belongs to — so repeated anchor text such as
 * a title reused in the body highlights only the commented instance. */
function commentPayload(source: string, c: Comment) {
  const anchor = c.anchor_content ?? "";
  return {
    id: c.id,
    anchor_content: anchor,
    occ: anchorOccurrence(source, anchor, c.start_index),
  };
}

/** Bridge payload for the active selection, or null. */
function activePayload(source: string, sel: ActiveSelection | null) {
  if (!sel) return null;
  return {
    anchor_content: sel.anchor_content,
    occ: anchorOccurrence(source, sel.anchor_content, sel.start_index),
    comment_id: sel.comment_id,
  };
}

// The bridge reports its `location.pathname`; this pulls out the path inside
// the bundle (the last segment of the route is the token).
const ARTIFACT_TAIL_RE = /\/v1\/artifacts\/[^/]+\/(.*)$/;

/** How long after an iframe load we wait for the bridge's `ready`. */
const BRIDGE_READY_TIMEOUT_MS = 4000;

/** Directory of the entry (posix); "/" for a root-level absolute entry. */
function bundleRoot(entryPath: string): string {
  const slash = entryPath.lastIndexOf("/");
  if (slash === -1) return "";
  if (slash === 0) return "/";
  return entryPath.slice(0, slash);
}

function joinBundlePath(root: string, rel: string): string {
  if (!root) return rel;
  if (root === "/") return `/${rel}`;
  return `${root}/${rel}`;
}

/** The page the frame is showing, derived from the bridge's `ready.pathname`. */
interface FramePage {
  /** Workspace path of the page, for comments. */
  pagePath: string;
  /** Still-encoded path within the bundle, for the source fetch URL. */
  tail: string;
}

function framePageFor(pathname: string | undefined, entryPath: string): FramePage | null {
  if (!pathname) return null;
  const match = ARTIFACT_TAIL_RE.exec(pathname);
  if (!match) return null;
  const tail = match[1];
  let decoded = tail;
  try {
    decoded = decodeURIComponent(tail);
  } catch {
    // Malformed escape — keep the raw form rather than dropping navigation.
  }
  return { pagePath: joinBundlePath(bundleRoot(entryPath), decoded), tail };
}

/** `/v1/artifacts/<token>/` prefix of a minted URL, for sibling page fetches. */
function artifactUrlPrefix(url: string): string | null {
  const match = /^(\/v1\/artifacts\/[^/]+\/)/.exec(url);
  return match ? match[1] : null;
}

export function HtmlCommentViewer({
  conversationId,
  path,
  content,
  truncated,
  comments,
  activeSelection,
  onSetActiveSelection,
  onFrameChange,
}: HtmlCommentViewerProps) {
  const canEdit = useCanEdit(conversationId);
  const isEmbed = hasOmnigentHostFetcher();

  // Embed mode cannot carry an iframe `src` through the host fetcher: it keeps
  // the client-injected srcdoc (bridge + fresh nonce per content load, which
  // also re-establishes the channel and clears stale highlights).
  const embedDoc = useMemo(() => {
    if (!isEmbed) return null;
    const n = genNonce();
    return { nonce: n, srcDoc: injectCommentBridge(content, n) };
  }, [isEmbed, content]);

  const load = useArtifactEntry(conversationId, path, !isEmbed);
  const entry = load?.status === "ready" ? load.entry : null;
  const errorMessage = load?.status === "error" ? load.message : null;
  const nonce = embedDoc?.nonce ?? entry?.nonce ?? null;

  // The page the frame currently displays: the entry once loaded, then whatever
  // the bridge reports after in-frame navigation. A navigation starts a fresh
  // source fetch, and until it lands `page` still shows the previous page, so
  // `sourcePending` blocks selection handling against those stale bytes.
  const [page, setPage] = useState<{ path: string; source: string } | null>(null);
  const sourcePendingRef = useRef(false);
  // Bumped on every navigation and input change; a source response whose
  // generation is stale must not overwrite fresher state.
  const navGenRef = useRef(0);
  // Path last reported for display, mirrored by showPage so a `ready` can tell
  // a replaced document from the entry's own first handshake.
  const displayedPathRef = useRef<string | null>(null);
  // A mount's first frame document is the entry; a later `load` replaces it.
  const entryDocumentLoadedRef = useRef(false);
  const [bridgeMissing, setBridgeMissing] = useState(false);

  const iframeRef = useRef<HTMLIFrameElement>(null);
  const portRef = useRef<MessagePort | null>(null);
  const [floating, setFloating] = useState<FloatingAnchor | null>(null);

  // Single writer for the displayed page: keeps displayedPathRef in lockstep
  // with the state the `ready` handler compares against.
  const showPage = useCallback((next: { path: string; source: string } | null) => {
    displayedPathRef.current = next?.path ?? null;
    setPage(next);
  }, []);

  // Latest values for the port message handler without re-establishing the channel.
  const commentsRef = useRef(comments);
  commentsRef.current = comments;
  const activeSource = page?.source ?? content;
  const sourceRef = useRef(activeSource);
  sourceRef.current = activeSource;
  const onSetActiveSelectionRef = useRef(onSetActiveSelection);
  onSetActiveSelectionRef.current = onSetActiveSelection;
  const activeSelectionRef = useRef(activeSelection);
  activeSelectionRef.current = activeSelection;
  const onFrameChangeRef = useRef(onFrameChange);
  onFrameChangeRef.current = onFrameChange;

  // Reset per-file page state; FileViewer resets its own frame on path change.
  // The generation bump drops source fetches started for the previous inputs.
  useEffect(() => {
    navGenRef.current += 1;
    sourcePendingRef.current = false;
    entryDocumentLoadedRef.current = false;
    showPage(null);
    setBridgeMissing(false);
  }, [conversationId, path, isEmbed, showPage]);

  // The entry's source is known as soon as its fetch lands.
  useEffect(() => {
    if (!entry) return;
    sourcePendingRef.current = false;
    showPage({ path, source: entry.source });
  }, [entry, path, showPage]);

  // Lift the displayed page so comments are read/written under its path; the
  // frame going away falls comments back to the viewer's own file.
  useEffect(() => {
    if (page) onFrameChangeRef.current?.({ path: page.path, source: page.source });
  }, [page]);
  useEffect(() => () => onFrameChangeRef.current?.(null), []);

  // Establish the MessageChannel once the iframe document has loaded. Parent-
  // initiated handshake (post init on load) avoids a ready/listen race.
  useEffect(() => {
    const iframe = iframeRef.current;
    if (!iframe || !nonce) return;
    let channel: MessageChannel | null = null;
    let readyTimer: number | null = null;

    const clearReadyTimer = () => {
      if (readyTimer !== null) {
        window.clearTimeout(readyTimer);
        readyTimer = null;
      }
    };

    // A `ready` names the page the frame is showing. Pages other than the entry
    // need their own source (the server injects the bridge into every panel
    // page), fetched fresh on every visit: the file may have changed since the
    // previous visit, so a cached source would anchor comments against old
    // bytes. The entry's source stays the one this mount's hook fetched.
    const handleFrameReady = (pathname: string | undefined) => {
      // A new document answered, so any source response still in flight for
      // the previous one is obsolete — even if it carries the same path.
      navGenRef.current += 1;
      const framePage = framePageFor(pathname, path);
      if (!framePage || !entry) return;
      const { pagePath } = framePage;
      // A ready naming another page means the frame replaced its document:
      // drop the previous page's selection and floating composer. The entry's
      // first handshake names the page shown, keeping a parent-applied one.
      if (displayedPathRef.current !== null && displayedPathRef.current !== pagePath) {
        setFloating(null);
        onSetActiveSelectionRef.current(null);
      }
      if (pagePath === path) {
        sourcePendingRef.current = false;
        showPage({ path, source: entry.source });
        return;
      }
      sourcePendingRef.current = true;
      const gen = navGenRef.current;
      const prefix = artifactUrlPrefix(entry.url);
      if (!prefix) {
        // No minted prefix to build a sibling URL from: keep the page path so
        // comments still save under it, but anchor nothing.
        sourcePendingRef.current = false;
        showPage({ path: pagePath, source: "" });
        return;
      }
      fetchArtifactSource(prefix + framePage.tail, entry.nonce).then(
        (source) => {
          if (navGenRef.current !== gen) return; // the frame moved on
          sourcePendingRef.current = false;
          showPage({ path: pagePath, source });
        },
        () => {
          if (navGenRef.current !== gen) return;
          sourcePendingRef.current = false;
          // The frame shows the server's error page for this file; keep the
          // page path so comments still save under it, but anchor nothing.
          showPage({ path: pagePath, source: "" });
        },
      );
    };

    const handleInbound = (raw: unknown) => {
      const msg = parseBridgeMessage(raw, nonce);
      if (!msg) return;
      if (msg.type === BRIDGE_MSG.ready) {
        clearReadyTimer();
        setBridgeMissing(false);
        if (!isEmbed) handleFrameReady(msg.pathname);
        postState();
      } else if (msg.type === BRIDGE_MSG.selection) {
        // The displayed page's own source has not arrived yet: anchoring now
        // would compute offsets against the previous page's bytes.
        if (sourcePendingRef.current) return;
        const offsets = findAnchorInSource(sourceRef.current, msg.text, msg.occ);
        // Selecting a range that already has a comment activates it (which
        // scrolls the panel to its card) rather than offering to add a new one.
        const existing =
          offsets &&
          commentsRef.current.find(
            (c) =>
              c.status === "draft" &&
              c.start_index === offsets.start_index &&
              c.end_index === offsets.end_index,
          );
        if (existing) {
          onSetActiveSelectionRef.current({
            start_index: existing.start_index,
            end_index: existing.end_index,
            anchor_content: existing.anchor_content ?? "",
            comment_id: existing.id,
          });
          setFloating(null);
          return;
        }
        const rect = iframe.getBoundingClientRect();
        setFloating({
          x: rect.left + msg.rect.left,
          y: rect.top + msg.rect.top - 6,
          start_index: offsets?.start_index ?? 0,
          end_index: offsets?.end_index ?? 0,
          anchor_content: msg.text,
        });
      } else if (msg.type === BRIDGE_MSG.commentClick) {
        if (sourcePendingRef.current) return;
        const c = commentsRef.current.find((x) => x.id === msg.id);
        if (c) {
          onSetActiveSelectionRef.current({
            start_index: c.start_index,
            end_index: c.end_index,
            anchor_content: c.anchor_content ?? "",
            comment_id: c.id,
          });
        }
        setFloating(null);
      } else if (msg.type === BRIDGE_MSG.selectionCleared) {
        onSetActiveSelectionRef.current(null);
        setFloating(null);
      }
    };

    const postState = () => {
      const port = portRef.current;
      // While a linked page's source is pending, the previous page's anchors
      // would paint highlights on unrelated text in the new document.
      if (!port || sourcePendingRef.current) return;
      port.postMessage({
        source: BRIDGE_SOURCE,
        nonce,
        type: BRIDGE_MSG.setComments,
        comments: commentsRef.current.map((c) => commentPayload(sourceRef.current, c)),
      });
      port.postMessage({
        source: BRIDGE_SOURCE,
        nonce,
        type: BRIDGE_MSG.setActive,
        active: activePayload(sourceRef.current, activeSelectionRef.current),
      });
    };

    const onLoad = () => {
      // A different document is loading: source fetches for the old one are
      // obsolete even before its `ready` arrives.
      navGenRef.current += 1;
      if (!isEmbed) {
        if (entryDocumentLoadedRef.current) {
          // A later `load` replaced the entry document: the previous page's
          // selection and composer no longer apply, and nothing may anchor
          // until this page's `ready` and source arrive.
          setFloating(null);
          onSetActiveSelectionRef.current(null);
          sourcePendingRef.current = true;
        }
        entryDocumentLoadedRef.current = true;
      }
      const win = iframe.contentWindow;
      if (!win) return;
      channel?.port1.close();
      channel = new MessageChannel();
      channel.port1.onmessage = (ev) => handleInbound(ev.data);
      portRef.current = channel.port1;
      // targetOrigin "*" is required: the sandboxed frame has an opaque ("null")
      // origin, so we cannot name a concrete origin. The transferred port + the
      // nonce are the trust mechanism, not the origin.
      win.postMessage({ source: BRIDGE_SOURCE, nonce, type: BRIDGE_MSG.init }, "*", [
        channel.port2,
      ]);
      // The bridge is inlined by the server; when the asset is missing from the
      // deployment, nothing answers and comments are impossible on this page.
      clearReadyTimer();
      readyTimer = window.setTimeout(() => setBridgeMissing(true), BRIDGE_READY_TIMEOUT_MS);
    };

    iframe.addEventListener("load", onLoad);
    return () => {
      navGenRef.current += 1;
      iframe.removeEventListener("load", onLoad);
      clearReadyTimer();
      channel?.port1.close();
      portRef.current = null;
    };
  }, [nonce, isEmbed, path, entry, showPage]);

  // Push comment-list changes into the frame.
  useEffect(() => {
    portRef.current?.postMessage({
      source: BRIDGE_SOURCE,
      nonce,
      type: BRIDGE_MSG.setComments,
      comments: comments.map((c) => commentPayload(activeSource, c)),
    });
  }, [comments, activeSource, nonce]);

  // Push active-selection changes into the frame (drives the active highlight).
  useEffect(() => {
    portRef.current?.postMessage({
      source: BRIDGE_SOURCE,
      nonce,
      type: BRIDGE_MSG.setActive,
      active: activePayload(activeSource, activeSelection),
    });
  }, [activeSelection, activeSource, nonce]);

  // Dismiss the floating button on any mousedown in the parent outside of it.
  // (Clicks inside the iframe are relayed as selection/clear messages instead.)
  useEffect(() => {
    const onMouseDown = (e: MouseEvent) => {
      if (!(e.target as HTMLElement).closest("[data-add-comment-btn]")) setFloating(null);
    };
    document.addEventListener("mousedown", onMouseDown);
    return () => document.removeEventListener("mousedown", onMouseDown);
  }, []);

  const preview = entry ? (
    <iframe
      ref={iframeRef}
      src={withBasePath(entry.url)}
      sandbox={HTML_PREVIEW_SANDBOX}
      title="HTML preview"
      className="w-full h-full border-0"
    />
  ) : embedDoc ? (
    <iframe
      ref={iframeRef}
      srcDoc={embedDoc.srcDoc}
      sandbox={HTML_PREVIEW_SANDBOX}
      title="HTML preview"
      className="w-full h-full border-0"
    />
  ) : errorMessage ? (
    <div className="flex items-center justify-center p-8 text-muted-foreground text-ui">
      {errorMessage}
    </div>
  ) : (
    <div className="flex items-center justify-center p-8 text-muted-foreground text-ui">
      Loading…
    </div>
  );

  return (
    <div className="flex h-full flex-col">
      {isEmbed && truncated && <TruncatedBanner />}
      {bridgeMissing && (
        <div className="shrink-0 border-b border-border bg-muted/40 px-4 py-1.5 text-sm text-muted-foreground">
          Comments are unavailable for this preview
        </div>
      )}
      <div className="min-h-0 flex-1">{preview}</div>
      {floating &&
        canEdit &&
        createPortal(
          <button
            data-add-comment-btn
            type="button"
            className="fixed z-50 flex items-center gap-1.5 rounded-md border border-border bg-popover backdrop-blur-xl backdrop-saturate-150 px-2.5 py-1 text-sm font-medium text-foreground shadow-md hover:bg-secondary transition-colors"
            style={{ left: floating.x, top: floating.y, transform: "translateY(-100%)" }}
            onClick={() => {
              onSetActiveSelection({
                start_index: floating.start_index,
                end_index: floating.end_index,
                anchor_content: floating.anchor_content,
              });
              setFloating(null);
            }}
          >
            <MessageSquarePlusIcon className="size-3.5" />
            Add comment
          </button>,
          getEmbedRoot() ?? document.body,
        )}
    </div>
  );
}
