import { expect, it } from "vitest";
import { terminalTextareaEdit } from "./terminalTextareaEdit";

it.each([
  ["", 0, "()", 1, false, "()\x1b[D"],
  ["()", 1, "(中文)", 3, false, "中文"],
  ["你好世界", 4, "您好世界", 1, false, "\x1b[D\x1b[D\x1b[D\x7f您"],
  ["abc", 1, "abc", 3, true, "\x1bOC\x1bOC"],
  ["A😀B", 3, "AB", 1, false, "\x7f"],
])("edits %s at %i into %s at %i", (before, start, after, end, application, expected) => {
  expect(
    terminalTextareaEdit(
      { value: before, selectionStart: start, selectionEnd: start },
      { value: after, selectionStart: end, selectionEnd: end },
      application,
    ),
  ).toBe(expected);
});
