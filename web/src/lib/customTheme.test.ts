import { afterEach, describe, expect, it } from "vitest";
import {
  applyCustomTheme,
  companionFor,
  createCustomThemeFromPalette,
  customThemeSwatches,
  DEFAULT_CUSTOM_THEME,
  deriveCustomTheme,
  ensureContrast,
  neutralPairFor,
  readCustomTheme,
  writeCustomTheme,
} from "./customTheme";
import { PALETTES } from "./themePalette";
import { setEmbedRoot, setEmbedScopeRoot } from "./host";

const STORAGE_KEY = "omnigent:custom-theme";

function parseHexRgb(hex: string): [number, number, number] {
  return [
    Number.parseInt(hex.slice(1, 3), 16),
    Number.parseInt(hex.slice(3, 5), 16),
    Number.parseInt(hex.slice(5, 7), 16),
  ];
}

function parseColorRgba(value: string): { rgb: [number, number, number]; alpha: number } {
  const hex = /^#([0-9a-f]{6})([0-9a-f]{2})?$/i.exec(value);
  if (hex) {
    return {
      rgb: parseHexRgb(`#${hex[1]}`),
      alpha: hex[2] ? Number.parseInt(hex[2], 16) / 255 : 1,
    };
  }
  const rgba = /^rgba?\(([\d.]+),\s*([\d.]+),\s*([\d.]+)(?:,\s*([\d.]+))?\)$/.exec(value);
  if (rgba) {
    return {
      rgb: [Number(rgba[1]), Number(rgba[2]), Number(rgba[3])],
      alpha: rgba[4] === undefined ? 1 : Number(rgba[4]),
    };
  }
  const spaceRgb = /^rgb\(([\d.]+)\s+([\d.]+)\s+([\d.]+)(?:\s*\/\s*([\d.]+)%?)?\)$/.exec(value);
  if (spaceRgb) {
    const rawAlpha = spaceRgb[4];
    const alpha =
      rawAlpha === undefined ? 1 : value.includes("%") ? Number(rawAlpha) / 100 : Number(rawAlpha);
    return {
      rgb: [Number(spaceRgb[1]), Number(spaceRgb[2]), Number(spaceRgb[3])],
      alpha,
    };
  }
  throw new Error(`unsupported color: ${value}`);
}

function blendOver(
  top: { rgb: [number, number, number]; alpha: number },
  under: [number, number, number],
): [number, number, number] {
  return [
    under[0] + (top.rgb[0] - under[0]) * top.alpha,
    under[1] + (top.rgb[1] - under[1]) * top.alpha,
    under[2] + (top.rgb[2] - under[2]) * top.alpha,
  ];
}

