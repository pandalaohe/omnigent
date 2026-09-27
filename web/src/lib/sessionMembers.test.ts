import { describe, expect, it } from "vitest";

import {
  hasMultipleMembers,
  memberUnavailableReason,
  parseSessionMembers,
  unavailableMembers,
} from "./sessionMembers";

/** Compact JSON snapshot value, as the F1a snapshot writer emits it. */
function snapshot(overrides: Record<string, unknown> = {}): string {
  return JSON.stringify({
    host: null,
    harness: "codex",
    model: null,
    effort: null,
    lead: false,
    ...overrides,
  });
}

const MEMBER_KEY = "omnigent.member.";

describe("parseSessionMembers", () => {
  it("parses a full member value into the roster shape", () => {
    const members = parseSessionMembers({
      [`${MEMBER_KEY}reviewer`]: snapshot({
        host: "Desktop-HRF",
        harness: "claude-sdk",
        model: "opus-4.8",
        effort: "high",
        lead: false,
      }),
    });
    expect(members).toEqual([
      {
        role: "reviewer",
        host: "Desktop-HRF",
        harness: "claude-sdk",
        model: "opus-4.8",
        effort: "high",
        lead: false,
        unavailable: null,
      },
    ]);
  });

  it("orders the lead first, then label order", () => {
    const members = parseSessionMembers({
      [`${MEMBER_KEY}executor`]: snapshot(),
      [`${MEMBER_KEY}architect`]: snapshot({ lead: true }),
      [`${MEMBER_KEY}reviewer`]: snapshot(),
    });
    expect(members.map((member) => member.role)).toEqual(["architect", "executor", "reviewer"]);
  });

  it("ignores malformed values and non-member labels", () => {
    const members = parseSessionMembers({
      "omnigent.wrapper": "claude-native",
      [`${MEMBER_KEY}broken`]: "{not json",
      [`${MEMBER_KEY}scalar`]: "42",
      [`${MEMBER_KEY}array`]: "[]",
      [`${MEMBER_KEY}noharness`]: JSON.stringify({ host: "Mac", model: "sonnet" }),
      [`${MEMBER_KEY}`]: snapshot(),
      [`${MEMBER_KEY}ok`]: snapshot({ harness: "codex" }),
    });
    expect(members.map((member) => member.role)).toEqual(["ok"]);
  });

  it("tolerates missing optional fields and non-string values", () => {
    const members = parseSessionMembers({
      [`${MEMBER_KEY}lead`]: JSON.stringify({ harness: "codex", lead: true, model: 7 }),
    });
    expect(members).toEqual([
      {
        role: "lead",
        host: null,
        harness: "codex",
        model: null,
        effort: null,
        lead: true,
        unavailable: null,
      },
    ]);
  });

  it("reads the unavailable reason code", () => {
    const members = parseSessionMembers({
      [`${MEMBER_KEY}reviewer`]: snapshot({ unavailable: "host_offline" }),
    });
    expect(members[0]?.unavailable).toBe("host_offline");
    expect(unavailableMembers(members).map((member) => member.role)).toEqual(["reviewer"]);
  });

  it("returns an empty roster for absent labels", () => {
    expect(parseSessionMembers(undefined)).toEqual([]);
    expect(parseSessionMembers({})).toEqual([]);
  });

  it("detects a 2+ member roster", () => {
    expect(hasMultipleMembers([])).toBe(false);
    expect(
      hasMultipleMembers(parseSessionMembers({ [`${MEMBER_KEY}a`]: snapshot({ lead: true }) })),
    ).toBe(false);
    expect(
      hasMultipleMembers(
        parseSessionMembers({
          [`${MEMBER_KEY}a`]: snapshot({ lead: true }),
          [`${MEMBER_KEY}b`]: snapshot(),
        }),
      ),
    ).toBe(true);
  });
});

describe("memberUnavailableReason", () => {
  it.each([
    ["host_offline", "host offline"],
    ["harness_not_configured", "harness not set up on the host"],
    ["binary-missing", "CLI missing"],
    ["needs-auth", "sign-in needed"],
    ["version-too-low", "CLI too old"],
    ["model_missing", "model not offered by the host"],
  ])("maps %s to a human reason", (code, reason) => {
    expect(memberUnavailableReason(code)).toBe(reason);
  });

  it("falls back to the raw code for an unknown reason", () => {
    expect(memberUnavailableReason("something_new")).toBe("something_new");
  });
});
