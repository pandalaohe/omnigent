import { describe, expect, it } from "vitest";
import type { PickerItem } from "@/components/SelectionList";
import { resolveMulti, suggestMode, summarizeMulti } from "./multiSelection";

const projects: PickerItem[] = [
  { id: "alpha", label: "Alpha" },
  { id: "beta", label: "Beta" },
  { id: "gamma", label: "Gamma" },
  { id: "archived", label: "Archived", unavailableReason: "Archived project" },
];

describe("project selection", () => {
  it("applies All except and Only to eligible projects while reporting skipped rows", () => {
    expect(resolveMulti({ mode: "except", ids: ["beta"] }, projects)).toEqual(["alpha", "gamma"]);
    expect(resolveMulti({ mode: "only", ids: ["beta", "archived"] }, projects)).toEqual(["beta"]);
    expect(summarizeMulti({ mode: "except", ids: ["beta"] }, projects)).toMatchObject({
      applies: 2,
      applicable: 3,
      skipped: 1,
      text: "All 3, except 1 · 1 skipped",
    });
  });

  it("offers an equivalent shorter representation", () => {
    expect(suggestMode({ mode: "only", ids: ["alpha", "beta"] }, projects)).toEqual({
      mode: "except",
      ids: ["gamma"],
    });
    expect(suggestMode({ mode: "except", ids: ["alpha", "beta"] }, projects)).toEqual({
      mode: "only",
      ids: ["gamma"],
    });
  });
});
