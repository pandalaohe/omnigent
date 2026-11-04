import { describe, expect, it } from "vitest";
import type { Host } from "@/hooks/useHosts";
import { usageBoardData } from "@/pages/usageBoard/usageBoardFixtures";
import { EMPTY_CHOICE, type SessionChoiceDocument } from "./sessionChoiceData";
import {
  addToOtherTypes,
  applyPreset,
  copyStrategy,
  removePreset,
  removeWorkType,
  renameWorkType,
  starter,
  weightOf,
  type ChoiceAgent,
} from "./sessionChoiceEdits";

const agents: ChoiceAgent[] = [
  {
    id: "native",
    name: "native",
    display_name: "Native",
    harness: "codex-native",
    product: "codex",
  },
  { id: "sdk", name: "sdk", display_name: "SDK", harness: "codex", product: "codex" },
  { id: "other", name: "other", display_name: "Other", harness: "pi-native", product: "pi" },
];
const target = {
  host_id: "b",
  name: "Laptop",
  configured_harnesses: { "codex-native": false, codex: true, "pi-native": false },
} as unknown as Host;
function document(): SessionChoiceDocument {
  return {
    ...structuredClone(EMPTY_CHOICE),
    enabled: true,
    defaultWorkType: "work",
    workTypes: [
      { id: "work", label: "Work" },
      { id: "review", label: "Review" },
    ],
    presets: [{ id: "code", name: "Code", links: [{ agent: "native", model: "new-model" }] }],
    hosts: {
      a: {
        work: [
          [
            { preset: "code", links: [{ agent: "native", model: "old-model" }] },
            { links: [{ agent: "other" }] },
          ],
        ],
      },
    },
    limits: { pool: { reserveExempt: ["work"] } },
    abilityOrder: [["code"]],
  };
}

describe("policy edits preserve working copies", () => {
  it("renames ids, aliases, defaults, every host and reserve exemptions", () => {
    const before = document();
    const next = renameWorkType(before, "work", "Big Design");
    expect(next.workTypes[0]).toMatchObject({ id: "big-design", aliases: ["work"] });
    expect(next.defaultWorkType).toBe("big-design");
    expect(next.hosts.a["big-design"]).toEqual(before.hosts.a.work);
    expect(next.hosts.a.work).toBeUndefined();
    expect(next.limits.pool.reserveExempt).toEqual(["big-design"]);
    expect(before.workTypes[0].id).toBe("work");
    expect(() => renameWorkType(next, "review", "work")).toThrow("already in use");
  });
  it("protects the default, and deleting the last type turns choice off", () => {
    expect(() => removeWorkType(document(), "work")).toThrow("default");
    const one = removeWorkType(document(), "review");
    expect(removeWorkType(one, "work")).toMatchObject({
      enabled: false,
      workTypes: [],
      hosts: {},
      defaultWorkType: null,
    });
  });
  it("applies to unadjusted rows, retaining weights and hand-edited links", () => {
    const before = document();
    before.hosts.a.work[0].push({ preset: "code", adjusted: true, links: [{ agent: "sdk" }] });
    before.hosts.a.work[0][0].weight = 4;
    const result = applyPreset(before, "code");
    expect(result).toMatchObject({ updated: 1, adjusted: 1 });
    expect(result.document.hosts.a.work[0][0]).toMatchObject({
      weight: 4,
      links: before.presets[0].links,
      name: "Code",
    });
    expect(result.document.hosts.a.work[0][2].links).toEqual([{ agent: "sdk" }]);
    const removed = removePreset(result.document, "code");
    expect(removed.abilityOrder).toEqual([]);
    expect(removed.hosts.a.work[0][0].adjusted).toBe(true);
    expect(removed.hosts.a.work[0][0].links).toEqual(before.presets[0].links);
  });
  it("swaps native to SDK, drops unavailable links, and keeps uncached models", () => {
    const before = document();
    const copied = copyStrategy(before, "a", "*", target, "work", agents, {
      sdk: ["cached-model"],
    });
    expect(copied.document.hosts.b.work[0]).toEqual([
      { preset: "code", adjusted: true, links: [{ agent: "sdk", model: "old-model" }] },
    ]);
    expect(copied.changes).toEqual(
      expect.arrayContaining([
        "Native → SDK",
        "Dropped Other",
        "old-model: not in Laptop's cached list; kept",
      ]),
    );
    expect(applyPreset(copied.document, "code").document.hosts.b.work[0][0].links[0].agent).toBe(
      "sdk",
    );
    expect(before.hosts.b).toBeUndefined();
    expect(
      copyStrategy(before, "a", "work", { ...target, host_id: "a" }, "review", agents).document
        .hosts.a.review,
    ).toEqual(before.hosts.a.work);
  });
  it("also adds only missing presets and leaves weights implicit", () => {
    const next = addToOtherTypes(document(), "a", "code", 1);
    expect(next.hosts.a.work).toHaveLength(1);
    expect(next.hosts.a.review[1][0]).toMatchObject({ preset: "code" });
    expect(next.hosts.a.review[1][0].weight).toBeUndefined();
    expect([0, 1, 2, 3].map((i) => weightOf({ links: [] }, i, 4))).toEqual([4, 3, 2, 1]);
    expect(weightOf({ links: [], weight: 7 }, 0, 4)).toBe(7);
  });
  it("builds starters from the selected host's ready agents, with two definitions", () => {
    const next = starter(target, agents, usageBoardData().accounts, false);
    expect(next.presets.map((p) => p.links[0])).toEqual([{ agent: "sdk", meter: "codex" }]);
    expect(next.workTypes.filter((w) => w.definition)).toHaveLength(2);
    expect(next.enabled).toBe(false);
    expect(starter(target, agents, [], true).workTypes).toEqual([{ id: "work", label: "Work" }]);
  });
});
