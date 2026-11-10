import { useEffect } from "react";
import { onArtifactOpenRequest } from "@/lib/artifactOpenBus";
import type { OpenFileOptions } from "@/shell/FileViewerContext";

export function useArtifactOpenRequest(
  conversationId: string | undefined,
  enabled: boolean,
  openFile: (path: string, options?: OpenFileOptions) => void,
): void {
  useEffect(() => {
    if (!conversationId || !enabled) return;
    return onArtifactOpenRequest(conversationId, (event) =>
      openFile(event.path, { preview: true }),
    );
  }, [conversationId, enabled, openFile]);
}
