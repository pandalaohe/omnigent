"use client";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { UIMessage } from "ai";
import { ArrowDownIcon, DownloadIcon } from "lucide-react";
import type { ComponentProps, ReactNode } from "react";
import { createContext, useCallback, useLayoutEffect, useMemo, useRef } from "react";
import { StickToBottom, useStickToBottomContext } from "use-stick-to-bottom";

export type ConversationProps = ComponentProps<typeof StickToBottom>;

/** StickToBottom's bottom-lock controls, for descendants that reposition the scroll. */
export interface ConversationScrollLock {
  stopScroll: () => void;
  state: { isAtBottom: boolean; escapedFromLock: boolean };
}

const QUESTION_CARD_HOLD = "data-question-card-scroll-held";

export function isQuestionCardScrollHeld(scroller: HTMLElement): boolean {
  return scroller.hasAttribute(QUESTION_CARD_HOLD);
}

export const ConversationScrollLockContext = createContext<
  (ConversationScrollLock & { holdQuestionCard?: (card: HTMLElement) => () => void }) | null
>(null);
export const CONVERSATION_RESUME_SCROLL_EVENT = "conversation-resume-scroll";
export const QUESTION_CARD_SCROLL_CHANGE_EVENT = "question-card-scroll-change";
const PAUSE_QUESTION_CARD_SCROLL_EVENT = "conversation-pause-question-card-scroll";

export function pauseQuestionCardScroll(scroller: HTMLElement): void {
  scroller.dispatchEvent(new Event(PAUSE_QUESTION_CARD_SCROLL_EVENT));
}

// Module handle so non-React scroll helpers (deep links, activity rail,
// Cmd+Alt nav) can release the bottom-lock without Conversation context.
let activeScrollLock: (ConversationScrollLock & { pauseQuestionCard: () => void }) | null = null;

/**
 * Release StickToBottom's bottom-lock so a programmatic scroll is not
 * overridden by the next content-resize ``scrollToBottom``. Same recipe as
 * JumpToTopButton / the "Worked for" fold snap. No-op outside a conversation.
 */
