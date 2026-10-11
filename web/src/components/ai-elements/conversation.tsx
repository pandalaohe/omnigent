"use client";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { UIMessage } from "ai";
import { ArrowDownIcon, DownloadIcon } from "lucide-react";
import type { ComponentProps, ReactNode } from "react";
import { createContext, useCallback, useLayoutEffect, useMemo } from "react";
import { StickToBottom, useStickToBottomContext } from "use-stick-to-bottom";

export type ConversationProps = ComponentProps<typeof StickToBottom>;

/** StickToBottom's bottom-lock controls, for descendants that reposition the scroll. */
export interface ConversationScrollLock {
  stopScroll: () => void;
  state: { isAtBottom: boolean; escapedFromLock: boolean };
}

export const ConversationScrollLockContext = createContext<ConversationScrollLock | null>(null);
export const CONVERSATION_RESUME_SCROLL_EVENT = "conversation-resume-scroll";

// Module handle so non-React scroll helpers (deep links, activity rail,
// Cmd+Alt nav) can release the bottom-lock without Conversation context.
let activeScrollLock: ConversationScrollLock | null = null;

/**
 * Release StickToBottom's bottom-lock so a programmatic scroll is not
 * overridden by the next content-resize ``scrollToBottom``. Same recipe as
 * JumpToTopButton / the "Worked for" fold snap. No-op outside a conversation.
 */
export function releaseConversationScrollLock(): void {
  if (!activeScrollLock) return;
  activeScrollLock.stopScroll();
  activeScrollLock.state.isAtBottom = false;
  activeScrollLock.state.escapedFromLock = true;
}

/**
 * Exposes the bottom-lock controls to deep descendants without the
 * library's throwing hook: a component that repositions the scroll (the
 * "Worked for" fold's snap-to-top) must release the lock first or the
 * resize-driven scrollToBottom yanks its scroll back to the bottom, and
 * it must no-op when rendered outside a conversation (tests, previews).
 * `stopScroll`/`state` exist at runtime but not in the library's public
 * types — same cast as ChatPage's ConversationScrollRefBridge.
 */
