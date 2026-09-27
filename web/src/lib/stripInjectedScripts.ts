// Leaf helpers for handling the HTML the server serves for an artifact. The
// visitor shell imports this module, so it must stay free of app imports
// (stores, identity, react-query) — see web/vite.config.ts `visit` entry.

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
