/*
 * Terminal-key sequence codec from Dinotty.
 *
 * MIT License
 *
 * Copyright (c) 2026 ChenXi
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in all
 * copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
 * SOFTWARE.
 */
export const FKEY_SEQ: Record<string, string> = {
  F1: "\x1bOP",
  F2: "\x1bOQ",
  F3: "\x1bOR",
  F4: "\x1bOS",
  F5: "\x1b[15~",
  F6: "\x1b[17~",
  F7: "\x1b[18~",
  F8: "\x1b[19~",
  F9: "\x1b[20~",
  F10: "\x1b[21~",
  F11: "\x1b[23~",
  F12: "\x1b[24~",
};

export function letterFromPhysicalCode(code: string): string | null {
  if (code.startsWith("Key")) return code.slice(3).toLowerCase();
  if (code.startsWith("Digit")) return code.slice(5);
  return null;
}

export function keyEventToSequence(
  e: Pick<KeyboardEvent, "key" | "code" | "ctrlKey" | "altKey" | "shiftKey" | "metaKey">,
): string {
  const ctrl = e.ctrlKey || e.metaKey;
  const alt = e.altKey;
  const shift = e.shiftKey;
  const hasMod = ctrl || alt || shift;

  // xterm modifier parameter: 1 (no mod) + shift:1, alt:2, ctrl:4
  const modBit = 1 + (shift ? 1 : 0) + (alt ? 2 : 0) + (ctrl ? 4 : 0);

  const fk = FKEY_SEQ[e.key];
  if (fk) {
    if (!hasMod) return fk;
    // F1-F4: SS3 (O) unmodified -> CSI 1;{mod}{letter} modified
    // F5-F12: CSI {n}~ unmodified -> CSI {n};{mod}~ modified
    const fkeyModified: Record<string, string> = {
      F1: `\x1b[1;${modBit}P`,
      F2: `\x1b[1;${modBit}Q`,
      F3: `\x1b[1;${modBit}R`,
      F4: `\x1b[1;${modBit}S`,
      F5: `\x1b[15;${modBit}~`,
      F6: `\x1b[17;${modBit}~`,
      F7: `\x1b[18;${modBit}~`,
      F8: `\x1b[19;${modBit}~`,
      F9: `\x1b[20;${modBit}~`,
      F10: `\x1b[21;${modBit}~`,
      F11: `\x1b[23;${modBit}~`,
      F12: `\x1b[24;${modBit}~`,
    };
    return fkeyModified[e.key] ?? fk;
  }

  // CSI special keys: arrows, home/end, pageup/down, insert/delete
  // Unmodified: omit param-1 for letter suffixes (\x1b[A), keep param for ~ suffixes (\x1b[5~)
  // Modified: \x1b[{param};{mod}{letter} or \x1b[{param};{mod}~
  const csiSpecial: Record<string, { param: string; suffix: string }> = {
    ArrowUp: { param: "1", suffix: "A" },
    ArrowDown: { param: "1", suffix: "B" },
    ArrowRight: { param: "1", suffix: "C" },
    ArrowLeft: { param: "1", suffix: "D" },
    Home: { param: "1", suffix: "H" },
    End: { param: "1", suffix: "F" },
    PageUp: { param: "5", suffix: "~" },
    PageDown: { param: "6", suffix: "~" },
    Insert: { param: "2", suffix: "~" },
    Delete: { param: "3", suffix: "~" },
  };
  const csi = csiSpecial[e.key];
  if (csi) {
    if (!hasMod) {
      if (csi.suffix === "~") return `\x1b[${csi.param}~`;
      return `\x1b[${csi.suffix}`;
    }
    if (csi.suffix === "~") return `\x1b[${csi.param};${modBit}~`;
    return `\x1b[${csi.param};${modBit}${csi.suffix}`;
  }

  let ch: string;
  if (e.key === "Escape") ch = "\x1b";
  else if (e.key === "Tab") ch = e.shiftKey ? "\x1b[Z" : "\t";
  else if (e.key === "Backspace") ch = "\x7f";
  else if (e.key === "Enter") ch = "\r";
  else if (Array.from(e.key).length === 1) {
    ch = e.key;
    if (ctrl) {
      const code = ch.toUpperCase().charCodeAt(0) - 64;
      if (code >= 1 && code <= 26) return String.fromCharCode(code);
    }
    if (alt) return "\x1b" + ch;
    return ch;
  } else {
    const phys = letterFromPhysicalCode(e.code);
    if (phys && phys.length === 1) {
      if (ctrl) {
        const code = phys.toUpperCase().charCodeAt(0) - 64;
        if (code >= 1 && code <= 26) return String.fromCharCode(code);
      }
      if (alt) return "\x1b" + phys;
      return phys;
    }
    return "";
  }

  if (alt && ch.length > 0) return "\x1b" + ch;
  return ch;
}