const ScrollLockBridge = ({ children }: { children: ReactNode }) => {
  const ctx = useStickToBottomContext() as ReturnType<typeof useStickToBottomContext> &
    ConversationScrollLock;
  const value = useMemo(
    () => ({ stopScroll: ctx.stopScroll, state: ctx.state }),
    [ctx.stopScroll, ctx.state],
  );
  // Layout so the module handle is live before sibling/parent useEffects
  // (message deep links) try to scroll away from the bottom.
  useLayoutEffect(() => {
    activeScrollLock = value;
    return () => {
      if (activeScrollLock === value) activeScrollLock = null;
    };
  }, [value]);
  useLayoutEffect(() => {
    const el = ctx.scrollRef.current;
    const content = ctx.contentRef.current;
    if (!el || !content) return;
    let touch: { x: number; y: number } | null = null;
    let pressed: { el: HTMLElement; top: number } | null = null;
    let releaseFrame = 0;
    let scrollTimer: ReturnType<typeof setTimeout> | undefined;
    let released = false;
    const release = () => {
      released = true;
      ctx.stopScroll();
    };
    // A nested scrollable control owns its gesture (code, terminals, editors).
    const ownsScroll = (target: EventTarget | null) => {
      for (
        let node = target instanceof Element ? target : null;
        node && node !== el;
        node = node.parentElement
      ) {
        if (
          node.scrollHeight > node.clientHeight &&
          /auto|scroll/.test(getComputedStyle(node).overflowY)
        )
          return false;
      }
      return true;
    };
    const onTouchStart = (event: TouchEvent) => {
      const point = event.touches.length === 1 ? event.touches[0] : undefined;
      touch = point && ownsScroll(event.target) ? { x: point.clientX, y: point.clientY } : null;
    };
    const onTouchMove = (event: TouchEvent) => {
      const point = event.touches.length === 1 ? event.touches[0] : undefined;
      if (!touch || !point) return;
      const dy = point.clientY - touch.y;
      if (dy > 6 && dy > Math.abs(point.clientX - touch.x)) {
        pressed = null;
        release();
      }
    };
    const onTouchEnd = () => {
      touch = null;
    };
    const onPointerDown = (event: PointerEvent) => {
      if (event.pointerType !== "touch") return;
      const target =
        event.target instanceof Element
          ? event.target.closest<HTMLElement>(
              "button, input, textarea, select, a, [role='button'], label",
            )
          : null;
      if (!target || !el.contains(target) || !ownsScroll(target)) return;
      cancelAnimationFrame(releaseFrame);
      ctx.stopScroll();
      pressed = { el: target, top: target.getBoundingClientRect().top };
    };
    // Keep the touched control under the finger, even when rows grow above it.
    const observer = new ResizeObserver(() => {
      if (released || pressed) ctx.stopScroll();
      if (!pressed?.el.isConnected) return;
      const delta = pressed.el.getBoundingClientRect().top - pressed.top;
      if (Math.abs(delta) > 1) el.scrollTop += delta;
    });
    observer.observe(content);
    const onPointerEnd = (event: PointerEvent) => {
      if (!pressed) return;
      if (event.type === "pointercancel") {
        pressed = null;
        return;
      }
      // The compatibility click is dispatched after pointerup.
      releaseFrame = requestAnimationFrame(() => {
        pressed = null;
        if (el.scrollHeight - el.clientHeight - el.scrollTop <= 1) {
          ctx.state.escapedFromLock = false;
          void ctx.scrollToBottom("instant");
        }
      });
    };
    // The library resumes within 70px of the bottom. A reader's escape lasts
    // until they actually reach it, including after a resize swallowed a scroll.
    const onScroll = () => {
      clearTimeout(scrollTimer);
      scrollTimer = setTimeout(() => {
        if (pressed) ctx.stopScroll();
        else if (released) {
          if (el.scrollHeight - el.clientHeight - el.scrollTop <= 1) {
            released = false;
            ctx.state.escapedFromLock = false;
            void ctx.scrollToBottom("instant");
          } else ctx.stopScroll();
        }
      }, 1);
    };
    const onWheel = (event: WheelEvent) => {
      if (event.deltaY < 0 && ownsScroll(event.target)) release();
    };
    const onResume = () => {
      released = false;
      ctx.state.escapedFromLock = false;
    };
    el.addEventListener(CONVERSATION_RESUME_SCROLL_EVENT, onResume);
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target;
      if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey) return;
      if ((event.shiftKey && event.key !== " ") || !ownsScroll(target)) return;
      if (
        target instanceof HTMLElement &&
        (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName))
      )
        return;
      if (
        ["ArrowUp", "PageUp", "Home"].includes(event.key) ||
        (event.key === " " && event.shiftKey)
      )
        release();
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    el.addEventListener("wheel", onWheel, { passive: true });
    el.addEventListener("keydown", onKeyDown);
    el.addEventListener("touchstart", onTouchStart, { passive: true });
    el.addEventListener("touchmove", onTouchMove, { passive: true });
    el.addEventListener("touchend", onTouchEnd, { passive: true });
    el.addEventListener("touchcancel", onTouchEnd, { passive: true });
    el.addEventListener("pointerdown", onPointerDown, { passive: true });
    el.addEventListener("pointerup", onPointerEnd, { passive: true });
    el.addEventListener("pointercancel", onPointerEnd, { passive: true });
    return () => {
      observer.disconnect();
      cancelAnimationFrame(releaseFrame);
      clearTimeout(scrollTimer);
      el.removeEventListener("scroll", onScroll);
      el.removeEventListener(CONVERSATION_RESUME_SCROLL_EVENT, onResume);
      el.removeEventListener("wheel", onWheel);
      el.removeEventListener("keydown", onKeyDown);
      el.removeEventListener("touchstart", onTouchStart);
      el.removeEventListener("touchmove", onTouchMove);
      el.removeEventListener("touchend", onTouchEnd);
      el.removeEventListener("touchcancel", onTouchEnd);
      el.removeEventListener("pointerdown", onPointerDown);
      el.removeEventListener("pointerup", onPointerEnd);
      el.removeEventListener("pointercancel", onPointerEnd);
    };
  }, [ctx.scrollRef, ctx.contentRef, ctx.stopScroll, ctx.scrollToBottom, ctx.state]);
  return (
    <ConversationScrollLockContext.Provider value={value}>
      {children}
    </ConversationScrollLockContext.Provider>
  );
};

