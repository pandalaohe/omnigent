import { describe, expect, it } from "vitest";

import { formatTokenCountShort } from "./formatCost";

describe("formatTokenCountShort", () => {
  it("uses locale-stable compact suffixes for dense status UI", () => {
    expect(formatTokenCountShort(842)).toBe("842");
    expect(formatTokenCountShort(25_000)).toBe("25k");
    expect(formatTokenCountShort(116_000)).toBe("116k");
    expect(formatTokenCountShort(306_900)).toBe("306.9k");
    expect(formatTokenCountShort(1_530_000)).toBe("1.5m");
  });
});
