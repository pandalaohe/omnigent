import { withBasePath } from "@/lib/basePath";
import type { ExtensionExternalPage } from "./types";

export function ExternalPageView({ page }: { page: ExtensionExternalPage }) {
  const proxied = page.kind === "proxied";
  const navigationUrl = proxied ? withBasePath(page.url) : page.url;
  const validProxyPath =
    /^\/v1\/extensions\/[a-z0-9.-]+\/proxy\/[a-z0-9_-]+\/(?:\?[^#]*)?(?:#.*)?$/.test(page.url);
  let url: URL | null;
  try {
    url =
      proxied && validProxyPath
        ? new URL(navigationUrl, window.location.origin)
        : new URL(page.url);
  } catch {
    url = null;
  }
  const error =
    (proxied && !validProxyPath) || !url || (url.protocol !== "http:" && url.protocol !== "https:")
      ? "The page must use an absolute HTTP or HTTPS URL."
      : !proxied && url.origin === window.location.origin
        ? "External pages cannot use the application's own origin."
        : null;

  if (error) {
    return (
      <div role="alert" className="flex h-full items-center justify-center p-6">
        <div className="max-w-md rounded-lg border border-destructive/40 bg-card p-6 text-center">
          <h1 className="font-semibold">{page.title} cannot be shown</h1>
          <p className="mt-2 text-sm text-muted-foreground">{error}</p>
        </div>
      </div>
    );
  }

  const blocked = window.location.protocol === "https:" && url?.protocol === "http:";
  return (
    <div className="relative flex h-full min-h-0 w-full flex-col overflow-hidden pt-14 md:pt-12">
      <div className="flex flex-wrap items-center gap-3 border-b px-4 py-2">
        <h1 className="mr-auto font-semibold">{page.title}</h1>
        <button
          type="button"
          className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
          onClick={() =>
            window.open(navigationUrl, "_blank", "popup,width=480,height=720,noopener,noreferrer")
          }
        >
          Open in window
        </button>
        <a
          href={navigationUrl}
          target="_blank"
          rel="noopener noreferrer"
          className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
        >
          Open in new tab
        </a>
      </div>
      {blocked ? (
        <div className="flex flex-1 items-center justify-center p-6">
          <p className="max-w-md text-center text-sm text-muted-foreground">
            This HTTP page cannot be embedded in an HTTPS application. Open it in a window or a new
            tab to continue.
          </p>
        </div>
      ) : (
        <iframe
          title={page.title}
          src={navigationUrl}
          // Proxied pages require an explicit operator-trusted server capability.
          // oxlint-disable-next-line react/iframe-missing-sandbox
          sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox"
          referrerPolicy="no-referrer"
          allow=""
          className="min-h-0 w-full flex-1 border-0 bg-background"
        />
      )}
    </div>
  );
}
