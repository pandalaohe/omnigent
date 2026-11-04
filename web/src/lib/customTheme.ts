import {
  isThemePalette,
  PALETTE_TOKEN_CSS_NAMES,
  PALETTES,
  type PaletteMeta,
  type PaletteSwatch,
  type PaletteTokens,
  type ThemePalette,
} from "./themePalette";
import { getStyleRoot, getThemeRoots } from "./host";

const STORAGE_KEY = "omnigent:custom-theme";
const HEX_COLOR = /^#[0-9a-f]{6}$/i;

export interface CustomTheme {
  basePalette: ThemePalette;
  accent: string;
  darkAccent: string;
  tint: string;
  darkTint: string;
  contrast: number;
  translucentSidebar: boolean;
}

export const DEFAULT_CUSTOM_THEME: CustomTheme = {
  basePalette: "omni",
  accent: "#df3c85",
  darkAccent: "#df3c85",
  tint: "#ffffff",
  darkTint: "#0e0b19",
  contrast: 50,
  translucentSidebar: false,
};

interface Rgb {
  r: number;
  g: number;
  b: number;
}

export type DerivedThemeVariant = PaletteTokens;

export interface DerivedCustomTheme {
  light: DerivedThemeVariant;
  dark: DerivedThemeVariant;
}

export function isHexColor(value: unknown): value is string {
  return typeof value === "string" && HEX_COLOR.test(value);
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function normalizeTheme(value: unknown): CustomTheme | null {
  if (!value || typeof value !== "object") return null;
  const candidate = value as Partial<CustomTheme>;
  if (
    !isThemePalette(candidate.basePalette) ||
    !isHexColor(candidate.accent) ||
    !isHexColor(candidate.tint) ||
    typeof candidate.contrast !== "number" ||
    !Number.isFinite(candidate.contrast) ||
    typeof candidate.translucentSidebar !== "boolean"
  ) {
    return null;
  }
  return {
    basePalette: candidate.basePalette,
    accent: candidate.accent.toLowerCase(),
    darkAccent: isHexColor(candidate.darkAccent)
      ? candidate.darkAccent.toLowerCase()
      : (PALETTES.find((palette) => palette.id === candidate.basePalette)?.tokens.dark.primary ??
        DEFAULT_CUSTOM_THEME.darkAccent),
    tint: candidate.tint.toLowerCase(),
    darkTint: isHexColor(candidate.darkTint)
      ? candidate.darkTint.toLowerCase()
      : (PALETTES.find((palette) => palette.id === candidate.basePalette)?.tokens.dark.background ??
        DEFAULT_CUSTOM_THEME.darkTint),
    contrast: Math.round(clamp(candidate.contrast, 0, 100)),
    translucentSidebar: candidate.translucentSidebar,
  };
}

export function readCustomTheme(): CustomTheme {
  if (typeof window === "undefined") return DEFAULT_CUSTOM_THEME;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return DEFAULT_CUSTOM_THEME;
    return normalizeTheme(JSON.parse(raw)) ?? DEFAULT_CUSTOM_THEME;
  } catch {
    return DEFAULT_CUSTOM_THEME;
  }
}

export function writeCustomTheme(theme: CustomTheme): void {
  if (typeof window === "undefined") return;
  const normalized = normalizeTheme(theme);
  if (!normalized) return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(normalized));
  } catch {
    // A failed preference write should not interrupt live theme updates.
  }
}

export function createCustomThemeFromPalette(palette: PaletteMeta): CustomTheme {
  return {
    basePalette: palette.id,
    accent: palette.tokens.light.primary.toLowerCase(),
    darkAccent: palette.tokens.dark.primary.toLowerCase(),
    tint: palette.tokens.light.background.toLowerCase(),
    darkTint: palette.tokens.dark.background.toLowerCase(),
    contrast: 50,
    translucentSidebar: false,
  };
}

function hexToRgb(hex: string): Rgb {
  return {
    r: Number.parseInt(hex.slice(1, 3), 16),
    g: Number.parseInt(hex.slice(3, 5), 16),
    b: Number.parseInt(hex.slice(5, 7), 16),
  };
}

