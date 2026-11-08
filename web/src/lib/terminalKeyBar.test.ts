import { describe, expect, it } from "vitest";
import envelopeFixturesJson from "../../../tests/fixtures/terminal_bar_preference_sizes.json?raw";

import {
  DEFAULT_TERMINAL_KEY_ITEMS,
  EMPTY_STICKY_MODIFIERS,
  MAX_PREFERENCE_ENVELOPE_BYTES,
  MAX_ROW_PAGES,
  barHeightPx,
  canonicalTerminalFields,
  canonicalizeBar,
  defaultTerminalBar,
  flattenBar,
  legacyFingerprint,
  migrateLegacy,
  moveBarKey,
  normalizeTerminalButtons,
  packRow,
  preferencesEnvelopeBytes,
  projectLegacy,
  reconcileLegacy,
  reduceSticky,
  resolveTerminalBar,
  validateBar,
  type TerminalBar,
  type TerminalBarKey,
} from "./terminalKeyBar";

const key = (id: string, width: 1 | 2 = 2, extra: Partial<TerminalBarKey> = {}): TerminalBarKey =>
  ({
    id,
    label: id,
    kind: "key",
    chord: { code: "KeyA", modifiers: [] },
    display: "text",
    width,
    ...extra,
  }) as TerminalBarKey;

function baseBar(): TerminalBar {
  return {
    top: { keys: [key("esc"), key("up")], pinned: 0, enabled: true },
    bottom: { keys: [key("tab"), key("ctrl-c")], pinned: 0, enabled: true },
  };
}

describe("terminalKeyBar migration and projection", () => {
  it("migrates today's flat list cell-for-cell without writing", () => {
    const bar = migrateLegacy(DEFAULT_TERMINAL_KEY_ITEMS, true);
    expect(bar.top.keys.map((item) => item.id)).toEqual([
      "esc",
      "up",
      "down",
      "enter",
      "ctrl-c",
      "keyboard",
    ]);
    expect(bar.bottom.keys).toHaveLength(12);
    expect(bar.top.keys.every((item) => item.width === 2)).toBe(true);
    expect(bar.bottom.keys.every((item) => item.width === 2)).toBe(true);
    expect(bar.top.pinned).toBe(0);
    expect(bar.bottom.enabled).toBe(true);
    expect(bar.top.keys.at(-1)).toMatchObject({ display: "icon", icon: "keyboard" });
  });

  it("keeps every bottom-row key when the legacy second row was off", () => {
    const bar = migrateLegacy(DEFAULT_TERMINAL_KEY_ITEMS, false);
    expect(bar.bottom.keys).toHaveLength(12);
    expect(bar.bottom.enabled).toBe(false);
  });

  it("projects only old-build keys, cuts labels, and is a fixed point of the old normalizer", () => {
    const bar = canonicalizeBar({
      top: {
        keys: [
          key("a", 2, { label: "a very long label over 24 chars" }),
          {
            id: "text",
            label: "ok",
            kind: "text",
            text: "ok",
            submit: true,
            display: "text",
            width: 1,
          } as TerminalBarKey,
          {
            id: "shift",
            label: "Shift",
            kind: "modifier",
            modifier: "shift",
            display: "text",
            width: 2,
          } as TerminalBarKey,
        ],
        pinned: 0,
        enabled: true,
      },
      bottom: {
        keys: [
          {
            id: "ctrl",
            label: "Ctrl",
            kind: "modifier",
            modifier: "ctrl",
            display: "text",
            width: 2,
          },
        ],
        pinned: 0,
        enabled: true,
      },
    });
    const projected = projectLegacy(bar);
    expect(projected.map((item) => item.id)).toEqual(["a", "ctrl"]);
    expect(projected[0].label).toHaveLength(24);
    expect(normalizeTerminalButtons(projected)).toEqual(projected);
    expect(projectLegacy(canonicalizeBar(migrateLegacy(projected, true)))).toEqual(projected);
  });

  it("fingerprints normalized old fields independent of key order in the JSON", () => {
    const reordered = DEFAULT_TERMINAL_KEY_ITEMS.map((item) => ({
      ...item,
      chord:
        item.kind === "key"
          ? { modifiers: item.chord.modifiers, code: item.chord.code }
          : undefined,
    }));
    expect(legacyFingerprint(DEFAULT_TERMINAL_KEY_ITEMS, true)).toBe(
      legacyFingerprint(reordered, true),
    );
    expect(legacyFingerprint(DEFAULT_TERMINAL_KEY_ITEMS, false)).not.toBe(
      legacyFingerprint(DEFAULT_TERMINAL_KEY_ITEMS, true),
    );
  });
});

