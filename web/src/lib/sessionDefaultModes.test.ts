import { describe, expect, it } from "vitest";
import { sessionDefaultModeOptions, sessionModeHandler } from "./sessionDefaultModes";
import {
  callingDefaultHarnesses,
  callingDefaultHasModels,
  callingDefaultParent,
} from "./callingDefaults";
import type { HarnessCapability } from "./agentLabels";

const capabilities: Record<string, HarnessCapability> = {
  custom: {
    calling_defaults: { model_options: false, inherits_from: "parent" },
    session_modes: {
      permission: {
        handler: "custom-review",
        options: [
          { value: "review", label: "Review", concept: "manual", contexts: ["default", "create"] },
          { value: "full", label: "Full", concept: "full-access", contexts: ["create"] },
        ],
      },
      speed: null,
    },
  },
  acp: { calling_defaults: { model_options: true, inherits_from: null }, session_modes: null },
  disabled: { calling_defaults: null, session_modes: null },
};
describe("declared mode contexts and calling defaults", () => {
  it("exposes plugin options by context and its registered transport", () => {
    expect(
      sessionDefaultModeOptions("custom", "permission", [], null, "default", capabilities),
    ).toEqual([{ value: "review", label: "Review" }]);
    expect(
      sessionDefaultModeOptions("custom", "permission", [], null, "create", capabilities).map(
        (x) => x.value,
      ),
    ).toEqual(["review", "full"]);
    expect(sessionModeHandler("custom", "permission", capabilities)).toBe("custom-review");
    expect(
      sessionDefaultModeOptions("disabled", "permission", [], null, "create", capabilities),
    ).toEqual([]);
  });
  it("uses declaration membership, model support, parent and concrete ACP fallback", () => {
    expect(callingDefaultHarnesses(capabilities)).toEqual(["custom", "acp"]);
    expect(callingDefaultHasModels("custom", capabilities)).toBe(false);
    expect(callingDefaultHasModels("acp:example", capabilities)).toBe(true);
    expect(callingDefaultParent("custom", capabilities)).toBe("parent");
  });
});