function rgbToHex({ r, g, b }: Rgb): string {
  const channel = (value: number) =>
    Math.round(clamp(value, 0, 255))
      .toString(16)
      .padStart(2, "0");
  return `#${channel(r)}${channel(g)}${channel(b)}`;
}

function mix(first: string, second: string, secondWeight: number): string {
  const firstRgb = hexToRgb(first);
  const secondRgb = hexToRgb(second);
  const weight = clamp(secondWeight, 0, 1);
  return rgbToHex({
    r: firstRgb.r * (1 - weight) + secondRgb.r * weight,
    g: firstRgb.g * (1 - weight) + secondRgb.g * weight,
    b: firstRgb.b * (1 - weight) + secondRgb.b * weight,
  });
}

function rgbLuminance(color: Rgb): number {
  const linear = (channel: number) => {
    const value = channel / 255;
    return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
  };
  return linear(color.r) * 0.2126 + linear(color.g) * 0.7152 + linear(color.b) * 0.0722;
}

function luminance(hex: string): number {
  return rgbLuminance(hexToRgb(hex));
}

function rgbContrastRatio(first: Rgb, second: Rgb): number {
  const firstLuminance = rgbLuminance(first);
  const secondLuminance = rgbLuminance(second);
  const lighter = Math.max(firstLuminance, secondLuminance);
  const darker = Math.min(firstLuminance, secondLuminance);
  return (lighter + 0.05) / (darker + 0.05);
}

function contrastRatio(first: string, second: string): number {
  return rgbContrastRatio(hexToRgb(first), hexToRgb(second));
}

export function ensureContrast(
  color: string,
  backgrounds: string[],
  toward: string,
  floor = 4.5,
  ownTintAlpha = 0,
): string {
  const bgRgbs = backgrounds.map(hexToRgb);
  const minRatio = (candidate: string, alpha: number): number => {
    const candidateRgb = hexToRgb(candidate);
    let lowest = Number.POSITIVE_INFINITY;
    for (const bgRgb of bgRgbs) {
      lowest = Math.min(lowest, rgbContrastRatio(candidateRgb, bgRgb));
      if (alpha > 0) {
        const tinted: Rgb = {
          r: bgRgb.r * (1 - alpha) + candidateRgb.r * alpha,
          g: bgRgb.g * (1 - alpha) + candidateRgb.g * alpha,
          b: bgRgb.b * (1 - alpha) + candidateRgb.b * alpha,
        };
        lowest = Math.min(lowest, rgbContrastRatio(candidateRgb, tinted));
      }
    }
    return lowest;
  };
  const passes = (candidate: string) => minRatio(candidate, ownTintAlpha) >= floor;
  if (passes(color)) return color;

  const search = (target: string, check: (candidate: string) => boolean): string | null => {
    if (!check(target)) return null;
    let lower = 0;
    let upper = 1;
    let result = target;
    for (let index = 0; index < 14; index += 1) {
      const weight = (lower + upper) / 2;
      const candidate = mix(color, target, weight);
      if (check(candidate)) {
        result = candidate;
        upper = weight;
      } else {
        lower = weight;
      }
    }
    return check(result) ? result : target;
  };

  const primaryPole = luminance(toward) >= 0.5 ? "#ffffff" : "#000000";
  const oppositePole = primaryPole === "#ffffff" ? "#000000" : "#ffffff";

  for (const target of [toward, primaryPole, oppositePole]) {
    const hit = search(target, passes);
    if (hit !== null) return hit;
  }

  if (ownTintAlpha > 0) {
    for (const target of [toward, primaryPole, oppositePole]) {
      const plainHit = search(target, (c) => minRatio(c, 0) >= floor);
      if (plainHit !== null) return plainHit;
    }
  }

  const bestPole =
    minRatio(primaryPole, 0) >= minRatio(oppositePole, 0) ? primaryPole : oppositePole;
  return minRatio(toward, 0) >= minRatio(bestPole, 0) ? toward : bestPole;
}

