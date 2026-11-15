import { expect, it } from "vitest";
import { EMPTY_CHOICE, type SessionChoiceDocument } from "./sessionChoiceData";
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
