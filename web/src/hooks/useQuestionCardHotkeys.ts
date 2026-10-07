// Keyboard focus for pending question cards.
//
// Every mounted card root registers itself here. The focus chord picks the
// card the user is looking at — focus inside it, else the card nearest the
// reference node in the DOM, else the last touched, else the newest — and
// calls that card's enter(). leaveQuestionCard() returns focus to the card's
// own composer textarea, or to wherever focus came from when the card has
// none (the Inbox).

import { useEffect, useRef, type RefObject } from "react";

import { eventMatchesShortcutAction } from "@/lib/keyboardShortcutPreferences";
import { focusOwnsHotkey } from "./useFocusComposerHotkey";

interface QuestionCardEntry {
  element: HTMLElement;
  enter: () => void;
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

function nearest<T>(items: T[], elementOf: (item: T) => HTMLElement, reference: Node): T | null {
  let best: T | null = null;
  let bestDepth = Number.POSITIVE_INFINITY;
  let tied = false;
  for (const item of items) {
    const depth = commonAncestorDepth(elementOf(item), reference);
    if (depth === null) continue;
    if (depth < bestDepth) {
      best = item;
      bestDepth = depth;
      tied = false;
    } else if (depth === bestDepth) {
      tied = true;
    }
  }
  return tied ? null : best;
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
 * else the unique nearest to the reference node, else the last touched, else
 * the newest. Two candidates equally near a reference tie, so a card arriving
 * mid-answer in the same pane loses to the one being touched.
 */
export function targetQuestionCard(): { element: HTMLElement; enter: () => void } | null {
  if (cardEntries.length === 0) return null;
  const active = document.activeElement;
  const containing = cardEntries.find(
    (entry) => active instanceof Node && entry.element.contains(active),
  );
  if (containing) return containing;

  const reference =
    active instanceof Node && active !== document.body
      ? active
      : lastPointerDownTarget instanceof Node
        ? lastPointerDownTarget
        : null;
  if (reference) {
    const nearestCard = nearest(cardEntries, (entry) => entry.element, reference);
    if (nearestCard) return nearestCard;
  }

  const touched = highest(
    cardEntries.filter((entry) => entry.touchedSeq !== null),
    (entry) => entry.touchedSeq ?? 0,
  );
  if (touched) return touched;
  return highest(cardEntries, (entry) => entry.mountedSeq);
}

/** Nearest composer textarea to the card — its own pane's, never another's. */
function nearestComposerTextarea(card: HTMLElement): HTMLElement | null {
  const textareas = Array.from(document.querySelectorAll<HTMLElement>(COMPOSER_TEXTAREA_SELECTOR));
  return nearest(textareas, (textarea) => textarea, card);
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

/**
 * Register a mounted, pending card root. `enter` focuses the card and seeds
 * its highlight; it is held in a ref so the latest closure runs.
 */
export function useQuestionCardTarget(ref: RefObject<HTMLElement | null>, enter: () => void): void {
  const enterRef = useRef(enter);
  enterRef.current = enter;

  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    mountedCounter += 1;
    const entry: QuestionCardEntry = {
      element,
      enter: () => enterRef.current(),
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
 * Bind the focus chord once in the app shell: remember where focus was, then
 * hand it to the chosen card. No card means the event is left untouched.
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
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);
}
