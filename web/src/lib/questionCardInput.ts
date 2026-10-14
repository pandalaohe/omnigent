/** A selected free-text answer in a pending async question card. */
export interface CardInputTarget {
  sessionId: string;
  cardId: string;
  questionKey: string;
  element: HTMLTextAreaElement;
  insert: (text: string, files: File[], commentIds: string[]) => boolean;
}

const targets = new Map<string, Map<string, CardInputTarget>>();

function targetForSession(sessionId: string): CardInputTarget | null {
  const cards = targets.get(sessionId);
  if (!cards) return null;
  let bottom: CardInputTarget | null = null;
  for (const candidate of cards.values()) {
    if (!candidate.element.isConnected) continue;
    if (
      !bottom ||
      bottom.element.compareDocumentPosition(candidate.element) & Node.DOCUMENT_POSITION_FOLLOWING
    ) {
      bottom = candidate;
    }
  }
  return bottom;
}

export type CardInsertResult = "absent" | "inserted" | "rejected";

export function setQuestionCardInputTarget(next: CardInputTarget): void {
  let cards = targets.get(next.sessionId);
  if (!cards) {
    cards = new Map();
    targets.set(next.sessionId, cards);
  }
  const existing = cards.get(next.cardId);
  if (existing?.questionKey === next.questionKey && existing.element === next.element) {
    existing.insert = next.insert;
  } else {
    cards.set(next.cardId, next);
  }
}

export function clearQuestionCardInputTarget(sessionId: string, cardId?: string): void {
  if (cardId === undefined) targets.delete(sessionId);
  else {
    const cards = targets.get(sessionId);
    cards?.delete(cardId);
    if (cards?.size === 0) targets.delete(sessionId);
  }
}

/** Capture the intended card before an asynchronous source action begins. */
export function captureQuestionCardInputTarget(sessionId: string): CardInputTarget | null {
  return targetForSession(sessionId);
}

export function insertIntoQuestionCard(
  sessionId: string,
  text: string,
  files: File[] = [],
  commentIds: string[] = [],
  intendedTarget: CardInputTarget | null = captureQuestionCardInputTarget(sessionId),
): CardInsertResult {
  if (!intendedTarget) return "absent";
  if (targetForSession(sessionId) !== intendedTarget || !intendedTarget.element.isConnected)
    return "rejected";
  if (!intendedTarget.insert(text, files, commentIds)) return "rejected";
  intendedTarget.element.focus({ preventScroll: true });
  return "inserted";
}

export function quoteForQuestionCard(text: string): string {
  return text
    .trim()
    .split("\n")
    .map((line) => `> ${line}`)
    .join("\n");
}