function hexToHsl(hex: string): [number, number, number] {
  const { r: r255, g: g255, b: b255 } = hexToRgb(hex);
  const r = r255 / 255;
  const g = g255 / 255;
  const b = b255 / 255;
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const delta = max - min;
  const l = (max + min) / 2;
  let h = 0;
  if (delta !== 0) {
    if (max === r) h = ((g - b) / delta + 6) % 6;
    else if (max === g) h = (b - r) / delta + 2;
    else h = (r - g) / delta + 4;
  }
  const s = delta === 0 ? 0 : delta / (1 - Math.abs(2 * l - 1));
  return [h * 60, s, l];
}

function hslToHex(h: number, s: number, l: number): string {
  const hue = ((h % 360) + 360) % 360;
  const c = (1 - Math.abs(2 * l - 1)) * s;
  const x = c * (1 - Math.abs(((hue / 60) % 2) - 1));
  const m = l - c / 2;
  const [r1, g1, b1] =
    hue < 60
      ? [c, x, 0]
      : hue < 120
        ? [x, c, 0]
        : hue < 180
          ? [0, c, x]
          : hue < 240
            ? [0, x, c]
            : hue < 300
              ? [x, 0, c]
              : [c, 0, x];
  return rgbToHex({
    r: (r1 + m) * 255,
    g: (g1 + m) * 255,
    b: (b1 + m) * 255,
  });
}

/** Base companion, turned 150° when within 35° of the accent's hue, then held to the floor. */
export function companionFor(
  baseCompanion: string,
  accent: string,
  surfaces: string[],
  toward: string,
): string {
  const [h, s, l] = hexToHsl(baseCompanion);
  const [accentHue] = hexToHsl(accent);
  const diff = Math.abs(h - accentHue) % 360;
  const distance = Math.min(diff, 360 - diff);
  const candidate = distance < 35 ? hslToHex((h + 150) % 360, s, l) : baseCompanion;
  return ensureContrast(candidate, surfaces, toward, 3);
}

function readableForeground(background: string): "#111318" | "#ffffff" {
  const value = luminance(background);
  const darkContrast = (value + 0.05) / (luminance("#111318") + 0.05);
  const lightContrast = (luminance("#ffffff") + 0.05) / (value + 0.05);
  return darkContrast >= lightContrast ? "#111318" : "#ffffff";
}

type GeneratedThemeVariant = Pick<
  PaletteTokens,
  | "background"
  | "foreground"
  | "card"
  | "cardSolid"
  | "primary"
  | "primaryForeground"
  | "secondary"
  | "muted"
  | "mutedForeground"
  | "codeBackground"
  | "accent"
  | "border"
  | "borderStrong"
  | "sidebar"
>;

interface GeneratedCustomTheme {
  light: GeneratedThemeVariant;
  dark: GeneratedThemeVariant;
}

interface CssColor extends Rgb {
  alpha: number;
}

function parseCssColor(value: string): CssColor | null {
  const hex = /^#([0-9a-f]{6})([0-9a-f]{2})?$/i.exec(value);
  if (hex) {
    return {
      ...hexToRgb(`#${hex[1]}`),
      alpha: hex[2] ? Number.parseInt(hex[2], 16) / 255 : 1,
    };
  }
  const rgb = /^rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)(?:\s*,\s*([\d.]+))?\s*\)$/i.exec(
    value,
  );
  if (!rgb) return null;
  return {
    r: Number(rgb[1]),
    g: Number(rgb[2]),
    b: Number(rgb[3]),
    alpha: rgb[4] ? Number(rgb[4]) : 1,
  };
}

function formatCssColor(color: CssColor, template: string): string {
  const rgb = rgbToHex(color);
  if (template.startsWith("#") && color.alpha === 1) return rgb;
  if (template.length === 9) {
    const alpha = Math.round(clamp(color.alpha, 0, 1) * 255)
      .toString(16)
      .padStart(2, "0");
    return `${rgb}${alpha}`;
  }
  return `rgba(${Math.round(clamp(color.r, 0, 255))}, ${Math.round(clamp(color.g, 0, 255))}, ${Math.round(clamp(color.b, 0, 255))}, ${Math.round(clamp(color.alpha, 0, 1) * 1000) / 1000})`;
}

