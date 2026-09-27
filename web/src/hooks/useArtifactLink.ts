// Artifact-relay links: mint a capability URL for a workspace file and read
// the bytes it serves. The URL carries its own token, so the read is a bare
// same-origin GET — authenticatedFetch would stamp identity headers and route
// through the embed host transport, neither of which an artifact URL wants.
//
// A host-absolute path (leading "/") is sent with `base: "host"` and the slash
// stripped, the same wire form `browseLocationSegment` uses for absolute reads.

import { useEffect, useState } from "react";
import { withBasePath } from "@/lib/basePath";
import { authenticatedFetch } from "@/lib/identity";
import { browseLocationBase } from "@/hooks/useWorkspaceChangedFiles";

export type ArtifactView = "panel" | "raw";

/** Mint response of `POST /v1/sessions/{sid}/artifacts`. */
export interface MintedArtifactLink {
  url: string;
  nonce: string;
  kind: "bundle" | "file";
  /** Unix seconds when a panel-view token dies; null for the raw view. */
  expires_at: number | null;
}

/** An HTTP status from the mint or the artifact read, so callers can branch. */
export class ArtifactLinkError extends Error {
  readonly status: number;

  constructor(status: number) {
    super(`artifact request failed (${status})`);
    this.name = "ArtifactLinkError";
    this.status = status;
  }
}

/** Mint a panel/raw-view URL for `path` (cookie auth, deterministic per key). */
export async function mintArtifactLink(
  conversationId: string,
  { path, view }: { path: string; view: ArtifactView },
): Promise<MintedArtifactLink> {
  const base = browseLocationBase(path);
  const body: { path: string; view: ArtifactView; base?: "host" } = {
    path: base ? path.replace(/^\//, "") : path,
    view,
  };
  if (base) body.base = base;
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(conversationId)}/artifacts`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
  );
  if (!res.ok) throw new ArtifactLinkError(res.status);
  const link = (await res.json()) as MintedArtifactLink;
  // A server older than the expiry field omits it; treat that as no expiry.
  return {
    ...link,
    expires_at: typeof link.expires_at === "number" ? link.expires_at : null,
  };
}

/**
 * Drop the bridge `<script data-omni-nonce="…">…</script>` tags the server
 * inlines into panel-view HTML, so comment offsets match the file's own bytes.
 * The asset escapes its own `</script`, so the first literal close ends a tag.
 */
export function stripInjectedScripts(html: string, nonce: string): string {
  const escapedNonce = nonce.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return html.replace(
    new RegExp(`<script data-omni-nonce="${escapedNonce}">[\\s\\S]*?</script>`, "g"),
    "",
  );
}

/** GET one panel-view artifact file's stripped source; throws on non-2xx. */
export async function fetchArtifactSource(url: string, nonce: string): Promise<string> {
  // oxlint-disable-next-line no-restricted-globals -- an artifact URL is token-authorized; identity/embed headers must not be attached.
  const res = await fetch(withBasePath(url));
  if (!res.ok) throw new ArtifactLinkError(res.status);
  return stripInjectedScripts(await res.text(), nonce);
}

export interface ArtifactEntry {
  url: string;
  nonce: string;
  source: string;
  expires_at: number | null;
}

/**
 * Mint the panel view for `path` and read its bytes. A 410 mints once more and
 * retries once: the link can be revoked between the panel opening and the
 * fetch, and a fresh mint for a live key is deterministic. Exported so the
 * viewer's pre-expiry refresh can reload the frame under a fresh token and
 * nonce.
 */
export async function fetchArtifactEntry(
  conversationId: string,
  path: string,
): Promise<ArtifactEntry> {
  let link = await mintArtifactLink(conversationId, { path, view: "panel" });
  let source: string;
  try {
    source = await fetchArtifactSource(link.url, link.nonce);
  } catch (err) {
    if (!(err instanceof ArtifactLinkError) || err.status !== 410) throw err;
    link = await mintArtifactLink(conversationId, { path, view: "panel" });
    source = await fetchArtifactSource(link.url, link.nonce);
  }
  return { url: link.url, nonce: link.nonce, source, expires_at: link.expires_at };
}

/** Map a failed entry load to the panel's one-line message. */
export function artifactErrorMessage(err: unknown): string {
  const status = err instanceof ArtifactLinkError ? err.status : 0;
  switch (status) {
    case 404:
      return "File not found";
    case 410:
      return "Link revoked";
    case 413:
      return "File too large to preview (10 MiB limit)";
    case 502:
      return "The session's host or runner needs an update";
    case 503:
      return "Host offline";
    default:
      return status ? `Preview failed (${status})` : "Preview failed";
  }
}

/** Ready-or-error state of one entry request; `null` while it is in flight. */
export type ArtifactEntryLoad =
  { status: "ready"; entry: ArtifactEntry } | { status: "error"; message: string };

/**
 * Load the panel-view entry for the current inputs. The request key is kept in
 * state so a path/conversation switch reads as "loading" (null) on the very
 * render the inputs changed, before the effect can reset the old result.
 */
export function useArtifactEntry(
  conversationId: string,
  path: string,
  enabled = true,
): ArtifactEntryLoad | null {
  const key = `${conversationId}\u0000${path}`;
  const [loaded, setLoaded] = useState<{ key: string; load: ArtifactEntryLoad } | null>(null);
  useEffect(() => {
    if (!enabled) {
      setLoaded(null);
      return;
    }
    let cancelled = false;
    setLoaded(null);
    fetchArtifactEntry(conversationId, path).then(
      (entry) => {
        if (!cancelled) setLoaded({ key, load: { status: "ready", entry } });
      },
      (err: unknown) => {
        if (!cancelled) {
          setLoaded({ key, load: { status: "error", message: artifactErrorMessage(err) } });
        }
      },
    );
    return () => {
      cancelled = true;
    };
  }, [conversationId, path, enabled, key]);
  return loaded?.key === key ? loaded.load : null;
}