describe("terminalKeyBar reconcile", () => {
  it("keeps untouched rich keys while applying an old build's reorder, delete and add", () => {
    const rich = canonicalizeBar({
      top: {
        keys: [
          key("esc", 2, { label: "Esc" }),
          { ...key("tab", 1), label: "Tab" },
          {
            id: "backspace",
            label: "Backspace",
            kind: "key",
            chord: { code: "Backspace", modifiers: [] },
            repeat: true,
            display: "text",
            width: 2,
          },
          {
            id: "text-ok",
            label: "OK",
            kind: "text",
            text: "ok",
            submit: true,
            display: "text",
            width: 1,
          },
          {
            id: "shift",
            label: "Shift",
            kind: "modifier",
            modifier: "shift",
            tapLocks: true,
            display: "text",
            width: 2,
          },
          key("delete-me"),
        ],
        pinned: 2,
        enabled: true,
      },
      bottom: {
        keys: [
          key("a"),
          { ...key("long"), label: "a label longer than twenty-four characters" },
          key("b"),
        ],
        pinned: 0,
        enabled: true,
      },
    });
    // The old build saw only the projection. It moves esc after tab, deletes
    // delete-me, adds an unknown key at the end, and turns the second row off.
    const legacy = [
      { id: "tab", label: "Tab", kind: "key", chord: { code: "Tab", modifiers: [] } },
      { id: "esc", label: "Esc", kind: "key", chord: { code: "Escape", modifiers: [] } },
      {
        id: "backspace",
        label: "Backspace",
        kind: "key",
        chord: { code: "Backspace", modifiers: [] },
        repeat: true,
      },
      { id: "a", label: "A", kind: "key", chord: { code: "KeyA", modifiers: [] } },
      {
        id: "long",
        label: "a label longer than twenty-four characters",
        kind: "key",
        chord: { code: "KeyA", modifiers: [] },
      },
      { id: "b", label: "B", kind: "key", chord: { code: "KeyB", modifiers: [] } },
      { id: "new", label: "New", kind: "key", chord: { code: "KeyN", modifiers: [] } },
    ];
    const reconciled = reconcileLegacy(rich, legacy, false);
    expect(reconciled.bottom.enabled).toBe(false);
    expect(reconciled.top.keys.map((item) => item.id)).toEqual([
      "tab",
      "esc",
      "backspace",
      "text-ok",
      "shift",
    ]);
    expect(reconciled.top.pinned).toBe(2);
    expect(reconciled.top.keys.find((item) => item.id === "tab")?.width).toBe(1);
    const backspace = reconciled.top.keys.find((item) => item.id === "backspace");
    expect(backspace?.kind === "key" && backspace.repeat).toBe(true);
    expect(reconciled.top.keys.find((item) => item.id === "text-ok")?.kind).toBe("text");
    expect(reconciled.top.keys.find((item) => item.id === "shift")?.kind).toBe("modifier");
    expect(reconciled.top.keys.some((item) => item.id === "delete-me")).toBe(false);
    expect(reconciled.bottom.keys.map((item) => item.id)).toEqual(["a", "long", "b", "new"]);
    expect(reconciled.bottom.keys.at(-1)).toMatchObject({ width: 2, display: "text" });
  });

  it("reports stored when an old build touches only chat controls", () => {
    const bar = defaultTerminalBar();
    const terminalButtons = projectLegacy(bar);
    const resolved = resolveTerminalBar({
      terminalButtons,
      terminalSecondRow: true,
      terminalBar: bar,
      terminalLegacyBasis: legacyFingerprint(terminalButtons, true),
    });
    expect(resolved.source).toBe("stored");
    expect(resolved.bar.top.keys.map((item) => item.id)).toEqual(
      bar.top.keys.map((item) => item.id),
    );
  });
});

