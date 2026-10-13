/** The free-text answer most recently used in a pending async question card. */
export interface CardInputTarget {
  sessionId: string;
  cardId: string;
  questionKey: string;
  element: HTMLTextAreaElement;
  insert: (text: string, files: File[], commentIds: string[]) => boolean;
}

let target: CardInputTarget | null = null;

export type CardInsertResult = "absent" | "inserted" | "rejected";

export function setQuestionCardInputTarget(next: CardInputTarget): void {
  target = next;
}

export function clearQuestionCardInputTarget(sessionId: string, cardId?: string): void {
  if (target?.sessionId === sessionId && (cardId === undefined || target.cardId === cardId)) {
    target = null;
  }
}

/** Capture the intended card before an asynchronous source action begins. */
export function captureQuestionCardInputTarget(sessionId: string): CardInputTarget | null {
  return target?.sessionId === sessionId ? target : null;
}

export function insertIntoQuestionCard(
  sessionId: string,
  text: string,
  files: File[] = [],
  commentIds: string[] = [],
  intendedTarget: CardInputTarget | null = captureQuestionCardInputTarget(sessionId),
): CardInsertResult {
  if (!intendedTarget) return "absent";
  if (target !== intendedTarget || !intendedTarget.element.isConnected) return "rejected";
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