function rebaseColor(base: string, reference: string, current: string): string {
  if (current === reference) return base;
  const baseColor = parseCssColor(base);
  const referenceColor = parseCssColor(reference);
  const currentColor = parseCssColor(current);
  if (!baseColor || !referenceColor || !currentColor) return base;
  return formatCssColor(
    {
      r: baseColor.r + currentColor.r - referenceColor.r,
      g: baseColor.g + currentColor.g - referenceColor.g,
      b: baseColor.b + currentColor.b - referenceColor.b,
      alpha: baseColor.alpha,
    },
    base,
  );
}

function setAlpha(color: string, alpha: number): string {
  const parsed = parseCssColor(color);
  return parsed ? formatCssColor({ ...parsed, alpha }, "rgba") : color;
}

function parseSurfaceColor(value: string): CssColor | null {
  const parsed = parseCssColor(value);
  if (parsed) return parsed;
  const spaceRgb = /^rgba?\(\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)(?:\s*\/\s*([\d.]+)(%)?)?\s*\)$/i.exec(
    value,
  );
  if (!spaceRgb) return null;
  const rawAlpha = spaceRgb[4] ? Number(spaceRgb[4]) : 1;
  return {
    r: Number(spaceRgb[1]),
    g: Number(spaceRgb[2]),
    b: Number(spaceRgb[3]),
    alpha: spaceRgb[5] === "%" ? rawAlpha / 100 : rawAlpha,
  };
}

function flattenSurface(surface: string, background: string): string {
  const fg = parseSurfaceColor(surface);
  const bg = parseSurfaceColor(background);
  if (!fg) return isHexColor(background) ? background : "#ffffff";
  if (fg.alpha >= 1 || !bg) return rgbToHex(fg);
  return rgbToHex({
    r: fg.r * fg.alpha + bg.r * (1 - fg.alpha),
    g: fg.g * fg.alpha + bg.g * (1 - fg.alpha),
    b: fg.b * fg.alpha + bg.b * (1 - fg.alpha),
  });
}

const LIGHT_PRIMARY_CTA = { primaryCta: "#11171c", primaryCtaForeground: "#ffffff" } as const;
const DARK_PRIMARY_CTA = { primaryCta: "#e8ecf0", primaryCtaForeground: "#11171c" } as const;

/** The neutral pair contrasting more with the flattened track; at least 3.9:1 against any opaque colour. */
export function neutralPairFor(track: string): {
  primaryCta: string;
  primaryCtaForeground: string;
} {
  const flattened = flattenSurface(track, "#ffffff");
  return contrastRatio(LIGHT_PRIMARY_CTA.primaryCta, flattened) >=
    contrastRatio(DARK_PRIMARY_CTA.primaryCta, flattened)
    ? { ...LIGHT_PRIMARY_CTA }
    : { ...DARK_PRIMARY_CTA };
}

