import { afterEach, describe, expect, it } from "vitest";
import {
  captureQuestionCardInputTarget,
  clearQuestionCardInputTarget,
  insertIntoQuestionCard,
  quoteForQuestionCard,
  setQuestionCardInputTarget,
} from "./questionCardInput";

afterEach(() => {
  clearQuestionCardInputTarget("session-a");
  clearQuestionCardInputTarget("session-b");
  document.body.replaceChildren();
});

describe("question card input target", () => {
  it("routes only to the live target in the same session and clears it on resolution", () => {
    const field = document.createElement("textarea");
    document.body.append(field);
    const received: string[] = [];
    setQuestionCardInputTarget({
      sessionId: "session-a",
      cardId: "card-1",
      questionKey: "question-2",
      element: field,
      insert: (text) => {
        received.push(text);
        return true;
      },
    });

    expect(insertIntoQuestionCard("session-b", "wrong session")).toBe("absent");
    expect(insertIntoQuestionCard("session-a", "quoted text")).toBe("inserted");
    expect(received).toEqual(["quoted text"]);
    expect(document.activeElement).toBe(field);

    clearQuestionCardInputTarget("session-a");
    expect(insertIntoQuestionCard("session-a", "resolved card")).toBe("absent");
  });

  it("chooses the bottom-most connected card regardless of focus or viewport position", () => {
    const upper = document.createElement("textarea");
    const lower = document.createElement("textarea");
    lower.style.position = "absolute";
    lower.style.top = "100000px";
    document.body.append(upper, lower);
    const received: string[] = [];
    setQuestionCardInputTarget({
      sessionId: "session-a",
      cardId: "lower",
      questionKey: "q2",
      element: lower,
      insert: (text) => {
        received.push(`lower: ${text}`);
        return true;
      },
    });
    setQuestionCardInputTarget({
      sessionId: "session-a",
      cardId: "upper",
      questionKey: "q1",
      element: upper,
      insert: (text) => {
        received.push(`upper: ${text}`);
        return true;
      },
    });
    upper.focus();
    expect(insertIntoQuestionCard("session-a", "first")).toBe("inserted");
    expect(received).toEqual(["lower: first"]);
    clearQuestionCardInputTarget("session-a", "lower");
    expect(insertIntoQuestionCard("session-a", "second")).toBe("inserted");
    expect(received).toEqual(["lower: first", "upper: second"]);
  });

  it("keeps a captured target valid while its insertion callback is refreshed", () => {
    const field = document.createElement("textarea");
    document.body.append(field);
    const received: string[] = [];
    const base = {
      sessionId: "session-a",
      cardId: "card-1",
      questionKey: "q1",
      element: field,
    };
    setQuestionCardInputTarget({ ...base, insert: () => true });
    const captured = captureQuestionCardInputTarget("session-a");
    setQuestionCardInputTarget({
      ...base,
      insert: (text) => {
        received.push(text);
        return true;
      },
    });
    expect(insertIntoQuestionCard("session-a", "comment", [], [], captured)).toBe("inserted");
    expect(received).toEqual(["comment"]);
  });

  it("does not route to a removed field or clear a newer card with an old card's cleanup", () => {
    const oldField = document.createElement("textarea");
    const newField = document.createElement("textarea");
    document.body.append(oldField, newField);
    const received: string[] = [];
    setQuestionCardInputTarget({
      sessionId: "session-a",
      cardId: "old",
      questionKey: "one",
      element: oldField,
      insert: () => true,
    });
    const intended = captureQuestionCardInputTarget("session-a");
    setQuestionCardInputTarget({
      sessionId: "session-a",
      cardId: "new",
      questionKey: "two",
      element: newField,
      insert: (text) => {
        received.push(text);
        return true;
      },
    });
    clearQuestionCardInputTarget("session-a", "old");
    expect(insertIntoQuestionCard("session-a", "old answer", [], [], intended)).toBe("rejected");
    expect(insertIntoQuestionCard("session-a", "current answer")).toBe("inserted");
    expect(received).toEqual(["current answer"]);

    newField.remove();
    expect(insertIntoQuestionCard("session-a", "stale answer")).toBe("absent");
  });

  it("reports a busy target as rejected so callers cannot fall back to sending", () => {
    const field = document.createElement("textarea");
    document.body.append(field);
    setQuestionCardInputTarget({
      sessionId: "session-a",
      cardId: "busy",
      questionKey: "q1",
      element: field,
      insert: () => false,
    });
    expect(insertIntoQuestionCard("session-a", "answer")).toBe("rejected");
  });

  it("formats selected multiline text as a quote", () => {
    expect(quoteForQuestionCard("  first\nsecond  ")).toBe("> first\n> second");
  });
});
