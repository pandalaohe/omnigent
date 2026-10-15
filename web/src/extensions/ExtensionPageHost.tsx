import { NotFoundPage } from "@/pages/NotFoundPage";
import type { ResolvedExtensionPage } from "./types";
import { useRefreshExtensions } from "./ExtensionProvider";
import { ExternalPageView } from "./ExternalPageView";
import { ExtensionViewHost } from "./ExtensionViewHost";
import { useExtensionHostServices } from "./services/useExtensionHostServices";

export function ExtensionPageHost({ resolved }: { resolved: ResolvedExtensionPage }) {
  const refresh = useRefreshExtensions();
  const { methods, events } = useExtensionHostServices(resolved.extension);
  if (resolved.page.kind === "external") return <ExternalPageView page={resolved.page} />;
  if (resolved.page.kind !== undefined && resolved.page.kind !== "view") return <NotFoundPage />;
  return (
    <ExtensionViewHost
      extension={resolved.extension}
      page={resolved.page}
      refresh={refresh}
      methods={methods}
      events={events}
    />
  );
}