export function keyEventToLabel(
  e: Pick<KeyboardEvent, "key" | "ctrlKey" | "altKey" | "shiftKey" | "metaKey">,
): string {
  const parts: string[] = [];
  if (e.ctrlKey) parts.push("ctrl");
  if (e.metaKey) parts.push("cmd");
  if (e.altKey) parts.push("opt");
  if (e.shiftKey) parts.push("shift");

  let key = e.key;
  if (key === " ") key = "space";
  else if (key === "Escape") key = "esc";
  else if (key === "Backspace") key = "⌫";
  else if (key === "Tab") key = "tab";
  else if (key === "Enter") key = "↵";
  else if (key === "ArrowUp") key = "↑";
  else if (key === "ArrowDown") key = "↓";
  else if (key === "ArrowLeft") key = "←";
  else if (key === "ArrowRight") key = "->";
  else if (key.length === 1) key = key.toLowerCase();
  // F1-F12 / PageUp / PageDown / Insert / Delete / Home / End: keep as-is

  // Modifier-only events (Ctrl/Alt/Shift/Meta alone) return just the modifier list
  if (["Control", "Alt", "Shift", "Meta"].includes(e.key)) {
    return parts.join("+");
  }

  if (parts.length) {
    parts.push(key);
    return parts.join("+");
  }
  return key;
}

export function escapeForDisplay(s: string | undefined): string {
  if (s === undefined) return "";
  // eslint-disable-next-line no-control-regex -- The codec serializes terminal control bytes.
  return s.replace(/[\x00-\x1f\x7f]/g, (c) => {
    const code = c.charCodeAt(0);
    if (code === 0x1b) return "\\e";
    if (code === 0x09) return "\\t";
    if (code === 0x0d) return "\\r";
    if (code === 0x0a) return "\\n";
    if (code === 0x7f) return "\\x7f";
    if (code <= 26) return "^" + String.fromCharCode(code + 64);
    return "\\x" + code.toString(16).padStart(2, "0");
  });
}

export function unescapeFromDisplay(s: string): string {
  return s.replace(/\\e|\\t|\\r|\\n|\\x([0-9a-fA-F]{2})|\^([A-Z@[\\\]^_?])/g, (m, hex, caret) => {
    if (m === "\\e") return "\x1b";
    if (m === "\\t") return "\t";
    if (m === "\\r") return "\r";
    if (m === "\\n") return "\n";
    if (hex) return String.fromCharCode(parseInt(hex, 16));
    if (caret) {
      if (caret === "?") return "\x7f";
      if (caret === "@") return "\x00";
      return String.fromCharCode(caret.charCodeAt(0) - 64);
    }
    return m;
  });
}

/** Encode literal legacy text without interpreting its backslashes or carets. */
export function escapeLiteral(text: string): string {
  return escapeForDisplay(text.replace(/[\\^]/g, (char) => (char === "\\" ? "\\x5c" : "\\x5e")));
}
