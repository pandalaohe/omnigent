import { describe, expect, it } from "vitest";
import type { NativeModelOption } from "./types";
import { defaultSpeedForModel, reconcileSpeed, speedOptionsForModel } from "./speedTiers";

const hostA: NativeModelOption[] = [
  {
    id: "gpt-a",
    isDefault: true,
    defaultServiceTier: "priority",
    serviceTiers: [
      { id: "priority", name: "Fast" },
      { id: "ultrafast", name: "Ultrafast" },
    ],
  },
  { id: "gpt-b", serviceTiers: [{ id: "priority", name: "Quick" }] },
];

describe("Codex speed tiers", () => {
  it("uses the chosen model's advertised names and future IDs", () => {
    expect(speedOptionsForModel(hostA, null)).toEqual([
      { value: "standard", label: "Standard" },
      { value: "fast", label: "Fast" },
      { value: "ultrafast", label: "Ultrafast" },
    ]);
    expect(speedOptionsForModel(hostA, "gpt-b")).toEqual([
      { value: "standard", label: "Standard" },
      { value: "fast", label: "Quick" },
    ]);
    expect(speedOptionsForModel([{ id: "gpt-a", isDefault: true }], null)).toEqual([
      { value: "standard", label: "Standard" },
    ]);
    expect(speedOptionsForModel(hostA, "unlisted-agent-model")).toEqual([
      { value: "standard", label: "Standard" },
    ]);
  });

  it("shows the effective default without making it an explicit override", () => {
    expect(defaultSpeedForModel(hostA, null)).toBe("fast");
    expect(defaultSpeedForModel(hostA, "gpt-b")).toBe("standard");
  });

  it("drops a tier when model or host changes and it is no longer offered", () => {
    expect(reconcileSpeed("ultrafast", hostA, "gpt-b")).toBeNull();
    expect(reconcileSpeed("priority", hostA, "gpt-b")).toBe("fast");
    expect(reconcileSpeed("fast", [{ id: "gpt-a", isDefault: true }], null)).toBeNull();
  });
});
