import { sha256 } from "@noble/hashes/sha2.js";
import { bytesToHex } from "@noble/hashes/utils.js";
import { expect, it } from "vitest";
import {
  EMPTY_CHOICE,
  type ChoiceEnvelope,
  type ChoiceRow,
  type SessionChoiceDocument,
} from "./sessionChoiceData";
import { projectChoiceDrainTags } from "./choiceDrainLocks";
import {
  choiceDocumentForSave,
  limitRuleErrors,
  moveLimitRule,
  replaceLimitRules,
} from "./choiceLimitEdits";
import { removeWorkType, renameWorkType } from "./sessionChoiceEdits";

function document(): SessionChoiceDocument {
  return {
    ...structuredClone(EMPTY_CHOICE),
    schemaVersion: 2,
    workTypes: [{ id: "work", label: "Work" }],
    futureDocumentField: { keep: true },
    limitRules: [
      {
        id: "broad",
        agent: "agent-a",
        maxRunning: 3,
        reserve: { mode: "total", window: "weekly", pct: 20, stopPct: 0 },
        reserveExempt: ["work"],
      },
      {
        id: "specific",
        agent: "agent-a",
        host: null,
        model: "model-x",
        maxRunning: null,
        reserve: { pct: null },
        futureRuleField: "keep",
      },
    ],
    limits: { pool: { maxRunning: 2 } },
  };
}

it("reorders without materializing inherited fields or losing explicit null and unknown fields", () => {
  const before = document();
  const next = moveLimitRule(before, "specific", -1);
  const saved = JSON.parse(JSON.stringify(choiceDocumentForSave(next, before)));
  expect(saved.limitRules).toEqual([before.limitRules![1], before.limitRules![0]]);
  expect(saved.limitRules[0].reserve).toEqual({ pct: null });
  expect(saved.limitRules[0]).not.toHaveProperty("reserveExempt");
  expect(saved.futureDocumentField).toEqual({ keep: true });
  expect(saved.limits).toEqual(before.limits);
  expect(before.limitRules![0].id).toBe("broad");
});

it("upgrades only when rules are edited and preserves remaining legacy constraints", () => {
  const legacy = { ...structuredClone(EMPTY_CHOICE), limits: { pool: { maxRunning: 2 } } };
  expect(choiceDocumentForSave(legacy, legacy)).not.toHaveProperty("schemaVersion");
  expect(replaceLimitRules(legacy, [])).toMatchObject({
    schemaVersion: 2,
    limitRules: [],
    limits: { pool: { maxRunning: 2 } },
  });
});

it.each([0, -1, 1.5, NaN, Infinity])(
  "rejects invalid concurrency %s before JSON can turn it into unlimited",
  (maxRunning) => {
    const next = document();
    next.limitRules![0].maxRunning = maxRunning;
    expect(limitRuleErrors(next)).toHaveLength(1);
  },
);

it.each([-1, 101, NaN, Infinity])("rejects invalid percentage %s", (pct) => {
  const next = document();
  next.limitRules![0].reserve = { pct };
  expect(limitRuleErrors(next)).toHaveLength(1);
});

it("keeps zero percentages, null overrides and absence valid", () => {
  expect(limitRuleErrors(document())).toEqual([]);
});

it("renames and removes work type references without materializing absent exemptions", () => {
  const renamed = renameWorkType(document(), "work", "Design");
  expect(renamed.limitRules![0].reserveExempt).toEqual(["design"]);
  expect(renamed.limitRules![1]).not.toHaveProperty("reserveExempt");
  expect(removeWorkType(renamed, "design").limitRules![0].reserveExempt).toEqual([]);
});

it("validates multi-member rules and rejects empty members, wildcards, or reserve fields", () => {
  const valid: SessionChoiceDocument = {
    ...structuredClone(EMPTY_CHOICE),
    schemaVersion: 2,
    limitRules: [
      {
        id: "opus-cap",
        members: [
          { agent: "agent-a", host: "host-a", model: "opus-5" },
          { agent: "agent-b", host: "host-b", model: "opus-5" },
        ],
        maxRunning: 3,
      },
    ],
  };
  expect(limitRuleErrors(valid)).toEqual([]);

  expect(
    limitRuleErrors({
      ...valid,
      limitRules: [{ id: "empty", members: [], maxRunning: 2 }],
    }),
  ).toHaveLength(1);
  expect(
    limitRuleErrors({
      ...valid,
      limitRules: [{ id: "blank", members: [{ agent: "" }], maxRunning: 2 }],
    }),
  ).toHaveLength(1);
  expect(
    limitRuleErrors({
      ...valid,
      limitRules: [{ id: "wild", members: [{ agent: "agent-a", model: "*" }], maxRunning: 2 }],
    }),
  ).toHaveLength(1);
  expect(
    limitRuleErrors({
      ...valid,
      limitRules: [
        {
          id: "reserve",
          members: [{ agent: "agent-a" }, { agent: "agent-b" }],
          maxRunning: 2,
          reserve: { mode: "total", window: "weekly", pct: 10 },
        },
      ],
    }),
  ).toHaveLength(1);
});

it("projects drain tags for tiered fallback links using sorted-key permissionTier hash", () => {
  const hash = (value: string) => bytesToHex(sha256(new TextEncoder().encode(value)));
  const primary = { agent: "agent-a", model: "model-a" };
  const fallback = {
    agent: "agent-b",
    model: "model-b",
    fallbackMode: "drain" as const,
    permissionTier: "accept-edits" as const,
  };
  const row: ChoiceRow = { links: [primary, fallback] };
  const serverRowIdentity = hash(
    JSON.stringify(
      [
        hash(JSON.stringify({ agent: "agent-a", model: "model-a" })),
        hash(
          JSON.stringify({
            agent: "agent-b",
            model: "model-b",
            permissionTier: "accept-edits",
          }),
        ),
      ].sort(),
    ),
  );
  const saved: ChoiceEnvelope = {
    settings: {
      ...structuredClone(EMPTY_CHOICE),
      workTypes: [{ id: "work", label: "Work" }],
      hosts: { "host-a": { work: [[row]] } },
    },
    revision: "rev-1",
    runtime: {
      settingsRevision: "rev-1",
      drainLocks: [
        {
          hostId: "host-a",
          workType: "work",
          tierIndex: 0,
          rowPosition: 0,
          savedLink: { agent: "agent-b", model: "model-b", permissionTier: "accept-edits" },
          rowIdentity: serverRowIdentity,
          settingsRevision: "rev-1",
          since: 1234,
        },
      ],
    },
  };
  const tags = projectChoiceDrainTags(saved, "host-a", "work", [[row]]);
  expect(tags.get(row)).toEqual({ linkIndex: 1, since: 1234 });

  const changedTierRow: ChoiceRow = {
    links: [primary, { ...fallback, permissionTier: "plan" }],
  };
  expect(projectChoiceDrainTags(saved, "host-a", "work", [[changedTierRow]]).size).toBe(0);
});
