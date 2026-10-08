// Assembly of the raw runtime sources shipped over the annotate port: the
// `pick` part carries the snapdom IIFE plus the trailer that captures
// `window.snapdom` before page scripts can overwrite it (design §2.5).

import { describe, expect, it } from "vitest";
import { loadRuntimeSources } from "./runtimeSources";

describe("loadRuntimeSources", () => {
  it("keeps the core part free of the capture engine", async () => {
    const sources = await loadRuntimeSources("core");
    expect(sources).toHaveLength(4);
    expect(sources.every((source) => !source.includes("window.snapdom"))).toBe(true);
  });

  it("assembles freeze, picker, snapdom and crop with the captured reference", async () => {
    const sources = await loadRuntimeSources("pick");
    expect(sources).toHaveLength(4);
    expect(sources[0]).toContain("ns.freeze =");
    expect(sources[1]).toContain("data-omni-pick");
    expect(sources[2]).toContain("window.snapdom=Y");
    expect(sources[2]!.endsWith(";ns.snapdom = window.snapdom;")).toBe(true);
    expect(sources[3]).toContain("ns.capture =");
  });
});