export const Conversation = ({ className, children, ...props }: ConversationProps) => (
  <StickToBottom
    className={cn("relative flex-1 overflow-y-hidden", className)}
    initial="instant"
    resize="instant"
    role="log"
    {...props}
  >
    {(context) => (
      <ScrollLockBridge>
        {typeof children === "function" ? children(context) : children}
      </ScrollLockBridge>
    )}
  </StickToBottom>
);

export type ConversationContentProps = ComponentProps<typeof StickToBottom.Content>;

export const ConversationContent = ({ className, ...props }: ConversationContentProps) => (
  <StickToBottom.Content className={cn("flex flex-col gap-8 px-6 py-4", className)} {...props} />
);

export type ConversationEmptyStateProps = ComponentProps<"div"> & {
  title?: string;
  description?: string;
  icon?: React.ReactNode;
};

export const ConversationEmptyState = ({
  className,
  title = "No messages yet",
  description = "Start a conversation to see messages here",
  icon,
  children,
  ...props
}: ConversationEmptyStateProps) => (
  <div
    className={cn(
      "flex size-full flex-col items-center justify-center gap-3 p-8 text-center",
      className,
    )}
    {...props}
  >
    {children ?? (
      <>
        {icon && <div className="text-muted-foreground">{icon}</div>}
        <div className="space-y-1.5">
          <h3 className="text-2xl font-medium tracking-[-0.02em]">{title}</h3>
          {description && <p className="text-muted-foreground text-ui">{description}</p>}
        </div>
      </>
    )}
  </div>
);

export type ConversationScrollButtonProps = ComponentProps<typeof Button>;

export const ConversationScrollButton = ({
  className,
  ...props
}: ConversationScrollButtonProps) => {
  const { isAtBottom, scrollToBottom, scrollRef } = useStickToBottomContext();

  const handleScrollToBottom = useCallback(() => {
    scrollRef.current?.dispatchEvent(new Event(CONVERSATION_RESUME_SCROLL_EVENT));
    scrollToBottom("instant");
  }, [scrollRef, scrollToBottom]);

  return (
    !isAtBottom && (
      <Button
        className={cn(
          "absolute bottom-4 left-[50%] translate-x-[-50%] rounded-full",
          // Keep the fill OPAQUE on hover. The outline variant's hover (bg-muted)
          // is a translucent black wash (--muted is #0000000f), so over the chat
          // content behind it the button reads as transparent on hover. Hover
          // feedback comes from a brightness filter instead, which stays opaque.
          "bg-background hover:bg-background hover:brightness-95",
          "dark:bg-background dark:hover:bg-background dark:hover:brightness-125",
          className,
        )}
        onClick={handleScrollToBottom}
        size="icon-sm"
        type="button"
        variant="outline"
        {...props}
      >
        <ArrowDownIcon className="size-4" />
      </Button>
    )
  );
};

const getMessageText = (message: UIMessage): string =>
  message.parts
    .filter((part) => part.type === "text")
    .map((part) => part.text)
    .join("");

export type ConversationDownloadProps = Omit<ComponentProps<typeof Button>, "onClick"> & {
  messages: UIMessage[];
  filename?: string;
  formatMessage?: (message: UIMessage, index: number) => string;
};

const defaultFormatMessage = (message: UIMessage): string => {
  const roleLabel = message.role.charAt(0).toUpperCase() + message.role.slice(1);
  return `**${roleLabel}:** ${getMessageText(message)}`;
};

export const messagesToMarkdown = (
  messages: UIMessage[],
  formatMessage: (message: UIMessage, index: number) => string = defaultFormatMessage,
): string => messages.map((msg, i) => formatMessage(msg, i)).join("\n\n");

export const ConversationDownload = ({
  messages,
  filename = "conversation.md",
  formatMessage = defaultFormatMessage,
  className,
  children,
  ...props
}: ConversationDownloadProps) => {
  const handleDownload = useCallback(() => {
    const markdown = messagesToMarkdown(messages, formatMessage);
    const blob = new Blob([markdown], { type: "text/markdown" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  }, [messages, filename, formatMessage]);

  return (
    <Button
      className={cn(
        "absolute top-4 right-4 rounded-full dark:bg-background dark:hover:bg-muted",
        className,
      )}
      onClick={handleDownload}
      size="icon"
      type="button"
      variant="outline"
      {...props}
    >
      {children ?? <DownloadIcon className="size-4" />}
    </Button>
  );
};
