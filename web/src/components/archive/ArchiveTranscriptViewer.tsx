import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { ArrowLeftIcon, CheckIcon, CopyIcon, ExternalLinkIcon, LinkIcon } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { copyText } from "@/lib/clipboard";
import { itemsToBlocks } from "@/lib/itemsToBlocks";
import { buildBubbles, type Bubble } from "@/lib/renderItems";
import {
  fetchSessionItemsPage,
  fetchSessionItemsWindow,
  INITIAL_WINDOW_ITEMS,
} from "@/lib/sessionsApi";
import { getSessionDeepLink, type SessionLinkTarget } from "@/lib/sessionLinks";
import { useNavigate, useRebasePath } from "@/lib/routing";
import { cn } from "@/lib/utils";
import { BubbleView, collectBubbleMarkdown } from "@/pages/ChatPage";
import type { Conversation } from "@/hooks/useConversations";

interface ArchiveTranscriptViewerProps {
  conversation: Conversation | null;
  onBack?: () => void;
  className?: string;
}

interface SelectedQuote {
  text: string;
  target: SessionLinkTarget;
}

function bubbleKey(bubble: Bubble): string {
  if (bubble.kind === "assistant") return `assistant:${bubble.stableId}`;
  if (bubble.kind === "user") return `user:${bubble.stableKey ?? bubble.itemId}`;
  return `${bubble.kind}:${bubble.itemId}`;
}

function bubbleItemIds(bubble: Bubble): string[] {
  if (bubble.kind === "user") return [bubble.itemId];
  if (bubble.kind === "assistant") {
    return bubble.items.flatMap((item) => (item.itemId ? [item.itemId] : []));
  }
  return [bubble.itemId];
}

function bubbleText(bubble: Bubble): string {
  if (bubble.kind === "assistant") return collectBubbleMarkdown(bubble.items).trim();
  if (bubble.kind !== "user") return "";
  return bubble.content
    .flatMap((part) => ("text" in part && typeof part.text === "string" ? [part.text] : []))
    .join("\n")
    .trim();
}

function bubbleTarget(bubble: Bubble): SessionLinkTarget {
  if (bubble.kind === "assistant") {
    const itemId = bubble.items.find((item) => item.itemId)?.itemId;
    return itemId
      ? { kind: "response", responseId: bubble.responseId, itemId }
      : { kind: "session" };
  }
  return { kind: "item", itemId: bubble.itemId };
}

function citation(
  text: string,
  conversation: Conversation,
  link: string,
  target: SessionLinkTarget,
): string {
  const excerpt = text.length > 600 ? `${text.slice(0, 599).trimEnd()}…` : text;
  const targetLabel =
    target.kind === "response"
      ? `response:${target.responseId}`
      : target.kind === "item"
        ? `item:${target.itemId}`
        : "session";
  const clean = (value: string) => value.replace(/[|\r\n⟦⟧]+/g, " ").trim();
  const title = clean(conversation.title?.trim() || "Untitled session");
  const agent = clean(conversation.agent_name ?? "unknown-agent");
  const host = clean(conversation.host_id ?? "unknown-host");
  const quoted = excerpt
    .split(/\r?\n/)
    .map((line) => `> ${line}`)
    .join("\n");
  return `⟦Omnigent reference | session=${conversation.id} | target=${targetLabel} | agent=${agent} | host=${host} | title=${title} | link=${link}⟧\n${quoted}`;
}

