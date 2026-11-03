import { describe, expect, it } from "vitest";

import { loadTablerIcons, loadTablerIconTags, tablerIconsIfLoaded } from "@/lib/tablerIconData";

describe("tablerIconData", () => {
  it("loads the full outline set once and caches it", async () => {
    const set = await loadTablerIcons();
    expect(Object.keys(set).length).toBeGreaterThan(5000);
    expect(set.rocket[0][0]).toBe("path");
    expect(typeof set.rocket[0][1].d).toBe("string");
    expect(tablerIconsIfLoaded()).toBe(set);
    expect(await loadTablerIcons()).toBe(set);
  });

  it("loads search tags from the installed package", async () => {
    const tags = await loadTablerIconTags();
    expect(tags.rocket.tags).toContain("space");
  });
});