describe("terminalKeyBar validation", () => {
  it("reports each structural rule separately", () => {
    expect(
      validateBar({ ...baseBar(), top: { ...baseBar().top, pinned: 9 } }).structural,
    ).not.toHaveLength(0);
    expect(
      validateBar({ ...baseBar(), top: { keys: [], pinned: 0, enabled: true } }).structural,
    ).toHaveLength(0);
    expect(
      validateBar({
        ...baseBar(),
        top: {
          keys: [key("x", 3 as 2)],
          pinned: 0,
          enabled: true,
        },
      }).structural,
    ).not.toHaveLength(0);
    expect(
      validateBar({
        ...baseBar(),
        top: {
          keys: [key("same"), key("same")],
          pinned: 0,
          enabled: true,
        },
      }).structural,
    ).toEqual(expect.arrayContaining(["duplicate key id same"]));
    expect(
      validateBar({
        top: {
          keys: [key("a"), key("b"), key("c"), key("d"), key("e"), key("f"), key("g")],
          pinned: 7,
          enabled: true,
        },
        bottom: { keys: [], pinned: 0, enabled: true },
      }).structural,
    ).toContain("top pinned keys exceed 12 cells");
  });

  it("distinguishes new-data limits from unchanged legacy overruns", () => {
    const long = canonicalizeBar({
      top: {
        keys: [{ ...key("long"), label: "a label longer than twelve" }],
        pinned: 0,
        enabled: true,
      },
      bottom: { keys: [], pinned: 0, enabled: true },
    });
    const unchanged = validateBar(long, { before: long });
    expect(unchanged.limits.find((issue) => issue.kind === "label")?.worse).toBe(false);
    const edited = canonicalizeBar({
      ...long,
      top: {
        ...long.top,
        keys: [{ ...long.top.keys[0], label: "another long label" }],
      },
    });
    expect(
      validateBar(edited, { before: long }).limits.find((issue) => issue.kind === "label")?.worse,
    ).toBe(true);
    expect(validateBar(long).limits.find((issue) => issue.kind === "label")?.worse).toBe(true);
  });

  it("refuses a sixth page only for a row that grew past the legacy page count", () => {
    const keys = Array.from({ length: 31 }, (_, index) => key(`k${index}`));
    const row = { keys, pinned: 0, enabled: true };
    const bar: TerminalBar = { top: row, bottom: { keys: [], pinned: 0, enabled: true } };
    expect(packRow(row).pages).toHaveLength(6);
    const pages = validateBar(bar).limits.find((issue) => issue.kind === "pages");
    expect(pages?.worse).toBe(true);
    expect(
      validateBar(bar, { before: bar }).limits.find((issue) => issue.kind === "pages")?.worse,
    ).toBe(false);
  });
});

