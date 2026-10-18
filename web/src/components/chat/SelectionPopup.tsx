import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { CornerUpLeftIcon, MessagesSquareIcon, PanelRightOpenIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { getEmbedRoot } from "@/lib/host";
import { useFileViewer, useFileViewerConversationId } from "@/shell/FileViewerContext";
import { useSelectionFileMatch, type SelectionFileMatch } from "@/hooks/useSelectionFileMatch";

export function SelectionPopup({
  containerRef,
  onReply,
  onAskInSideChat,
}: {
  containerRef: React.RefObject<HTMLElement | null>;
  onReply: (text: string) => void;
  // Present only when the session's harness supports side chat; renders the
  // "Ask in side chat" action beside Reply.
  onAskInSideChat?: (text: string) => void;
}) {
  const [popupPos, setPopupPos] = useState<{ x: number; y: number } | null>(null);
  // The lookup text trails the live selection by a debounce; `popupPos` keeps
  // following every event so the button tracks the pointer.
  const [settledText, setSettledText] = useState("");
  const [listOpen, setListOpen] = useState(false);
  const selectedTextRef = useRef<string>("");
  const openFile = useFileViewer();
  const conversationId = useFileViewerConversationId();
  const { matches, pending, truncated } = useSelectionFileMatch(
    conversationId,
    popupPos ? settledText : "",
  );

  const updatePopup = useCallback(() => {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || sel.rangeCount === 0) {
      setPopupPos(null);
      selectedTextRef.current = "";
      return;
    }

    const text = sel.toString().trim();
    if (!text) {
      setPopupPos(null);
      selectedTextRef.current = "";
      return;
    }

    // Scope to the conversation container — ignore selections in the composer.
    const container = containerRef.current;
    if (!container) {
      setPopupPos(null);
      selectedTextRef.current = "";
      return;
    }
    const anchor = sel.anchorNode;
    if (!anchor || !container.contains(anchor)) {
      setPopupPos(null);
      selectedTextRef.current = "";
      return;
    }

    const range = sel.getRangeAt(0);
    const rect = range.getBoundingClientRect();
    // Position the button just above the selection, horizontally centered.
    setPopupPos({
      x: rect.left + rect.width / 2,
      y: rect.top,
    });
    selectedTextRef.current = text;
  }, [containerRef]);

  useEffect(() => {
    document.addEventListener("mouseup", updatePopup);
    document.addEventListener("selectionchange", updatePopup);
    return () => {
      document.removeEventListener("mouseup", updatePopup);
      document.removeEventListener("selectionchange", updatePopup);
    };
  }, [updatePopup]);

  // Restarting the timer on every position change waits out a drag (each
  // selectionchange moves the popup) before spending a lookup on the text.
  useEffect(() => {
    if (!popupPos) {
      setSettledText("");
      setListOpen(false);
      return;
    }
    setSettledText("");
    setListOpen(false);
    const timer = window.setTimeout(() => setSettledText(selectedTextRef.current), 150);
    return () => window.clearTimeout(timer);
  }, [popupPos]);

  if (!popupPos) return null;

  const clearSelection = () => {
    window.getSelection()?.removeAllRanges();
    setPopupPos(null);
    selectedTextRef.current = "";
    setSettledText("");
    setListOpen(false);
  };
  const openMatch = (match: SelectionFileMatch) => {
    if (openFile) {
      openFile(match.path, match.line ? { line: match.line } : undefined);
    }
    clearSelection();
  };
  const directOpen = matches.length === 1 && !truncated;

  // Panel containment changes the fixed-position origin; use the viewport.
  return createPortal(
    <div
      style={{
        position: "fixed",
        // Translate left by 50% to center the button over the midpoint of the
        // selection, and up by 100% + 6px to sit just above the selection rect.
        left: popupPos.x,
        top: popupPos.y,
        transform: "translate(-50%, calc(-100% - 6px))",
        zIndex: 50,
      }}
    >
      <div className="flex items-center gap-1">
        <Button
          type="button"
          variant="secondary"
          size="sm"
          // Override shared-variant translucent hover — this button floats over text.
          className="gap-1 shadow-md hover:bg-secondary hover:brightness-95 dark:hover:brightness-110"
          onMouseDown={(e) => {
            // Prevent the mousedown from clearing the selection before we read it.
            e.preventDefault();
          }}
          onClick={() => {
            const text = selectedTextRef.current;
            if (text) {
              onReply(text);
              clearSelection();
            }
          }}
        >
          <CornerUpLeftIcon className="size-3.5" />
          Reply ↵
        </Button>
        {onAskInSideChat ? (
          <Button
            type="button"
            variant="secondary"
            size="sm"
            className="gap-1 shadow-md hover:bg-secondary hover:brightness-95 dark:hover:brightness-110"
            onMouseDown={(e) => {
              e.preventDefault();
            }}
            onClick={() => {
              const text = selectedTextRef.current;
              if (text) {
                onAskInSideChat(text);
                clearSelection();
              }
            }}
          >
            <MessagesSquareIcon className="size-3.5" />
            Ask in side chat
          </Button>
        ) : null}
        {openFile && !pending && matches.length > 0 ? (
          <Button
            type="button"
            variant="secondary"
            size="sm"
            className="gap-1 shadow-md hover:bg-secondary hover:brightness-95 dark:hover:brightness-110"
            onMouseDown={(e) => {
              e.preventDefault();
            }}
            onClick={() => {
              if (directOpen) openMatch(matches[0]);
              else setListOpen((open) => !open);
            }}
          >
            <PanelRightOpenIcon className="size-3.5" />
            Open in panel
          </Button>
        ) : null}
      </div>
      {listOpen ? (
        <div className="mt-1 flex flex-col overflow-hidden rounded-md border bg-popover shadow-md">
          {matches.map((match) => (
            <button
              key={match.path}
              type="button"
              title={match.path}
              className="max-w-96 truncate px-2 py-1 text-left font-mono text-xs hover:bg-accent"
              onMouseDown={(e) => {
                e.preventDefault();
              }}
              onClick={() => openMatch(match)}
            >
              {match.path}
            </button>
          ))}
          {truncated ? (
            <div className="px-2 py-1 text-xs text-muted-foreground">Results may be incomplete</div>
          ) : null}
        </div>
      ) : null}
    </div>,
    getEmbedRoot() ?? document.body,
  );
}