function srgbLum([r, g, b]: [number, number, number]): number {
  const lin = (c: number) => {
    const v = c / 255;
    return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}

function contrastRgb(a: [number, number, number], b: [number, number, number]): number {
  const l1 = srgbLum(a);
  const l2 = srgbLum(b);
  return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
}

function hueDegrees([r, g, b]: [number, number, number]): number {
  const rn = r / 255;
  const gn = g / 255;
  const bn = b / 255;
  const max = Math.max(rn, gn, bn);
  const min = Math.min(rn, gn, bn);
  const d = max - min;
  if (d === 0) return 0;
  const h =
    max === rn
      ? (gn - bn) / d + (gn < bn ? 6 : 0)
      : max === gn
        ? (bn - rn) / d + 2
        : (rn - gn) / d + 4;
  return (h / 6) * 360;
}

function hueDistanceHex(first: string, second: string): number {
  const diff = Math.abs(hueDegrees(parseHexRgb(first)) - hueDegrees(parseHexRgb(second))) % 360;
  return diff > 180 ? 360 - diff : diff;
}

afterEach(() => {
  localStorage.clear();
  setEmbedScopeRoot(null);
  setEmbedRoot(null);
  document.documentElement.removeAttribute("data-custom-translucent-sidebar");
  for (const property of Array.from(document.documentElement.style)) {
    if (property.startsWith("--custom-")) {
      document.documentElement.style.removeProperty(property);
    }
  }
});

describe("customTheme", () => {
  it("returns a safe default when no valid preference is stored", () => {
    expect(readCustomTheme()).toEqual(DEFAULT_CUSTOM_THEME);

    localStorage.setItem(STORAGE_KEY, JSON.stringify({ accent: "red" }));
    expect(readCustomTheme()).toEqual(DEFAULT_CUSTOM_THEME);
  });

  it("round-trips a valid shared custom-theme configuration", () => {
    const theme = {
      basePalette: "github" as const,
      accent: "#1267d6",
      darkAccent: "#238636",
      tint: "#dce8f7",
      darkTint: "#0d1117",
      contrast: 72,
      translucentSidebar: true,
    };

    writeCustomTheme(theme);

    expect(readCustomTheme()).toEqual(theme);
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "null")).toEqual(theme);
  });

  it("restores the dark tint for legacy saved themes", () => {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        basePalette: "github",
        accent: "#1f883d",
        tint: "#f6f8fa",
        contrast: 51,
        translucentSidebar: false,
      }),
    );

    expect(readCustomTheme().darkTint).toBe("#0d1117");
  });

  it("restores the dark accent for legacy saved themes", () => {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        basePalette: "github",
        accent: "#1f883d",
        tint: "#f6f8fa",
        darkTint: "#0d1117",
        contrast: 51,
        translucentSidebar: false,
      }),
    );

    expect(readCustomTheme().darkAccent).toBe("#58a6ff");
  });

  it("creates one editable configuration from a built-in palette", () => {
    const github = PALETTES.find((palette) => palette.id === "github");
    expect(github).toBeDefined();

    expect(createCustomThemeFromPalette(github!)).toEqual({
      basePalette: "github",
      accent: "#0969da",
      darkAccent: "#58a6ff",
      tint: "#f6f8fa",
      darkTint: "#0d1117",
      contrast: 50,
      translucentSidebar: false,
    });
  });

  it.each(PALETTES)("uses the exact $label tokens at contrast 50", (palette) => {
    const theme = createCustomThemeFromPalette(palette);
    const variants = deriveCustomTheme(theme);

    expect(variants.light).toEqual(palette.tokens.light);
    expect(variants.dark).toEqual(palette.tokens.dark);
  });

  it.each(PALETTES)("restores the exact $label tokens after changing contrast", (palette) => {
    const theme = createCustomThemeFromPalette(palette);

    const adjusted = deriveCustomTheme({ ...theme, contrast: 68 });

    expect(adjusted.light).not.toEqual(palette.tokens.light);
    expect(adjusted.dark).not.toEqual(palette.tokens.dark);
    expect(deriveCustomTheme({ ...theme, contrast: 50 })).toEqual(palette.tokens);
  });

  it.each(PALETTES)("tints $label's selection with a custom accent", (palette) => {
    for (const accent of ["#2563eb", "#777777", "#f59e0b"]) {
      const variants = deriveCustomTheme({
        ...createCustomThemeFromPalette(palette),
        accent,
        darkAccent: accent,
        contrast: 100,
        translucentSidebar: true,
      });
      const [r, g, b] = [1, 3, 5].map((offset) =>
        Number.parseInt(accent.slice(offset, offset + 2), 16),
      );

      for (const mode of ["light", "dark"] as const) {
        // The wash keeps the stock alpha for this palette and mode.
        const alpha = /, ([\d.]+)\)$/.exec(palette.tokens[mode].selectionBackground)?.[1] ?? "0.12";
        expect(variants[mode].selectionBackground).toBe(`rgba(${r}, ${g}, ${b}, ${alpha})`);
        expect(variants[mode].selectionForeground).toBe(variants[mode].foreground);
      }
    }
  });

  it("keeps Omnigent's selection tint after contrast changes", () => {
    const theme = createCustomThemeFromPalette(PALETTES[0]);
    const variants = deriveCustomTheme({ ...theme, contrast: 53 });

    expect(variants.light.selectionBackground).toBe("rgba(223, 60, 133, 0.1)");
    expect(variants.light.selectionForeground).toBe("#651249");
    expect(variants.dark.selectionBackground).toBe("rgba(223, 60, 133, 0.15)");
    expect(variants.dark.selectionForeground).toBe("#f9a8d4");
  });

  it("tints the text selection with a custom accent, like the sidebar's active row", () => {
    const theme = createCustomThemeFromPalette(PALETTES[0]);
    const variants = deriveCustomTheme({ ...theme, accent: "#2563eb", darkAccent: "#f59e0b" });

    expect(variants.light.selectionBackground).toBe("rgba(37, 99, 235, 0.1)");
    expect(variants.dark.selectionBackground).toBe("rgba(245, 158, 11, 0.15)");
    expect(variants.light.selectionForeground).toBe(variants.light.foreground);
    expect(variants.dark.selectionForeground).toBe(variants.dark.foreground);
  });

  it("keeps Omnigent's selected-session colors after contrast changes", () => {
    const theme = createCustomThemeFromPalette(PALETTES[0]);
    const variants = deriveCustomTheme({ ...theme, contrast: 53 });

    expect(variants.light.sidebarActive).toContain("var(--primary)");
    expect(variants.light.sidebarActiveForeground).toBe("var(--primary)");
    expect(variants.dark.sidebarActive).toContain("var(--primary)");
    expect(variants.dark.sidebarActiveForeground).toBe("var(--primary)");
  });

  it("tints the sidebar active highlight with a custom accent", () => {
    const theme = createCustomThemeFromPalette(PALETTES[0]);
    const variants = deriveCustomTheme({
      ...theme,
      accent: "#2563eb",
      darkAccent: "#f59e0b",
    });

    // Background tracks the accent at low alpha, in both modes.
    expect(variants.light.sidebarActive).toBe("rgba(37, 99, 235, 0.12)");
    expect(variants.dark.sidebarActive).toBe("rgba(245, 158, 11, 0.12)");

    expect(variants.light.sidebarActiveForeground).toBe("#2563eb");
    expect(variants.dark.sidebarActiveForeground).toBe("#f59e0b");
  });

  it.each(PALETTES)("keeps the exact $label preview at contrast 50", (palette) => {
    const swatches = customThemeSwatches(createCustomThemeFromPalette(palette));

    expect(swatches).toEqual({ light: palette.light, dark: palette.dark });
  });

  it("shows explicitly customized accents in the preview", () => {
    const palette = PALETTES.find((candidate) => candidate.id === "omni")!;
    const theme = createCustomThemeFromPalette(palette);
    const swatches = customThemeSwatches({
      ...theme,
      accent: "#2563eb",
      darkAccent: "#2563eb",
    });

    expect(swatches.light.accent).toBe("#2563eb");
    expect(swatches.dark.accent).toBe("#2563eb");
  });

  it("derives readable light and dark variants from the same configuration", () => {
    const variants = deriveCustomTheme({
      basePalette: "omni",
      accent: "#2563eb",
      darkAccent: "#2563eb",
      tint: "#dbeafe",
      darkTint: "#160e24",
      contrast: 60,
      translucentSidebar: false,
    });

    expect(variants.light.background).not.toBe(PALETTES[0].tokens.light.background);
    expect(variants.dark.background).toBe("#160e24");
    expect(variants.light.primary).toBe("#2563eb");
    expect(variants.dark.primary).toBe("#2563eb");
    expect(variants.light.primaryForeground).toBe("#ffffff");
    expect(variants.dark.primaryForeground).toBe("#ffffff");
    expect(variants.light.foreground).not.toBe(variants.dark.foreground);
    expect(variants.light.shellBackground).toBe(variants.light.background);
    expect(variants.dark.shellBackground).toBe(variants.dark.background);
    expect(variants.light.sidebarBackground).toBe(variants.light.sidebar);
    expect(variants.dark.sidebarBackground).toBe(variants.dark.sidebar);
  });

  it("derives each mode from its own tint", () => {
    const theme = createCustomThemeFromPalette(PALETTES[0]);
    const first = deriveCustomTheme({ ...theme, tint: "#dbeafe", darkTint: "#160e24" });
    const second = deriveCustomTheme({ ...theme, tint: "#fee2e2", darkTint: "#160e24" });

    expect(first.light.foreground).not.toBe(second.light.foreground);
    expect(first.dark.foreground).toBe(second.dark.foreground);
    expect(first.dark.background).toBe("#160e24");
  });

  it("keeps helper text and links readable across custom contrast settings", () => {
    const channel = (hex: string, offset: number) => {
      const value = Number.parseInt(hex.slice(offset, offset + 2), 16) / 255;
      return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
    };
    const luminance = (hex: string) =>
      channel(hex, 1) * 0.2126 + channel(hex, 3) * 0.7152 + channel(hex, 5) * 0.0722;
    const ratio = (first: string, second: string) => {
      const lighter = Math.max(luminance(first), luminance(second));
      const darker = Math.min(luminance(first), luminance(second));
      return (lighter + 0.05) / (darker + 0.05);
    };

    for (const contrast of [0, 50, 100]) {
      const variants = deriveCustomTheme({
        basePalette: "github",
        accent: "#777777",
        darkAccent: "#777777",
        tint: "#ffffff",
        darkTint: "#0d1117",
        contrast,
        translucentSidebar: false,
      });
      for (const surface of [
        variants.light.background,
        variants.light.cardSolid,
        variants.light.muted,
      ]) {
        expect(ratio(variants.light.mutedForeground, surface)).toBeGreaterThanOrEqual(4.5);
        expect(ratio(variants.light.link, surface)).toBeGreaterThanOrEqual(4.5);
      }
      for (const surface of [
        variants.dark.background,
        variants.dark.cardSolid,
        variants.dark.muted,
      ]) {
        expect(ratio(variants.dark.mutedForeground, surface)).toBeGreaterThanOrEqual(4.5);
        expect(ratio(variants.dark.link, surface)).toBeGreaterThanOrEqual(4.5);
      }
    }
  });

  it("applies both mode variants as document-level custom properties", () => {
    applyCustomTheme({
      basePalette: "omni",
      accent: "#2563eb",
      darkAccent: "#2563eb",
      tint: "#dbeafe",
      darkTint: "#160e24",
      contrast: 60,
      translucentSidebar: true,
    });

    const style = document.documentElement.style;
    expect(style.getPropertyValue("--custom-light-background")).not.toBe("");
    expect(style.getPropertyValue("--custom-dark-background")).toBe("#160e24");
    expect(style.getPropertyValue("--custom-light-sidebar")).toMatch(/^rgba\(/);
    expect(style.getPropertyValue("--custom-dark-sidebar")).toMatch(/^rgba\(/);
    expect(document.documentElement).toHaveAttribute("data-custom-translucent-sidebar");
  });

  it("targets the embed roots when embedded (vars on scope root, attr on both)", () => {
    // Embedded, the `--custom-*` vars go on the scope root (inherited by the
    // inner `.dark` root); the translucent attribute goes on BOTH roots so the
    // light `:root[...]` and dark `.dark[...]` selectors each match.
    const scope = document.createElement("div");
    const inner = document.createElement("div");
    setEmbedScopeRoot(scope);
    setEmbedRoot(inner);
    applyCustomTheme({
      basePalette: "omni",
      accent: "#2563eb",
      darkAccent: "#2563eb",
      tint: "#dbeafe",
      darkTint: "#160e24",
      contrast: 60,
      translucentSidebar: true,
    });
    expect(scope.style.getPropertyValue("--custom-dark-background")).toBe("#160e24");
    expect(scope).toHaveAttribute("data-custom-translucent-sidebar");
    expect(inner).toHaveAttribute("data-custom-translucent-sidebar");
    expect(document.documentElement.style.getPropertyValue("--custom-dark-background")).toBe("");
    expect(document.documentElement).not.toHaveAttribute("data-custom-translucent-sidebar");
  });

  it("rotates companion hue by 150 degrees when a custom accent is within 35 degrees of the base companion", () => {
    const omni = PALETTES.find((palette) => palette.id === "omni")!;
    const theme = {
      ...createCustomThemeFromPalette(omni),
      accent: "#14b8a6",
      darkAccent: "#14b8a6",
    };
    const variants = deriveCustomTheme(theme);

    expect(variants.light.companion).not.toBe(omni.tokens.light.companion);
    expect(hueDistanceHex(variants.light.companion, "#14b8a6")).toBeGreaterThanOrEqual(35);
    expect(hueDistanceHex(variants.dark.companion, "#14b8a6")).toBeGreaterThanOrEqual(35);

    for (const mode of ["light", "dark"] as const) {
      const tokens = variants[mode];
      const bgRgb = parseHexRgb(tokens.background);
      for (const surface of [tokens.background, tokens.cardSolid, tokens.popover, tokens.sidebar]) {
        const sRgb = blendOver(parseColorRgba(surface), bgRgb);
        expect(contrastRgb(parseHexRgb(tokens.companion), sRgb)).toBeGreaterThanOrEqual(3);
      }
    }
  });

  it("keeps the base companion when a custom accent is far from the base companion hue", () => {
    const omni = PALETTES.find((palette) => palette.id === "omni")!;
    const variants = deriveCustomTheme({
      ...createCustomThemeFromPalette(omni),
      accent: "#7c3aed",
      darkAccent: "#7c3aed",
    });

    expect(variants.light.companion).toBe("#0d9488");
    expect(variants.dark.companion).toBe("#2dd4bf");
  });

  it("adjusts companion and status colours when a custom tint lowers contrast", () => {
    const omni = PALETTES.find((palette) => palette.id === "omni")!;
    // Dark tint near #2dd4bf lightness, and light tint near #0d9488 lightness.
    const variants = deriveCustomTheme({
      ...createCustomThemeFromPalette(omni),
      tint: "#99f6e4",
      darkTint: "#134e4a",
      contrast: 50,
    });

    for (const mode of ["light", "dark"] as const) {
      const tokens = variants[mode];
      const bgRgb = parseHexRgb(tokens.background);
      const surfaces = [tokens.background, tokens.cardSolid, tokens.popover, tokens.sidebar];
      for (const surface of surfaces) {
        const sRgb = blendOver(parseColorRgba(surface), bgRgb);
        expect(contrastRgb(parseHexRgb(tokens.companion), sRgb)).toBeGreaterThanOrEqual(3);

        for (const [role, hex, alpha] of [
          ["success", tokens.success, 0.15],
          ["warning", tokens.warning, 0.15],
          ["destructive", tokens.destructive, 0.1],
          ["keepCold", tokens.keepCold, 0.1],
        ] as const) {
          const cRgb = parseHexRgb(hex);
          expect(contrastRgb(cRgb, sRgb), `${mode} ${role} on ${surface}`).toBeGreaterThanOrEqual(
            4.5,
          );
          const tinted = blendOver({ rgb: cRgb, alpha }, sRgb);
          expect(
            contrastRgb(cRgb, tinted),
            `${mode} ${role} on ${alpha} tint over ${surface}`,
          ).toBeGreaterThanOrEqual(4.5);
        }
      }
    }

    // Direct helper verification when contrast is forced below floor.
    const adjustedCompanion = companionFor("#0d9488", "#df3c85", ["#1a524c"], "#ffffff");
    expect(
      contrastRgb(parseHexRgb(adjustedCompanion), parseHexRgb("#1a524c")),
    ).toBeGreaterThanOrEqual(3);
    const adjustedStatus = ensureContrast("#247345", ["#173d27"], "#ffffff", 4.5, 0.15);
    expect(contrastRgb(parseHexRgb(adjustedStatus), parseHexRgb("#173d27"))).toBeGreaterThanOrEqual(
      4.5,
    );

    // Mid-grey surfaces (#777777 / #827f88 / #7b7a7d) where toward=#ffffff fails 4.5:1 on #827f88
    // and ensureContrast must flip toward #000000 to clear 4.5:1 across all surfaces.
    const midGreySurfaces = ["#777777", "#827f88", "#7b7a7d"];
    const midGreyStatus = ensureContrast("#43c367", midGreySurfaces, "#ffffff", 4.5, 0.15);
    const midGreyComp = companionFor("#2dd4bf", "#bd93f9", midGreySurfaces, "#ffffff");
    for (const surface of midGreySurfaces) {
      const sRgb = parseHexRgb(surface);
      expect(contrastRgb(parseHexRgb(midGreyComp), sRgb)).toBeGreaterThanOrEqual(3);
      expect(contrastRgb(parseHexRgb(midGreyStatus), sRgb)).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("loads a legacy stored custom theme and derives all element and CTA tokens", () => {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        basePalette: "dracula",
        accent: "#bd93f9",
        tint: "#f7f5ff",
        contrast: 55,
        translucentSidebar: false,
      }),
    );

    const stored = readCustomTheme();
    const variants = deriveCustomTheme(stored);
    const swatches = customThemeSwatches(stored);

    for (const mode of ["light", "dark"] as const) {
      expect(variants[mode].companion).toMatch(/^#[0-9a-f]{6}$/);
      expect(variants[mode].success).toMatch(/^#[0-9a-f]{6}$/);
      expect(variants[mode].warning).toMatch(/^#[0-9a-f]{6}$/);
      expect(variants[mode].destructive).toMatch(/^#[0-9a-f]{6}$/);
      expect(variants[mode].keepCold).toMatch(/^#[0-9a-f]{6}$/);
      expect(variants[mode].primaryCta).toMatch(/^#[0-9a-f]{6}$/);
      expect(variants[mode].primaryCtaForeground).toMatch(/^#[0-9a-f]{6}$/);
      expect(swatches[mode].companion).toBe(variants[mode].companion);
    }
  });

  it("keeps primaryCta and primaryCtaForeground readable across built-in palettes and custom tints (including mid-greys and #00873c)", () => {
    for (const palette of PALETTES) {
      expect(palette.tokens.light.primaryCta).toBe("#11171c");
      expect(palette.tokens.light.primaryCtaForeground).toBe("#ffffff");
      expect(palette.tokens.dark.primaryCta).toBe("#e8ecf0");
      expect(palette.tokens.dark.primaryCtaForeground).toBe("#11171c");

      for (const mode of ["light", "dark"] as const) {
        const tokens = palette.tokens[mode];
        const bgRgb = parseColorRgba(tokens.background).rgb;
        const cardRgb = blendOver(parseColorRgba(tokens.card), bgRgb);
        const trackRgb = blendOver(parseColorRgba(tokens.muted), cardRgb);
        const ctaRgb = parseHexRgb(tokens.primaryCta);
        const ctaFgRgb = parseHexRgb(tokens.primaryCtaForeground);

        expect(
          contrastRgb(ctaRgb, trackRgb),
          `${palette.id} ${mode} primaryCta vs track`,
        ).toBeGreaterThanOrEqual(3);
        expect(
          contrastRgb(ctaFgRgb, ctaRgb),
          `${palette.id} ${mode} primaryCtaForeground vs primaryCta`,
        ).toBeGreaterThanOrEqual(4.5);
      }
    }

    // Verify the 18-step RGB grid + explicit edge tints (#00873c, mid-greys, pale dark tints)
    const steps = Array.from({ length: 18 }, (_, idx) => Math.round((idx * 255) / 17));
    for (const r of steps) {
      for (const g of [0, 90, 135, 180, 255]) {
        for (const b of [0, 90, 135, 180, 255]) {
          const trackHex = `#${[r, g, b].map((c) => c.toString(16).padStart(2, "0")).join("")}`;
          const { primaryCta, primaryCtaForeground } = neutralPairFor(trackHex);
          expect(contrastRgb(parseHexRgb(primaryCta), [r, g, b])).toBeGreaterThanOrEqual(3.9);
          expect(
            contrastRgb(parseHexRgb(primaryCtaForeground), parseHexRgb(primaryCta)),
          ).toBeGreaterThanOrEqual(4.5);
        }
      }
    }

    const customTints = ["#00873c", "#737373", "#777777", "#808080", "#d8dee9", "#2e3440"];
    for (const tint of customTints) {
      const variants = deriveCustomTheme({
        basePalette: "omni",
        accent: "#df3c85",
        darkAccent: "#df3c85",
        tint,
        darkTint: tint,
        contrast: 50,
        translucentSidebar: false,
      });
      for (const mode of ["light", "dark"] as const) {
        const tokens = variants[mode];
        const bgRgb = parseColorRgba(tokens.background).rgb;
        const cardRgb = blendOver(parseColorRgba(tokens.card), bgRgb);
        const trackRgb = blendOver(parseColorRgba(tokens.muted), cardRgb);
        const ctaRgb = parseHexRgb(tokens.primaryCta);
        const ctaFgRgb = parseHexRgb(tokens.primaryCtaForeground);
        expect(
          contrastRgb(ctaRgb, trackRgb),
          `custom tint ${tint} ${mode} cta vs track`,
        ).toBeGreaterThanOrEqual(3.9);
        expect(
          contrastRgb(ctaFgRgb, ctaRgb),
          `custom tint ${tint} ${mode} fg vs cta`,
        ).toBeGreaterThanOrEqual(4.5);
      }
    }
  });
});
