import { useContext, useLayoutEffect, type RefObject } from "react";
import { ConversationScrollLockContext } from "@/components/ai-elements/conversation";
import { createTextFieldMirror } from "@/lib/textFieldMirror";

export function useQuestionCardScroll(
  rootRef: RefObject<HTMLDivElement | null>,
  held: boolean,
  field: HTMLTextAreaElement | null,
) {
  const hold = useContext(ConversationScrollLockContext)?.holdQuestionCard;
  useLayoutEffect(() => {
    const root = rootRef.current;
    if (!held || !root?.closest(".session-chat-transcript")) return undefined;
    return hold?.(root);
  }, [held, hold, rootRef]);

  useLayoutEffect(() => {
    const scroller = field?.closest<HTMLElement>("[data-question-card-scroll-held]");
    if (!held || !field || !scroller) return;
    const composer = scroller
      .closest("[data-chat-surface]")
      ?.querySelector<HTMLElement>(".chat-composer-form");
    const viewport = window.visualViewport;
    let frame = 0;
    let keyboardRoom = 0;
    // Reuse the attached mirror throughout focus. Attaching one on every input
    // forces document-wide style invalidation in WebKit (see upstream #14492).
    const host = document.createElement("div");
    host.setAttribute("aria-hidden", "true");
    host.style.cssText =
      "position:fixed;inset:0 auto auto 0;width:0;height:0;overflow:hidden;contain:strict";
    const prefix = document.createTextNode("");
    const tail = document.createElement("span");
    let mirror: HTMLDivElement | undefined;

    const reveal = () => {
      frame = 0;
      if (document.activeElement !== field) return;
      const scrollRect = scroller.getBoundingClientRect();
      const composerTop = composer?.getBoundingClientRect().top ?? scrollRect.bottom;
      const layoutBottom = Math.min(scrollRect.bottom, composerTop);
      const viewportTop = viewport?.offsetTop ?? 0;
      const viewportBottom = viewport ? viewportTop + viewport.height : window.innerHeight;
      const room = Math.ceil(Math.max(0, layoutBottom - viewportBottom));
      if (room !== keyboardRoom) {
        keyboardRoom = room;
        scroller.style.setProperty("--question-card-keyboard-room", `${room}px`);
      }
      const top = Math.max(scrollRect.top, viewportTop) + 8;
      // Desktop fades the last 32px of the transcript. On a phone the fade
      // starts 8px above the composer, whose stack overlays the scroller.
      const fade = composerTop < scrollRect.bottom ? 8 : 32;
      const bottom = Math.min(layoutBottom - fade, viewportBottom - 8);
      if (bottom <= top) return;
      const fieldRect = field.getBoundingClientRect();
      if (fieldRect.top >= top && fieldRect.bottom <= bottom) return;
      if (fieldRect.width === 0) return;

      const style = getComputedStyle(field);
      const nextMirror = createTextFieldMirror(field, style);
      if (mirror?.style.cssText !== nextMirror.style.cssText) {
        nextMirror.append(prefix, tail);
        host.replaceChildren(nextMirror);
        mirror = nextMirror;
        if (!host.isConnected) document.body.append(host);
      }
      if (!mirror) return;
      const offset =
        field.selectionDirection === "backward" ? field.selectionStart : field.selectionEnd;
      prefix.data = field.value.slice(0, offset);
      tail.textContent = field.value.slice(offset) || " ";
      const glyph = tail.getClientRects()[0];
      if (!glyph) return;
      const lineHeight = parseFloat(style.lineHeight) || glyph.height;
      if (lineHeight <= 0) return;
      const line = Math.floor(
        (glyph.top + glyph.height / 2 - mirror.getBoundingClientRect().top) / lineHeight,
      );
      const lineTop =
        fieldRect.top +
        field.clientTop +
        (parseFloat(style.paddingTop) || 0) +
        line * lineHeight -
        field.scrollTop;
      const delta = Math.max(lineTop + lineHeight - bottom, 0) + Math.min(lineTop - top, 0);
      if (Math.abs(delta) >= 1) scroller.scrollTop += delta;
    };
    const schedule = () => {
      if (!frame) frame = requestAnimationFrame(reveal);
    };
    field.addEventListener("input", schedule);
    field.addEventListener("keyup", schedule);
    field.addEventListener("select", schedule);
    document.addEventListener("selectionchange", schedule);
    viewport?.addEventListener("resize", schedule);
    viewport?.addEventListener("scroll", schedule);
    const observer = new ResizeObserver(schedule);
    observer.observe(field);
    observer.observe(scroller);
    if (composer) observer.observe(composer);
    schedule();
    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
      field.removeEventListener("input", schedule);
      field.removeEventListener("keyup", schedule);
      field.removeEventListener("select", schedule);
      document.removeEventListener("selectionchange", schedule);
      viewport?.removeEventListener("resize", schedule);
      viewport?.removeEventListener("scroll", schedule);
      host.remove();
      scroller.style.removeProperty("--question-card-keyboard-room");
    };
  }, [field, held]);
}
