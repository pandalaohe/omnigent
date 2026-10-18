import { expect, it } from "vitest";
import { supportsEffortControl } from "./sessionCapabilities";

it("gates ACP effort on the live session advertisement", () => {
  const session = {
    harness: "acp:neutral",
    codexModelOptions: [{ id: "model-a", isDefault: true, efforts: ["deliberate"] }],
  };
  expect(supportsEffortControl({ harness: "acp:neutral" })).toBe(false);
  expect(supportsEffortControl(session)).toBe(true);
  expect(supportsEffortControl(session, [{ id: "model-b", isDefault: true, efforts: [] }])).toBe(
    false,
  );
});

it("uses the active ACP model without changing the reset default", () => {
  const session = { harness: "acp:neutral" };
  const rows = [
    { id: "a", isDefault: true, isActive: false, efforts: ["quick"] },
    { id: "b", isDefault: false, isActive: true, efforts: [] },
  ];
  expect(supportsEffortControl(session, rows)).toBe(false);
  rows[1].efforts = ["deliberate"];
  expect(supportsEffortControl(session, rows)).toBe(true);
});