function generateCustomTheme(theme: CustomTheme): GeneratedCustomTheme {
  const normalized = normalizeTheme(theme) ?? DEFAULT_CUSTOM_THEME;
  const contrast = normalized.contrast / 100;
  const lightBackground = mix(normalized.tint, "#fffdff", 0.79 - contrast * 0.15);
  const darkBackground = normalized.darkTint;

  const lightCard = mix(lightBackground, "#ffffff", 0.72 + contrast * 0.16);
  const darkCard = mix(darkBackground, "#ffffff", 0.05 + contrast * 0.08);
  const lightForeground = mix(normalized.tint, "#111318", 0.91 + contrast * 0.05);
  const darkForeground = mix(normalized.darkTint, "#ffffff", 0.86 + contrast * 0.08);
  const lightBorder = mix(lightBackground, lightForeground, 0.08 + contrast * 0.08);
  const darkBorder = mix(darkBackground, darkForeground, 0.1 + contrast * 0.08);
  const lightMuted = mix(lightBackground, lightForeground, 0.04 + contrast * 0.05);
  const darkMuted = mix(darkBackground, darkForeground, 0.07 + contrast * 0.06);
  const lightSidebar = mix(lightBackground, lightCard, 0.42);
  const darkSidebar = mix(darkBackground, darkCard, 0.34);
  const lightMutedForeground = ensureContrast(
    mix(lightForeground, lightBackground, 0.45 - contrast * 0.12),
    [lightBackground, lightCard, lightMuted],
    lightForeground,
  );
  const darkMutedForeground = ensureContrast(
    mix(darkForeground, darkBackground, 0.34 - contrast * 0.08),
    [darkBackground, darkCard, darkMuted],
    darkForeground,
  );

  return {
    light: {
      background: lightBackground,
      foreground: lightForeground,
      card: lightCard,
      cardSolid: lightCard,
      primary: normalized.accent,
      primaryForeground: readableForeground(normalized.accent),
      secondary: lightMuted,
      muted: lightMuted,
      mutedForeground: lightMutedForeground,
      codeBackground: mix(lightBackground, lightForeground, 0.05 + contrast * 0.05),
      accent: mix(lightBackground, normalized.accent, 0.09 + contrast * 0.06),
      border: lightBorder,
      borderStrong: mix(lightBackground, lightForeground, 0.18 + contrast * 0.12),
      sidebar: lightSidebar,
    },
    dark: {
      background: darkBackground,
      foreground: darkForeground,
      card: darkCard,
      cardSolid: darkCard,
      primary: normalized.darkAccent,
      primaryForeground: readableForeground(normalized.darkAccent),
      secondary: darkMuted,
      muted: darkMuted,
      mutedForeground: darkMutedForeground,
      codeBackground: mix(darkBackground, "#000000", 0.16 + contrast * 0.08),
      accent: mix(darkBackground, normalized.accent, 0.14 + contrast * 0.08),
      border: darkBorder,
      borderStrong: mix(darkBackground, darkForeground, 0.2 + contrast * 0.12),
      sidebar: darkSidebar,
    },
  };
}

/**
 * A custom accent tints the selection the way it tints the sidebar's active
 * row: a wash of the accent, at the stock wash's alpha, under the page
 * foreground. With the accent unchanged, each palette keeps its stock wash.
 */
function rebaseSelection(
  base: PaletteTokens,
  primary: string | null,
  foreground: string,
): Pick<PaletteTokens, "selectionBackground" | "selectionForeground"> {
  if (primary === null) {
    return {
      selectionBackground: base.selectionBackground,
      selectionForeground: base.selectionForeground,
    };
  }
  const alpha = parseCssColor(base.selectionBackground)?.alpha ?? 0.12;
  return { selectionBackground: setAlpha(primary, alpha), selectionForeground: foreground };
}

