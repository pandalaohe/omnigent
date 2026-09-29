import { describe, expect, it } from "vitest";

import type { Host } from "@/hooks/useHosts";
import { sandboxOptionLabel } from "./capabilities";
import {
  HOST_COLORS,
  LOCAL_HOST_LABEL,
  hostColor,
  hostDisplayName,
  isHostColorKey,
} from "./hostColors";

describe("hostColors", () => {
  it("offers eight distinct palette keys and hexes", () => {
    expect(HOST_COLORS).toHaveLength(8);
    expect(new Set(HOST_COLORS.map((entry) => entry.key)).size).toBe(8);
    expect(new Set(HOST_COLORS.map((entry) => entry.hex)).size).toBe(8);
    for (const entry of HOST_COLORS) {
      expect(entry.hex).toMatch(/^#[0-9a-f]{6}$/i);
      expect(isHostColorKey(entry.key)).toBe(true);
    }
    expect(isHostColorKey("chartreuse")).toBe(false);
    expect(isHostColorKey(null)).toBe(false);
  });

  it("uses the user's valid pick for a host", () => {
    expect(hostColor("host-1", "TMB", { "host-1": "purple" })).toEqual({
      key: "purple",
      hex: "#8250df",
    });
  });

  it("ignores an unknown pick and falls back to the automatic colour", () => {
    const automatic = hostColor("host-1", "TMB", {});
    expect(hostColor("host-1", "TMB", { "host-1": "chartreuse" as never })).toEqual(automatic);
    expect(HOST_COLORS).toContainEqual(automatic);
  });

  it("derives a stable automatic colour from the host name", () => {
    const first = hostColor("host-1", "TMB", {});
    expect(hostColor("host-1", "TMB", {})).toEqual(first);
    // The name, not the id, owns the hash when both are known.
    expect(hostColor("a-different-id", "TMB", {})).toEqual(first);
  });

  it("falls back to the host id and then the local sentinel", () => {
    const byId = hostColor("host-1", null, {});
    expect(hostColor("host-1", undefined, {})).toEqual(byId);
    expect(HOST_COLORS).toContainEqual(hostColor(null, null, {}));
  });
});

describe("hostDisplayName", () => {
  it("mirrors resolveHostBadge's naming", () => {
    const host = { host_id: "host-1", name: "TMB" } as Host;
    expect(hostDisplayName("host-1", host)).toBe("TMB");
    expect(
      hostDisplayName("host-2", {
        host_id: "host-2",
        name: "sandbox",
        sandbox_provider: "modal",
      } as Host),
    ).toBe(sandboxOptionLabel("modal"));
    // Unresolved record: the raw id, like the badge's fallback.
    expect(hostDisplayName("host-9", undefined)).toBe("host-9");
  });

  it("labels a missing host as the local machine", () => {
    expect(hostDisplayName(null, undefined)).toBe(LOCAL_HOST_LABEL);
    expect(hostDisplayName("local", undefined)).toBe(LOCAL_HOST_LABEL);
  });
});
