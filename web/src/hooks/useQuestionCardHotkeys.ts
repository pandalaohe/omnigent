// Keyboard focus for pending question cards.
//
// Every mounted card root registers itself here. The focus chord picks the
// card the user is looking at — focus inside it; else the nearest set to the
// reference node (cards tied at the deepest common ancestor), narrowed by
// last touched, then newest; with no usable reference every card is a
// candidate — and calls that card's enter(). Pressing again while focus is
// inside a card steps to the next card of that pane, newest first, wrapping.
// The reveal that follows keeps the entered card in view, releasing the
// conversation bottom-lock when it scrolls up. leaveQuestionCard() returns
// focus to the card's own composer textarea, or to wherever focus came from
// when the card has none (the Inbox).

import {
  ConversationScrollLockContext,
  type ConversationScrollLock,
} from "@/components/ai-elements/conversation";
import { useContext, useEffect, useRef, type RefObject } from "react";

import { eventMatchesShortcutAction } from "@/lib/keyboardShortcutPreferences";
import { focusOwnsHotkey } from "./useFocusComposerHotkey";

interface QuestionCardEntry {
  element: HTMLElement;
  enter: () => void;
  lock: () => ConversationScrollLock | null;
  mountedSeq: number;
  touchedSeq: number | null;
}

const QUESTION_CARD_SELECTOR = "[data-question-card]";
const COMPOSER_TEXTAREA_SELECTOR = "[data-composer-card] textarea";

const cardEntries: QuestionCardEntry[] = [];
let mountedCounter = 0;
let touchCounter = 0;
let lastPointerDownTarget: EventTarget | null = null;
let pointerDownTracked = false;
let preEntryElement: HTMLElement | null = null;

function onPointerDown(event: globalThis.PointerEvent): void {
  lastPointerDownTarget = event.target;
}

// Safari does not focus a clicked button, so proximity falls back to the last
// pointerdown target. The one listener lives only while a card is mounted.
function syncPointerTracking(): void {
  if (cardEntries.length > 0 && !pointerDownTracked) {
    document.addEventListener("pointerdown", onPointerDown, true);
    pointerDownTracked = true;
  } else if (cardEntries.length === 0 && pointerDownTracked) {
    document.removeEventListener("pointerdown", onPointerDown, true);
    pointerDownTracked = false;
    lastPointerDownTarget = null;
    preEntryElement = null;
  }
}

// Steps from the reference node up to its deepest common ancestor with the
// candidate. A small depth means a deep common ancestor, i.e. proximity.
function commonAncestorDepth(element: HTMLElement, reference: Node): number | null {
  const depths = new Map<Node, number>();
  let depth = 0;
  for (let node: Node | null = reference; node; node = node.parentNode) {
    depths.set(node, depth);
    depth += 1;
  }
  for (let node: Node | null = element; node; node = node.parentNode) {
    const referenceDepth = depths.get(node);
    if (referenceDepth !== undefined) return referenceDepth;
  }
  return null;
}

/** All items whose deepest common ancestor with the reference is deepest. */
function nearestSet<T>(items: T[], elementOf: (item: T) => HTMLElement, reference: Node): T[] {
  const result: T[] = [];
  let bestDepth = Number.POSITIVE_INFINITY;
  for (const item of items) {
    const depth = commonAncestorDepth(elementOf(item), reference);
    if (depth === null) continue;
    if (depth < bestDepth) {
      bestDepth = depth;
      result.length = 0;
      result.push(item);
    } else if (depth === bestDepth) {
      result.push(item);
    }
  }
  return result;
}

function highest(
  entries: QuestionCardEntry[],
  score: (entry: QuestionCardEntry) => number,
): QuestionCardEntry | null {
  let best: QuestionCardEntry | null = null;
  for (const entry of entries) {
    if (!best || score(entry) > score(best)) best = entry;
  }
  return best;
}

/**
 * The pending card the focus chord should enter: the one containing focus,
 * advanced to the next of its pane's cards (newest first, wrapping to the
 * newest); else among the cards nearest the reference node (all tied at the
 * deepest common ancestor) the last touched, else the newest; with no usable
 * reference every card is a candidate. A touched card in another pane is
 * never a candidate, so it cannot win over cards in the focused pane.
 */
export function targetQuestionCard(): QuestionCardEntry | null {
  if (cardEntries.length === 0) return null;
  const active = document.activeElement;
  const containing = cardEntries.find(
    (entry) => active instanceof Node && entry.element.contains(active),
  );
  if (containing) {
    // Pane scoping by proximity: a sibling card shares a deeper ancestor than
    // any card in another pane. One card (or none) leaves the card itself.
    const peers = [
      containing,
      ...nearestSet(
        cardEntries.filter((entry) => entry !== containing),
        (entry) => entry.element,
        containing.element,
      ),
    ].sort((a, b) => b.mountedSeq - a.mountedSeq);
    return peers[(peers.indexOf(containing) + 1) % peers.length];
  }

  // The clicked control can be gone by chord time; a disconnected node
  // positions nothing, so it is not a reference.
  const pointerReference =
    lastPointerDownTarget instanceof Node && lastPointerDownTarget.isConnected
      ? lastPointerDownTarget
      : null;
  const reference = active instanceof Node && active !== document.body ? active : pointerReference;
  const near = reference ? nearestSet(cardEntries, (entry) => entry.element, reference) : [];
  // A reference that shares no ancestor leaves no candidates; use every card.
  const candidates = near.length > 0 ? near : cardEntries;

  const touched = highest(
    candidates.filter((entry) => entry.touchedSeq !== null),
    (entry) => entry.touchedSeq ?? 0,
  );
  if (touched) return touched;
  return highest(candidates, (entry) => entry.mountedSeq);
}