function rebaseVariant(
  base: PaletteTokens,
  reference: GeneratedThemeVariant,
  current: GeneratedThemeVariant,
  primary: string,
  translucentSidebar: boolean,
): DerivedThemeVariant {
  const primaryChanged = primary !== base.primary.toLowerCase();
  const foreground = rebaseColor(base.foreground, reference.foreground, current.foreground);
  const card = rebaseColor(base.card, reference.card, current.card);
  const cardSolid = rebaseColor(base.cardSolid, reference.cardSolid, current.cardSolid);
  const accent = rebaseColor(base.accent, reference.accent, current.accent);
  const accentForeground = rebaseColor(
    base.accentForeground,
    reference.foreground,
    current.foreground,
  );
  const border = rebaseColor(base.border, reference.border, current.border);
  const sidebar = rebaseColor(base.sidebar, reference.sidebar, current.sidebar);
  const background = rebaseColor(base.background, reference.background, current.background);
  const muted = rebaseColor(base.muted, reference.muted, current.muted);
  const codeBackground = rebaseColor(
    base.codeBackground,
    reference.codeBackground,
    current.codeBackground,
  );
  const linkSurfaces = [background, cardSolid, muted, codeBackground].flatMap((surface) => {
    const color = parseCssColor(surface);
    return color?.alpha === 1 ? [rgbToHex(color)] : [];
  });
  const mutedForeground = rebaseColor(
    base.mutedForeground,
    reference.mutedForeground,
    current.mutedForeground,
  );
  const mutedContrastChanged =
    current.mutedForeground !== reference.mutedForeground ||
    current.background !== reference.background ||
    current.cardSolid !== reference.cardSolid ||
    current.muted !== reference.muted ||
    current.codeBackground !== reference.codeBackground;
  const popover = rebaseColor(base.popover, reference.cardSolid, current.cardSolid);
  const effectiveSidebar = translucentSidebar ? setAlpha(sidebar, 0.72) : sidebar;
  const elementSurfaces = [background, cardSolid, popover, effectiveSidebar].map((surface) =>
    flattenSurface(surface, background),
  );
  const toward = readableForeground(background);
  const activePrimary = primaryChanged ? primary : base.primary;
  const flattenedTrack = flattenSurface(muted, flattenSurface(card, background));
  const { primaryCta, primaryCtaForeground } = neutralPairFor(flattenedTrack);

  return {
    background,
    foreground,
    card,
    cardSolid,
    cardForeground: rebaseColor(base.cardForeground, reference.foreground, current.foreground),
    tray: rebaseColor(base.tray, reference.card, current.card),
    popover,
    popoverForeground: rebaseColor(
      base.popoverForeground,
      reference.foreground,
      current.foreground,
    ),
    primary: activePrimary,
    link: ensureContrast(primaryChanged ? primary : base.link, linkSurfaces, toward),
    primaryForeground: primaryChanged ? readableForeground(primary) : base.primaryForeground,
    primaryCta,
    primaryCtaForeground,
    ...rebaseSelection(base, primaryChanged ? primary : null, foreground),
    secondary: rebaseColor(base.secondary, reference.secondary, current.secondary),
    secondaryForeground: rebaseColor(
      base.secondaryForeground,
      reference.foreground,
      current.foreground,
    ),
    muted,
    mutedForeground:
      mutedContrastChanged && isHexColor(mutedForeground) && isHexColor(foreground)
        ? ensureContrast(mutedForeground, linkSurfaces, foreground)
        : mutedForeground,
    codeBackground,
    accent,
    accentForeground,
    border,
    borderStrong: rebaseColor(base.borderStrong, reference.borderStrong, current.borderStrong),
    buttonBorder: rebaseColor(base.buttonBorder, reference.border, current.border),
    input: rebaseColor(base.input, reference.border, current.border),
    ring: primaryChanged ? primary : base.ring,
    brandAccent: primaryChanged ? primary : base.brandAccent,
    companion: companionFor(base.companion, activePrimary, elementSurfaces, toward),
    success: ensureContrast(base.success, elementSurfaces, toward, 4.5, 0.15),
    warning: ensureContrast(base.warning, elementSurfaces, toward, 4.5, 0.15),
    destructive: ensureContrast(base.destructive, elementSurfaces, toward, 4.5, 0.1),
    // Per-palette info / cold-cache colour: held to contrast on custom surfaces, never tinted by the custom accent.
    keepCold: ensureContrast(base.keepCold, elementSurfaces, toward, 4.5, 0.1),
    sidebar: effectiveSidebar,
    sidebarForeground: rebaseColor(
      base.sidebarForeground,
      reference.foreground,
      current.foreground,
    ),
    sidebarPrimary: primaryChanged ? primary : base.sidebarPrimary,
    sidebarPrimaryForeground: primaryChanged
      ? readableForeground(primary)
      : base.sidebarPrimaryForeground,
    sidebarAccent: rebaseColor(base.sidebarAccent, reference.accent, current.accent),
    sidebarAccentForeground: rebaseColor(
      base.sidebarAccentForeground,
      reference.foreground,
      current.foreground,
    ),
    sidebarBorder: rebaseColor(base.sidebarBorder, reference.border, current.border),
    sidebarRing: primaryChanged ? primary : base.sidebarRing,
    // Tint the active-row highlight with the accent so it tracks a custom
    // accent color; keep the hand-tuned base tint when the accent is unchanged.
    // The foreground uses the same accent so selected sidebar text and icons
    // track the color picker along with the low-alpha row background.
    sidebarActive: primaryChanged ? setAlpha(primary, 0.12) : base.sidebarActive,
    sidebarActiveForeground: primaryChanged ? primary : base.sidebarActiveForeground,
    sidebarBackground: sidebar === base.sidebar ? base.sidebarBackground : sidebar,
    shellBackground: background === base.background ? base.shellBackground : background,
  };
}