export function releaseConversationScrollLock(): void {
  if (!activeScrollLock) return;
  activeScrollLock.pauseQuestionCard();
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
  const { scrollRef, contentRef, stopScroll, scrollToBottom, state } = ctx;
  const cards = useRef(new Set<HTMLElement>());
  const anchor = useRef<{ card: HTMLElement; offset: number; scrollTop: number } | null>(null);
  const resumeFollow = useRef(false);
  const resumeFrame = useRef(0);
  const keepCardInPlace = useCallback(() => {
    const el = scrollRef.current;
    if (!el || !isQuestionCardScrollHeld(el) || cards.current.size === 0) return false;
    stopScroll();
    const held = [...cards.current];
    const card = held.find((node) => node.contains(document.activeElement)) ?? held.at(-1)!;
    const offset = card.getBoundingClientRect().top - el.getBoundingClientRect().top + el.scrollTop;
    const previous = anchor.current;
    if (previous?.card === card) {
      // At the bottom, shrinking content can move scrollTop before the resize
      // callback. Only compensate the layout movement the clamp didn't absorb.
      const clamp = Math.min(previous.scrollTop, el.scrollHeight - el.clientHeight);
      const absorbed = Math.abs(el.scrollTop - clamp) <= 1 ? clamp - previous.scrollTop : 0;
      const delta = offset - previous.offset - absorbed;
      if (Math.abs(delta) >= 1) state.scrollTop += delta;
    }
    anchor.current = { card, offset, scrollTop: el.scrollTop };
    return true;
  }, [scrollRef, state, stopScroll]);
  const startQuestionCardAnchor = useCallback((el: HTMLElement, content: HTMLElement) => {
    // Keep the starting spacer height available while stream output fills it.
    const spacer = content.querySelector("[data-latest-turn-spacer]");
    if (spacer) {
      el.style.setProperty(
        "--question-card-spacer-floor",
        `${spacer.getBoundingClientRect().height}px`,
      );
    }
    el.setAttribute(QUESTION_CARD_HOLD, "");
    el.dispatchEvent(new Event(QUESTION_CARD_SCROLL_CHANGE_EVENT));
  }, []);
  const clearQuestionCardAnchor = useCallback((el: HTMLElement) => {
    anchor.current = null;
    if (!isQuestionCardScrollHeld(el)) return;
    el.removeAttribute(QUESTION_CARD_HOLD);
    el.style.removeProperty("--question-card-spacer-floor");
    el.dispatchEvent(new Event(QUESTION_CARD_SCROLL_CHANGE_EVENT));
  }, []);
  const pauseQuestionCard = useCallback(() => {
    const el = scrollRef.current;
    if (!el || (!isQuestionCardScrollHeld(el) && !resumeFrame.current)) return false;
    cancelAnimationFrame(resumeFrame.current);
    resumeFrame.current = 0;
    resumeFollow.current = false;
    clearQuestionCardAnchor(el);
    return true;
  }, [clearQuestionCardAnchor, scrollRef]);
  const holdQuestionCard = useCallback(
    (card: HTMLElement) => {
      // A restored draft can mount before the ancestor's refs are attached.
      const content = contentRef.current ?? card.closest(".chat-conversation-content");
      const el = scrollRef.current ?? content?.parentElement;
      if (!el || !content?.contains(card)) return () => {};
      const transcript = el.parentElement;
      const session = transcript?.dataset.transcriptSession;
      if (cards.current.size === 0) {
        resumeFollow.current =
          resumeFrame.current !== 0 ||
          (state.isAtBottom && !state.escapedFromLock) ||
          el.scrollHeight - el.clientHeight - el.scrollTop <= 1;
        cancelAnimationFrame(resumeFrame.current);
        resumeFrame.current = 0;
        startQuestionCardAnchor(el, content);
      }
      cards.current.add(card);
      if (!keepCardInPlace() && isQuestionCardScrollHeld(el)) stopScroll();
      // The virtual list can grow while its trailing spacer shrinks by the
      // same amount, so content height alone cannot signal a shifted card.
      const observer = new ResizeObserver(keepCardInPlace);
      observer.observe(el);
      for (const child of content.children) observer.observe(child);
      return () => {
        observer.disconnect();
        cards.current.delete(card);
        if (cards.current.size > 0) return;
        clearQuestionCardAnchor(el);
        if (!resumeFollow.current) return;
        // Let a submitted card's replacement render before restoring follow.
        resumeFrame.current = requestAnimationFrame(() => {
          resumeFrame.current = 0;
          if (scrollRef.current !== el || transcript?.dataset.transcriptSession !== session) return;
          el.dispatchEvent(new Event(CONVERSATION_RESUME_SCROLL_EVENT));
          void scrollToBottom("instant");
        });
      };
    },
    [
      clearQuestionCardAnchor,
      contentRef,
      keepCardInPlace,
      scrollRef,
      scrollToBottom,
      startQuestionCardAnchor,
      state,
      stopScroll,
    ],
  );
  const value = useMemo(
    () => ({ stopScroll, state, holdQuestionCard }),
    [stopScroll, state, holdQuestionCard],
  );
  // Layout so the module handle is live before sibling/parent useEffects
  // (message deep links) try to scroll away from the bottom.
  useLayoutEffect(() => {
    const lock = {
      stopScroll,
      state,
      pauseQuestionCard: () => {
        const el = scrollRef.current;
        if (el) pauseQuestionCardScroll(el);
      },
    };
    activeScrollLock = lock;
    return () => {
      if (activeScrollLock === lock) activeScrollLock = null;
    };
  }, [scrollRef, state, stopScroll]);
  useLayoutEffect(() => {
    const el = ctx.scrollRef.current;
    const content = ctx.contentRef.current;
    if (!el || !content) return;
    let touch: { x: number; y: number } | null = null;
    let pressed: { el: HTMLElement; top: number } | null = null;
    let releaseFrame = 0;
    let caretFrame = 0;
    let scrollTimer: ReturnType<typeof setTimeout> | undefined;
    let released = false;
    const cancelCaretAnchor = () => {
      cancelAnimationFrame(caretFrame);
      caretFrame = 0;
    };
    const release = () => {
      cancelCaretAnchor();
      released = true;
      pauseQuestionCard();
      ctx.stopScroll();
    };
    const onPauseCardScroll = () => {
      cancelCaretAnchor();
      if (!pauseQuestionCard()) return;
      released = true;
      ctx.stopScroll();
    };
    const onCardEdit = (event: Event) => {
      const held = isQuestionCardScrollHeld(el);
      if (held && event.type !== "focusin") return;
      if (!(event.target instanceof HTMLElement)) return;
      const target = event.target;
      if (![...cards.current].some((card) => card.contains(target))) return;
      if (!held) startQuestionCardAnchor(el, content);
      keepCardInPlace();
    };
    const onCardCaretIntent = (event: Event) => {
      const target = event.target;
      if (
        isQuestionCardScrollHeld(el) ||
        !(target instanceof HTMLTextAreaElement) ||
        target !== document.activeElement
      )
        return;
      cancelCaretAnchor();
      // React controls and window shortcuts can consume the event after this listener.
      caretFrame = requestAnimationFrame(() => {
        caretFrame = 0;
        if (!event.defaultPrevented && target === document.activeElement) onCardEdit(event);
      });
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
      const distance = isQuestionCardScrollHeld(el) ? Math.abs(dy) : dy;
      if (distance > 6 && distance > Math.abs(point.clientX - touch.x)) {
        pressed = null;
        release();
      }
    };
    const onTouchEnd = () => {
      touch = null;
    };
    const onPointerDown = (event: PointerEvent) => {
      if (event.pointerType !== "touch" || isQuestionCardScrollHeld(el)) return;
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
      if (keepCardInPlace()) return;
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
        if (
          !isQuestionCardScrollHeld(el) &&
          el.scrollHeight - el.clientHeight - el.scrollTop <= 1
        ) {
          ctx.state.escapedFromLock = false;
          void ctx.scrollToBottom("instant");
        }
      });
    };
    // The library resumes within 70px of the bottom. A reader's escape lasts
    // until they actually reach it, including after a resize swallowed a scroll.
    const onScroll = () => {
      const previous = anchor.current;
      if (previous && el.scrollTop !== previous.scrollTop) {
        const cap = el.scrollHeight - el.clientHeight;
        const clamped = previous.scrollTop > cap && Math.abs(el.scrollTop - cap) <= 1;
        // Caret reveal can move the viewport on its own. A clamp is handled
        // by the resize observer using the previous scrollTop.
        if (!clamped) previous.scrollTop = el.scrollTop;
      }
      clearTimeout(scrollTimer);
      scrollTimer = setTimeout(() => {
        if (isQuestionCardScrollHeld(el) || pressed) ctx.stopScroll();
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
      if (
        (event.deltaY < 0 ||
          (event.deltaY > 0 && (isQuestionCardScrollHeld(el) || caretFrame !== 0))) &&
        ownsScroll(event.target)
      )
        release();
    };
    const onResume = () => {
      released = false;
      ctx.state.escapedFromLock = false;
    };
    el.addEventListener(CONVERSATION_RESUME_SCROLL_EVENT, onResume);
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target;
      if (!(target instanceof HTMLElement) || !el.contains(target)) return;
      if (event.defaultPrevented) return;
      if (target instanceof HTMLTextAreaElement) {
        if (
          !event.isComposing &&
          ["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)
        )
          onCardCaretIntent(event);
        return;
      }
      if (event.altKey || event.ctrlKey || event.metaKey) return;
      if ((event.shiftKey && event.key !== " ") || !ownsScroll(target)) return;
      if (target.isContentEditable || /^(INPUT|SELECT)$/.test(target.tagName)) return;
      if (event.key === " " && target.closest("button")) return;
      if (
        ["ArrowUp", "PageUp", "Home"].includes(event.key) ||
        (event.key === " " && event.shiftKey) ||
        (isQuestionCardScrollHeld(el) && ["ArrowDown", "PageDown", "End", " "].includes(event.key))
      )
        release();
    };
    el.addEventListener(PAUSE_QUESTION_CARD_SCROLL_EVENT, onPauseCardScroll);
    el.addEventListener("focusin", onCardEdit);
    el.addEventListener("input", onCardEdit);
    el.addEventListener("click", onCardCaretIntent);
    el.addEventListener("scroll", onScroll, { passive: true });
    el.addEventListener("wheel", onWheel, { passive: true });
    // Let React controls consume their shortcuts before treating the key as scroll intent.
    document.addEventListener("keydown", onKeyDown);
    el.addEventListener("touchstart", onTouchStart, { passive: true });
    el.addEventListener("touchmove", onTouchMove, { passive: true });
    el.addEventListener("touchend", onTouchEnd, { passive: true });
    el.addEventListener("touchcancel", onTouchEnd, { passive: true });
    el.addEventListener("pointerdown", onPointerDown, { passive: true });
    el.addEventListener("pointerup", onPointerEnd, { passive: true });
    el.addEventListener("pointercancel", onPointerEnd, { passive: true });
    return () => {
      observer.disconnect();
      cancelAnimationFrame(resumeFrame.current);
      resumeFrame.current = 0;
      cancelAnimationFrame(releaseFrame);
      cancelCaretAnchor();
      clearTimeout(scrollTimer);
      el.removeEventListener(PAUSE_QUESTION_CARD_SCROLL_EVENT, onPauseCardScroll);
      el.removeEventListener("focusin", onCardEdit);
      el.removeEventListener("input", onCardEdit);
      el.removeEventListener("click", onCardCaretIntent);
      el.removeEventListener("scroll", onScroll);
      el.removeEventListener(CONVERSATION_RESUME_SCROLL_EVENT, onResume);
      el.removeEventListener("wheel", onWheel);
      document.removeEventListener("keydown", onKeyDown);
      el.removeEventListener("touchstart", onTouchStart);
      el.removeEventListener("touchmove", onTouchMove);
      el.removeEventListener("touchend", onTouchEnd);
      el.removeEventListener("touchcancel", onTouchEnd);
      el.removeEventListener("pointerdown", onPointerDown);
      el.removeEventListener("pointerup", onPointerEnd);
      el.removeEventListener("pointercancel", onPointerEnd);
    };
  }, [
    ctx.scrollRef,
    ctx.contentRef,
    ctx.stopScroll,
    ctx.scrollToBottom,
    ctx.state,
    keepCardInPlace,
    pauseQuestionCard,
    startQuestionCardAnchor,
  ]);
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
    const el = scrollRef.current;
    if (el) {
      pauseQuestionCardScroll(el);
      el.dispatchEvent(new Event(CONVERSATION_RESUME_SCROLL_EVENT));
    }
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
