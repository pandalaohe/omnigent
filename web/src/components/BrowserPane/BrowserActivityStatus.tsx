import { useBrowserActivity } from "@/lib/browserActivity";
import { browserViewOwnerId } from "@/lib/browserTabs";
import { useChatStore } from "@/store/chatStore";

export function BrowserActivityStatus({ viewId }: { viewId: string }) {
  const operating = useBrowserActivity(viewId);
  if (!operating) return null;
  return (
    <div className="flex shrink-0 items-center justify-between gap-2 border-border border-b bg-card px-3 py-1 text-muted-foreground text-xs">
      <span role="status">Session is operating this page</span>
      <button
        type="button"
        className="rounded px-2 py-1 text-foreground hover:bg-muted focus-visible:outline focus-visible:outline-ring"
        onClick={() => void useChatStore.getState().stop(browserViewOwnerId(viewId))}
      >
        Stop
      </button>
    </div>
  );
}