export function deriveCustomTheme(theme: CustomTheme): DerivedCustomTheme {
  const normalized = normalizeTheme(theme) ?? DEFAULT_CUSTOM_THEME;
  const palette =
    PALETTES.find((candidate) => candidate.id === normalized.basePalette) ?? PALETTES[0];
  const referenceTheme = createCustomThemeFromPalette(palette);
  const reference = generateCustomTheme(referenceTheme);
  const current = generateCustomTheme({ ...normalized, translucentSidebar: false });

  return {
    light: rebaseVariant(
      palette.tokens.light,
      reference.light,
      current.light,
      normalized.accent,
      normalized.translucentSidebar,
    ),
    dark: rebaseVariant(
      palette.tokens.dark,
      reference.dark,
      current.dark,
      normalized.darkAccent,
      normalized.translucentSidebar,
    ),
  };
}

export function customThemeSwatches(theme: CustomTheme): {
  light: PaletteSwatch;
  dark: PaletteSwatch;
} {
  const normalized = normalizeTheme(theme) ?? DEFAULT_CUSTOM_THEME;
  const palette =
    PALETTES.find((candidate) => candidate.id === normalized.basePalette) ?? PALETTES[0];
  const reference = generateCustomTheme(createCustomThemeFromPalette(palette));
  const current = generateCustomTheme({ ...normalized, translucentSidebar: false });
  const derived = deriveCustomTheme(normalized);

  const swatch = (
    base: PaletteSwatch,
    referenceVariant: GeneratedThemeVariant,
    currentVariant: GeneratedThemeVariant,
    derivedVariant: DerivedThemeVariant,
  ): PaletteSwatch => ({
    bg: rebaseColor(base.bg, referenceVariant.background, currentVariant.background),
    card: rebaseColor(base.card, referenceVariant.cardSolid, currentVariant.cardSolid),
    accent:
      currentVariant.primary === referenceVariant.primary ? base.accent : currentVariant.primary,
    companion: derivedVariant.companion,
    border: rebaseColor(base.border, referenceVariant.border, currentVariant.border),
    text: rebaseColor(base.text, referenceVariant.foreground, currentVariant.foreground),
  });

  return {
    light: swatch(palette.light, reference.light, current.light, derived.light),
    dark: swatch(palette.dark, reference.dark, current.dark, derived.dark),
  };
}

export function applyCustomTheme(theme: CustomTheme): void {
  // The `--custom-*` variables go on the style root (embedded, the scope root;
  // else the document root); the `data-custom-translucent-sidebar` attribute
  // goes on every theme root so both the light `:root[...]` and dark `.dark[...]`
  // selectors match. Standalone both are the document root, so behavior is the
  // same as before.
  const styleRoot = getStyleRoot();
  if (!styleRoot) return;
  const normalized = normalizeTheme(theme) ?? DEFAULT_CUSTOM_THEME;
  const variants = deriveCustomTheme(normalized);
  const style = styleRoot.style;
  for (const root of getThemeRoots()) {
    root.toggleAttribute("data-custom-translucent-sidebar", normalized.translucentSidebar);
  }
  for (const mode of ["light", "dark"] as const) {
    for (const [key, token] of Object.entries(PALETTE_TOKEN_CSS_NAMES) as [
      keyof DerivedThemeVariant,
      (typeof PALETTE_TOKEN_CSS_NAMES)[keyof typeof PALETTE_TOKEN_CSS_NAMES],
    ][]) {
      style.setProperty(`--custom-${mode}-${token}`, variants[mode][key]);
    }
  }
}
