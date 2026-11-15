import { expect, it } from "vitest";
import { resolveDropIntent, type ChoiceDragItem } from "./choiceDrop";
import { applyPolicyDrop } from "./choicePolicyDrop";
import { EMPTY_CHOICE, type SessionChoiceDocument } from "./sessionChoiceData";

function fixture(): SessionChoiceDocument {
  return {
    ...structuredClone(EMPTY_CHOICE),
    workTypes: [{ id: "work", label: "Work" }],
    hosts: {
      host: {
        work: [
          ["a", "b", "c"].map((name) => ({
            name,
            links: [{ agent: name }],
            weight: 2,
            adjusted: true,
          })),
          [],
        ],
      },
    },
  };
}
const row = (index: number): ChoiceDragItem => ({
  id: `row-${index}`,
  label: `Row ${index}`,
  data: { kind: "row", tier: 0, row: index },
});

it.each([
  [0, 2, "before", ["b", "a", "c"]],
  [0, 2, "after", ["b", "c", "a"]],
  [2, 0, "before", ["c", "a", "b"]],
  [2, 0, "after", ["a", "c", "b"]],
] as const)(
  "places row %s %s %s using the visible insertion intent",
  (source, target, edge, expected) => {
    const initial = fixture();
    const intent = resolveDropIntent(row(source), row(target), edge);
    const next = applyPolicyDrop(initial, "host", "work", intent);
    expect(next.hosts.host.work[0].map((entry) => entry.name)).toEqual(expected);
    expect(next.hosts.host.work[0].every((entry) => entry.weight === 2 && entry.adjusted)).toBe(
      true,
    );
    expect(initial.hosts.host.work[0].map((entry) => entry.name)).toEqual(["a", "b", "c"]);
  },
);

it("rejects incompatible targets and duplicate ability copies without changing the draft", () => {
  const initial = fixture();
  const target: ChoiceDragItem = {
    id: "work",
    label: "Work",
    data: { kind: "work", work: "work" },
  };
  const invalid = resolveDropIntent(row(0), target, "before");
  expect(invalid.valid).toBe(false);
  expect(applyPolicyDrop(initial, "host", "work", invalid)).toBe(initial);
  const duplicate = resolveDropIntent(
    { id: "preset", label: "Code", data: { kind: "preset", preset: "code" } },
    { id: "level", label: "Level 2", data: { kind: "ability-level", level: 1 } },
    "empty",
    [["code"]],
  );
  expect(duplicate.valid).toBe(false);
  expect(duplicate.message).toMatch(/Already in ability order/);
});

it.each(["before", "after"] as const)("moves to the %s end of a nonempty container", (edge) => {
  const initial = fixture();
  initial.hosts.host.work[1] = [{ name: "d", links: [{ agent: "d" }] }];
  const intent = resolveDropIntent(
    row(0),
    { id: "tier-2", label: "Tier 2", data: { kind: "tier", tier: 1 } },
    edge,
  );
  expect(
    applyPolicyDrop(initial, "host", "work", intent).hosts.host.work[1].map((entry) => entry.name),
  ).toEqual(edge === "before" ? ["a", "d"] : ["d", "a"]);
});
