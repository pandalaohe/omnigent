/** Design-mode prompt helpers — pure functions (unit-testable, no React) that
 *  turn a picked element + prompt into a chat message. There is NO backend
 *  route: it's sent as an ordinary user message via `chatStore.send` with the
 *  cropped screenshot as an attachment. */

export { buildDesignModePrompt } from "../../../browsers/shared/evidence";
export type { DesignModeElement } from "../../../browsers/shared/evidence";

/**
 * Convert a base64 data URL (the cropped screenshot) into a `File` for the
 * normal chat-send attachment path. Null if not a usable data: URL.
 *
 * @param dataUrl e.g. "data:image/png;base64,iVBORw0K…"
 * @param filename the attachment filename
 */
export function dataUrlToFile(dataUrl: string | null | undefined, filename: string): File | null {
  if (typeof dataUrl !== "string") return null;
  const match = /^data:([^;,]+)(;base64)?,(.*)$/s.exec(dataUrl);
  if (!match) return null;
  const mime = match[1] || "image/png";
  const isBase64 = !!match[2];
  const data = match[3] ?? "";
  try {
    const raw = isBase64 ? atob(data) : decodeURIComponent(data);
    // Back the bytes with a plain ArrayBuffer (not the SharedArrayBuffer-union
    // TS infers for a bare Uint8Array) so the BlobPart type checks cleanly.
    const buffer = new ArrayBuffer(raw.length);
    const bytes = new Uint8Array(buffer);
    for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
    return new File([buffer], filename, { type: mime });
  } catch {
    return null;
  }
}