describe("terminalKeyBar packing and height", () => {
  it("inserts before or after a target boundary without moving an unrelated pin", () => {
    const bar = {
      top: { keys: [key("A"), key("B"), key("C")], pinned: 1, enabled: true },
      bottom: { keys: [], pinned: 0, enabled: true },
    };
    const afterA = moveBarKey(bar, "C", "top", 1, false);
    expect(afterA.top.keys.map((item) => item.id)).toEqual(["A", "C", "B"]);
    expect(afterA.top.pinned).toBe(1);
    const beforeA = moveBarKey(bar, "C", "top", 0, true);
    expect(beforeA.top.keys.map((item) => item.id)).toEqual(["C", "A", "B"]);
    expect(beforeA.top.pinned).toBe(2);
    const out = moveBarKey(bar, "A", "top", 3, false);
    expect(out.top.keys.map((item) => item.id)).toEqual(["B", "C", "A"]);
    expect(out.top.pinned).toBe(0);
    const cross = moveBarKey(bar, "A", "bottom", 0, false);
    expect(cross.top.pinned).toBe(0);
    expect(cross.bottom.keys[0].id).toBe("A");
    expect(bar.top.keys.map((item) => item.id)).toEqual(["A", "B", "C"]);
    const twoPins = { ...bar, top: { ...bar.top, pinned: 2 } };
    const withinPins = moveBarKey(twoPins, "A", "top", 2, true);
    expect(withinPins.top.keys.map((item) => item.id)).toEqual(["B", "A", "C"]);
    expect(withinPins.top.pinned).toBe(2);
    const outOfPins = moveBarKey(twoPins, "A", "top", 3, false);
    expect(outOfPins.top.keys.map((item) => item.id)).toEqual(["B", "C", "A"]);
    expect(outOfPins.top.pinned).toBe(1);
  });

  it("keeps unchanged legacy label and id overruns when a key moves to the other row", () => {
    const bar = baseBar();
    const legacy = key("long-id-".repeat(5), 2, { label: "a legacy label longer than twelve" });
    bar.top.keys.push(legacy);
    const moved = moveBarKey(bar, legacy.id, "bottom", bar.bottom.keys.length, false);
    const validation = validateBar(moved, { before: bar });
    expect(validation.structural).toEqual([]);
    expect(validation.limits).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ kind: "label", worse: false }),
        expect.objectContaining({ kind: "id", worse: false }),
      ]),
    );
    expect(validation.limits.some((issue) => issue.worse)).toBe(false);
  });
  it("repeats pinned keys on every page and packs greedily by width", () => {
    const pinned = [key("down", 1), key("ctrl", 2)];
    const rest = Array.from({ length: 12 }, (_, index) => key(`k${index}`, 2));
    const pack = packRow({ keys: [...pinned, ...rest], pinned: 2, enabled: true });
    expect(pack.pagerCells).toBe(9);
    expect(pack.pinned.map((item) => item.id)).toEqual(["down", "ctrl"]);
    expect(pack.pages).toHaveLength(3);
    expect(pack.pages.map((page) => page.length)).toEqual([4, 4, 4]);
  });

  it("flattens both rows even when the bottom row is off", () => {
    const bar = baseBar();
    bar.bottom.enabled = false;
    expect(flattenBar(bar).map((item) => item.id)).toEqual(["esc", "up", "tab", "ctrl-c"]);
  });

  it("computes the documented bar heights", () => {
    const onePage: TerminalBar = {
      top: { keys: [key("a")], pinned: 0, enabled: true },
      bottom: { keys: [key("b")], pinned: 0, enabled: true },
    };
    expect(barHeightPx(onePage, false)).toBe(93);
    expect(barHeightPx(onePage, true)).toBe(47);
    const paging: TerminalBar = {
      top: { keys: Array.from({ length: 13 }, (_, i) => key(`t${i}`)), pinned: 0, enabled: true },
      bottom: { keys: [key("b")], pinned: 0, enabled: true },
    };
    expect(barHeightPx(paging, false)).toBe(119);
    expect(barHeightPx(paging, true)).toBe(73);
    const bottomOff = { ...paging, bottom: { ...paging.bottom, enabled: false } };
    expect(barHeightPx(bottomOff, false)).toBe(73);
  });
});