/** First of the nearest set still returns one textarea — the card's own pane's. */
function nearestComposerTextarea(card: HTMLElement): HTMLElement | null {
  const textareas = Array.from(document.querySelectorAll<HTMLElement>(COMPOSER_TEXTAREA_SELECTOR));
  return nearestSet(textareas, (textarea) => textarea, card)[0] ?? null;
}

/**
 * Return focus to the card's own composer textarea; when the card has none
 * (the Inbox), restore the element focus came from; else blur.
 */
export function leaveQuestionCard(card: HTMLElement | null): void {
  if (card) {
    const composer = nearestComposerTextarea(card);
    if (composer) {
      composer.focus();
      return;
    }
  }
  if (preEntryElement?.isConnected && (!card || !card.contains(preEntryElement))) {
    preEntryElement.focus();
    preEntryElement = null;
    return;
  }
  if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
}

/** The one predicate other window listeners use to yield to a focused card. */
export function isInsideQuestionCard(target: EventTarget | null): boolean {
  return target instanceof Element && target.closest(QUESTION_CARD_SELECTOR) !== null;
}

const REVEAL_MARGIN_PX = 12;

/** The nearest ancestor that scrolls vertically, or null outside one. */
function scrollContainerFor(element: HTMLElement): HTMLElement | null {
  for (let node = element.parentElement; node; node = node.parentElement) {
    const overflowY = getComputedStyle(node).overflowY;
    if (overflowY === "auto" || overflowY === "scroll" || overflowY === "overlay") return node;
  }
  return null;
}

/**
 * Put the entered card in view. Scrolling up first releases the conversation
 * bottom-lock, or the next content resize scrolls the transcript back down.
 */
function revealQuestionCard(entry: QuestionCardEntry): void {
  const { element } = entry;
  const container = scrollContainerFor(element);
  if (!container) {
    element.scrollIntoView?.({ block: "nearest" });
    return;
  }
  const view = container.getBoundingClientRect();
  const card = element.getBoundingClientRect();
  if (card.top >= view.top && card.bottom <= view.bottom) return;
  const delta =
    card.height > view.height - 2 * REVEAL_MARGIN_PX || card.top < view.top
      ? card.top - view.top - REVEAL_MARGIN_PX
      : card.bottom - view.bottom + REVEAL_MARGIN_PX;
  if (delta < 0) {
    const lock = entry.lock();
    if (lock) {
      lock.stopScroll();
      lock.state.isAtBottom = false;
      lock.state.escapedFromLock = true;
    }
  }
  container.scrollTop += delta;
}

/**
 * Register a mounted, pending card root. `enter` focuses the card and seeds
 * its highlight; it is held in a ref so the latest closure runs. The card's
 * conversation bottom-lock (null outside a conversation) is tracked the same
 * way, so the reveal reads the latest lock at chord time.
 */
export function useQuestionCardTarget(ref: RefObject<HTMLElement | null>, enter: () => void): void {
  const enterRef = useRef(enter);
  enterRef.current = enter;
  const lockRef = useRef<ConversationScrollLock | null>(null);
  lockRef.current = useContext(ConversationScrollLockContext);

  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    mountedCounter += 1;
    const entry: QuestionCardEntry = {
      element,
      enter: () => enterRef.current(),
      lock: () => lockRef.current,
      mountedSeq: mountedCounter,
      touchedSeq: null,
    };
    cardEntries.push(entry);
    syncPointerTracking();
    // A remount is a new entry: it loses its touchedSeq and counts as newest.
    const touch = () => {
      touchCounter += 1;
      entry.touchedSeq = touchCounter;
    };
    element.addEventListener("focusin", touch);
    element.addEventListener("pointerdown", touch);
    return () => {
      element.removeEventListener("focusin", touch);
      element.removeEventListener("pointerdown", touch);
      const index = cardEntries.indexOf(entry);
      if (index >= 0) cardEntries.splice(index, 1);
      syncPointerTracking();
    };
  }, [ref]);
}

/**
 * Bind the focus chord once in the app shell: remember where focus was, hand
 * it to the chosen card, then reveal the card. No card means the event is
 * left untouched.
 */
export function useFocusQuestionCardHotkey(): void {
  useEffect(() => {
    const handler = (e: globalThis.KeyboardEvent): void => {
      // Auto-repeat would re-fire focus pointlessly.
      if (e.repeat) return;
      // AltGr reports as Ctrl+Alt on some layouts; guard explicitly.
      if (typeof e.getModifierState === "function" && e.getModifierState("AltGraph")) return;
      if (!eventMatchesShortcutAction(e, "focusQuestionCard")) return;
      if (focusOwnsHotkey()) return;
      const target = targetQuestionCard();
      if (!target) return;
      const active = document.activeElement;
      if (
        active instanceof HTMLElement &&
        active !== document.body &&
        !target.element.contains(active)
      ) {
        preEntryElement = active;
      }
      e.preventDefault();
      e.stopPropagation();
      target.enter();
      revealQuestionCard(target);
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);
}