export function ArchiveTranscriptViewer({
  conversation,
  onBack,
  className,
}: ArchiveTranscriptViewerProps) {
  const navigate = useNavigate();
  const rebasePath = useRebasePath();
  const transcriptRef = useRef<HTMLDivElement>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const [selectedQuote, setSelectedQuote] = useState<SelectedQuote | null>(null);
  const requestedFocusItemId = conversation?.search_match?.item_id ?? null;
  const [showMatchWindow, setShowMatchWindow] = useState(requestedFocusItemId !== null);
  const focusItemId = showMatchWindow ? requestedFocusItemId : null;
  const sessionId = conversation?.id ?? null;

  useEffect(() => {
    setShowMatchWindow(requestedFocusItemId !== null);
  }, [requestedFocusItemId, sessionId]);

  useEffect(() => {
    setSelectedQuote(null);
    setCopied(null);
  }, [sessionId]);

  const focusedQuery = useQuery({
    queryKey: ["archive-transcript-window", sessionId, focusItemId],
    queryFn: () => fetchSessionItemsWindow(sessionId as string, focusItemId as string),
    enabled: sessionId !== null && focusItemId !== null,
    staleTime: 60_000,
    retry: false,
  });
  const historyQuery = useInfiniteQuery({
    queryKey: ["archive-transcript", sessionId],
    queryFn: ({ pageParam }) =>
      fetchSessionItemsPage(sessionId as string, {
        olderThan: pageParam,
        limit: INITIAL_WINDOW_ITEMS,
      }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => (page.hasMore && page.items[0]?.id ? page.items[0].id : undefined),
    enabled: sessionId !== null && focusItemId === null,
    staleTime: 60_000,
    retry: false,
  });

  const items = useMemo(() => {
    if (focusItemId !== null) return focusedQuery.data?.items ?? [];
    const pages = historyQuery.data?.pages ?? [];
    const seen = new Set<string>();
    return [...pages]
      .reverse()
      .flatMap((page) => page.items)
      .filter((item) => (seen.has(item.id) ? false : (seen.add(item.id), true)));
  }, [focusItemId, focusedQuery.data, historyQuery.data]);
  const bubbles = useMemo(() => buildBubbles(itemsToBlocks(items), null), [items]);
  const loading = focusItemId !== null ? focusedQuery.isLoading : historyQuery.isLoading;
  const error = focusItemId !== null ? focusedQuery.error : historyQuery.error;

  useEffect(() => {
    if (!focusItemId || items.length === 0) return;
    const element = [
      ...(transcriptRef.current?.querySelectorAll<HTMLElement>("[data-archive-item]") ?? []),
    ].find((candidate) => candidate.dataset.archiveItem?.split(" ").includes(focusItemId));
    element?.scrollIntoView?.({ block: "center" });
  }, [focusItemId, items]);

  useEffect(() => {
    if (copied === null) return;
    const timeout = window.setTimeout(() => setCopied(null), 1600);
    return () => window.clearTimeout(timeout);
  }, [copied]);

  if (conversation === null) {
    return (
      <div className={cn("flex min-h-0 flex-1 items-center justify-center p-6", className)}>
        <p className="max-w-xs text-center text-sm text-muted-foreground">
          Select an archived session to read its conversation.
        </p>
      </div>
    );
  }

  const title = conversation.title?.trim() || "Untitled session";
  const sessionLink = getSessionDeepLink(conversation.id, rebasePath);
  const copy = (value: string, key: string) => {
    void copyText(value).then(() => setCopied(key));
  };

  return (
    <section className={cn("flex min-h-0 flex-1 flex-col bg-background", className)}>
      <header className="flex shrink-0 items-center gap-2 border-b border-border px-3 py-2">
        {onBack && (
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            className="md:hidden"
            onClick={onBack}
          >
            <ArrowLeftIcon className="size-4" />
            <span className="sr-only">Back to archive list</span>
          </Button>
        )}
        <div className="min-w-0 flex-1">
          <h2 className="truncate text-sm font-semibold" title={title}>
            {title}
          </h2>
          <p className="truncate font-mono text-[11px] text-muted-foreground">
            {conversation.agent_name ?? "Agent not recorded"} · {conversation.id}
          </p>
        </div>
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          aria-label="Copy session ID"
          onClick={() => copy(conversation.id, "session-id")}
        >
          {copied === "session-id" ? <CheckIcon /> : <CopyIcon />}
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          aria-label="Copy session link"
          onClick={() => copy(sessionLink, "session-link")}
        >
          {copied === "session-link" ? <CheckIcon /> : <LinkIcon />}
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          aria-label="Open full session"
          onClick={() => navigate(`/c/${conversation.id}`)}
        >
          <ExternalLinkIcon />
        </Button>
      </header>

      {selectedQuote && (
        <div className="flex shrink-0 items-center gap-2 border-b bg-accent/40 px-3 py-2">
          <p className="min-w-0 flex-1 truncate text-xs text-muted-foreground">
            “{selectedQuote.text}”
          </p>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() =>
              copy(
                citation(
                  selectedQuote.text,
                  conversation,
                  getSessionDeepLink(conversation.id, rebasePath, selectedQuote.target),
                  selectedQuote.target,
                ),
                "selection",
              )
            }
          >
            {copied === "selection" ? <CheckIcon /> : <CopyIcon />}
            Copy citation
          </Button>
        </div>
      )}

      <div
        ref={transcriptRef}
        data-testid="archive-transcript"
        className="min-h-0 flex-1 overflow-y-auto px-3 py-4"
        onMouseUp={() => {
          const selection = window.getSelection();
          const text = selection?.toString().trim() ?? "";
          const anchor = selection?.anchorNode;
          if (!text || !anchor || !transcriptRef.current?.contains(anchor)) {
            setSelectedQuote(null);
            return;
          }
          const anchorElement =
            anchor.nodeType === Node.ELEMENT_NODE ? (anchor as Element) : anchor.parentElement;
          const article = anchorElement?.closest<HTMLElement>("[data-archive-item]");
          const itemId = article?.dataset.archiveItem?.split(" ")[0];
          if (!itemId) {
            setSelectedQuote(null);
            return;
          }
          const responseId = article.dataset.archiveResponse;
          setSelectedQuote({
            text: text.slice(0, 1200),
            target: responseId
              ? { kind: "response", responseId, itemId }
              : { kind: "item", itemId },
          });
        }}
      >
        {loading ? (
          <p className="text-sm text-muted-foreground">Loading conversation…</p>
        ) : error ? (
          <p className="text-sm text-destructive">Couldn’t load this archived conversation.</p>
        ) : bubbles.length === 0 ? (
          <p className="text-sm text-muted-foreground">This session has no visible messages.</p>
        ) : (
          <div className="mx-auto flex w-full max-w-3xl flex-col gap-3">
            {focusItemId === null && historyQuery.hasNextPage && (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                disabled={historyQuery.isFetchingNextPage}
                onClick={() => void historyQuery.fetchNextPage()}
              >
                {historyQuery.isFetchingNextPage ? "Loading…" : "Load earlier conversation"}
              </Button>
            )}
            {focusItemId !== null && (
              <div className="flex items-center justify-center gap-2 text-xs text-muted-foreground">
                <span>Showing the matching conversation window</span>
                <Button
                  type="button"
                  variant="ghost"
                  size="xs"
                  onClick={() => setShowMatchWindow(false)}
                >
                  View full conversation
                </Button>
              </div>
            )}
            {bubbles.map((bubble) => {
              const ids = bubbleItemIds(bubble);
              const text = bubbleText(bubble);
              const target = bubbleTarget(bubble);
              const link = getSessionDeepLink(conversation.id, rebasePath, target);
              const active = focusItemId !== null && ids.includes(focusItemId);
              return (
                <article
                  key={bubbleKey(bubble)}
                  data-archive-item={ids.join(" ")}
                  data-archive-response={target.kind === "response" ? target.responseId : undefined}
                  data-archive-match={active || undefined}
                  className={cn(
                    "group/archive relative rounded-lg px-2 py-1 ring-offset-background",
                    active && "bg-primary/5 ring-2 ring-primary/40",
                  )}
                >
                  <BubbleView
                    bubble={bubble}
                    readOnly
                    sessionId={conversation.id}
                    isLastAssistant={false}
                  />
                  {text && (
                    <div className="flex justify-end gap-1 pt-1 opacity-60 transition-opacity md:opacity-0 md:group-hover/archive:opacity-100 md:group-focus-within/archive:opacity-100">
                      <Button
                        type="button"
                        variant="ghost"
                        size="xs"
                        aria-label="Copy message citation"
                        onClick={() =>
                          copy(
                            citation(text, conversation, link, target),
                            `citation:${bubbleKey(bubble)}`,
                          )
                        }
                      >
                        {copied === `citation:${bubbleKey(bubble)}` ? <CheckIcon /> : <CopyIcon />}
                        Citation
                      </Button>
                      <Button
                        type="button"
                        variant="ghost"
                        size="xs"
                        aria-label="Copy message link"
                        onClick={() => copy(link, `link:${bubbleKey(bubble)}`)}
                      >
                        {copied === `link:${bubbleKey(bubble)}` ? <CheckIcon /> : <LinkIcon />}
                        Link
                      </Button>
                    </div>
                  )}
                </article>
              );
            })}
          </div>
        )}
      </div>
    </section>
  );
}