describe("terminalKeyBar resolve, canonical fields and size", () => {
  it("matches the shared server fixture at and just beyond the UTF-8 budget", () => {
    const fixtures = JSON.parse(envelopeFixturesJson) as {
      expected_bytes: number;
      envelope: { version: 1; settings: Record<string, unknown> };
    }[];
    for (const fixture of fixtures)
      expect(preferencesEnvelopeBytes(fixture.envelope)).toBe(fixture.expected_bytes);
  });
  it("falls back to the legacy list, then defaults, without touching the stored bar", () => {
    const invalid = { ...defaultTerminalBar(), top: { ...defaultTerminalBar().top, pinned: 9 } };
    const legacy = resolveTerminalBar({
      terminalBar: invalid,
      terminalButtons: DEFAULT_TERMINAL_KEY_ITEMS,
      terminalSecondRow: true,
    });
    expect(legacy.source).toBe("fallback-legacy");
    expect(legacy.bar.top.keys).toHaveLength(6);
    expect(invalid.top.pinned).toBe(9);

    const defaults = resolveTerminalBar({ terminalBar: invalid });
    expect(defaults.source).toBe("fallback-defaults");
    expect(defaults.bar).toEqual(defaultTerminalBar());
  });

  it("marks an invalid preset without affecting the live bar", () => {
    const resolved = resolveTerminalBar({
      terminalButtons: DEFAULT_TERMINAL_KEY_ITEMS,
      terminalBar: defaultTerminalBar(),
      terminalBarPreset: {
        ...defaultTerminalBar(),
        top: { ...defaultTerminalBar().top, pinned: 9 },
      },
    });
    expect(resolved.preset).toBe("invalid");
    expect(resolved.bar.top.keys).toHaveLength(6);
  });

  it("canonicalizes idempotently and passes invalid values through", () => {
    const raw = {
      version: 2,
      enabled: true,
      buttons: [],
      terminalBar: { invalid: true },
      terminalBarPreset: "unreadable",
      terminalLegacyBasis: "fnv1a:00000000",
      terminalSecondRow: false,
    };
    const once = canonicalTerminalFields(raw);
    const twice = canonicalTerminalFields(once);
    expect(twice).toEqual(once);
    expect(JSON.stringify(twice)).toBe(JSON.stringify(once));
    expect(once.terminalBar).toEqual({ invalid: true });
    expect(once.terminalBarPreset).toBe("unreadable");
  });

  it("matches the server's UTF-8, sorted-key envelope rule, excluding prompts", () => {
    const envelope = {
      version: 1 as const,
      settings: {
        mobile_assistant: { terminalBar: { note: "😀" } },
        builtin_agent_prompts: { huge: "x".repeat(1000) },
      },
    };
    const expected = new TextEncoder().encode(
      '{"settings":{"mobile_assistant":{"terminalBar":{"note":"😀"}}},"version":1}',
    ).length;
    expect(preferencesEnvelopeBytes(envelope)).toBe(expected);
    expect(MAX_PREFERENCE_ENVELOPE_BYTES).toBe(65_536);
    expect(MAX_ROW_PAGES).toBe(5);
  });
});

describe("sticky reducer", () => {
  it("implements tap, long-press and consumed transitions for all three modifiers", () => {
    let state = { ...EMPTY_STICKY_MODIFIERS };
    state = reduceSticky(state, { type: "tap", mod: "ctrl", tapLocks: false });
    expect(state.ctrl).toBe("once");
    state = reduceSticky(state, { type: "tap", mod: "ctrl", tapLocks: false });
    expect(state.ctrl).toBe("off");
    state = reduceSticky(state, { type: "longPress", mod: "shift", tapLocks: false });
    expect(state.shift).toBe("locked");
    state = reduceSticky(state, { type: "tap", mod: "shift", tapLocks: false });
    expect(state.shift).toBe("off");
    state = reduceSticky(state, { type: "tap", mod: "alt", tapLocks: true });
    expect(state.alt).toBe("locked");
    state = reduceSticky(state, { type: "tap", mod: "alt", tapLocks: true });
    expect(state.alt).toBe("off");
    state = reduceSticky({ ctrl: "once", alt: "locked", shift: "once" }, { type: "consumed" });
    expect(state).toEqual({ ctrl: "off", alt: "locked", shift: "off" });
    expect(reduceSticky(state, { type: "reset" })).toEqual(EMPTY_STICKY_MODIFIERS);
  });
});
