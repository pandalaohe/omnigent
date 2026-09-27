// The `#omni-visit-config` block the server embeds in the visitor shell, and
// the URL derivations the shell needs from it. Leaf module: the visit entry
// must not pull in app code (no stores, identity or react-query).

/** Visit config the server writes into `#omni-visit-config`. */
export interface VisitConfig {
  /** The `h` frame URL, under the deployment's base path. */
  frameUrl: string;
  /** The bridge nonce of the `h` token, shared with the frame's injected bridge. */
  nonce: string;
  /** The bare `g` token, authority for visitor comment POSTs. */
  token: string;
  /** The `g` grant, or null when the gate admitted the visitor without one. */
  grant: string | null;
  /** The entry page path relative to the bundle root. */
  path: string;
  commentsEnabled: boolean;
}

const ARTIFACT_MARKER = "/v1/artifacts/";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

/**
 * Read and validate the config block. Returns null when it is absent or does
 * not carry the expected shape; the shell then renders nothing.
 */
export function readVisitConfig(doc: Document): VisitConfig | null {
  const element = doc.getElementById("omni-visit-config");
  if (!element) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(element.textContent ?? "");
  } catch {
    return null;
  }
  if (!isRecord(parsed)) return null;
  const { frameUrl, nonce, token, grant, path, commentsEnabled } = parsed;
  if (
    typeof frameUrl !== "string" ||
    typeof nonce !== "string" ||
    typeof token !== "string" ||
    (grant !== null && typeof grant !== "string") ||
    typeof path !== "string" ||
    typeof commentsEnabled !== "boolean"
  ) {
    return null;
  }
  return { frameUrl, nonce, token, grant, path, commentsEnabled };
}

/**
 * The bundle-relative page path a frame pathname names, decoded. The bridge
 * reports `location.pathname`, which keeps the `/v1/artifacts/<h>/` route
 * prefix across in-frame navigation. Null when the pathname is not under the
 * artifact route. The route's first marker wins: a bundle may itself contain a
 * `v1/artifacts` directory (mirrors the bridge asset's own boundary rule).
 */
export function framePagePath(pathname: string): string | null {
  const marker = pathname.indexOf(ARTIFACT_MARKER);
  if (marker === -1) return null;
  const rest = pathname.slice(marker + ARTIFACT_MARKER.length);
  const slash = rest.indexOf("/");
  if (slash === -1) return null;
  const tail = rest.slice(slash + 1);
  try {
    return decodeURIComponent(tail);
  } catch {
    return tail;
  }
}

/**
 * The visitor comment POST URL for the deployment the frame was served
 * under: the base-path prefix of `frameUrl` plus `/v1/artifact-comments`.
 */
export function artifactCommentsEndpoint(frameUrl: string): string {
  const marker = frameUrl.indexOf(ARTIFACT_MARKER);
  const prefix = marker === -1 ? "" : frameUrl.slice(0, marker);
  return `${prefix}/v1/artifact-comments`;
}
