// Unit tests for the owner-facing comment display helpers: the visitor
// author label and the counts shown before a delete.

import { describe, expect, it } from "vitest";
import {
  bulkCommentsDeleteLine,
  commentAuthorLabel,
  unhandledCommentsDeleteLine,
} from "./comments";

describe("commentAuthorLabel", () => {
  it("labels a named visitor", () => {
    expect(commentAuthorLabel("visitor:Alice")).toBe("Visitor · Alice");
  });

  it("labels an unnamed visitor", () => {
    expect(commentAuthorLabel("visitor:")).toBe("Visitor");
  });

  it("passes a real author through unchanged", () => {
    expect(commentAuthorLabel("alice@example.com")).toBe("alice@example.com");
  });

  it("falls back to You without an author", () => {
    // Legacy / single-user rows store no author; the panel has always
    // shown them as the viewer's own.
    expect(commentAuthorLabel(null)).toBe("You");
  });
});

describe("unhandledCommentsDeleteLine", () => {
  it("counts draft comments and how many came from visitors", () => {
    const line = unhandledCommentsDeleteLine([
      { status: "draft", created_by: "visitor:Alice" },
      { status: "draft", created_by: "visitor:" },
      { status: "draft", created_by: "alice@example.com" },
      { status: "addressed", created_by: "visitor:Bob" },
    ]);
    expect(line).toBe("3 unhandled comments (2 from visitors) will be deleted.");
  });

  it("is null when nothing is unhandled", () => {
    expect(
      unhandledCommentsDeleteLine([{ status: "addressed", created_by: "visitor:Alice" }]),
    ).toBeNull();
    expect(unhandledCommentsDeleteLine([])).toBeNull();
  });
});

describe("bulkCommentsDeleteLine", () => {
  it("names the total across the sessions", () => {
    expect(bulkCommentsDeleteLine(5)).toBe("These sessions hold 5 comments; they will be deleted.");
  });

  it("is null when the sessions hold no comments", () => {
    expect(bulkCommentsDeleteLine(0)).toBeNull();
  });
});
