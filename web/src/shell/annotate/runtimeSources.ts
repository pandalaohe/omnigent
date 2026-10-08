// Raw sources for the in-frame annotation runtime (design §2.5), loaded lazily
// on the parent side and shipped over the annotate port. The runtime is plain
// ES2020 and never imported as a module: the frame evaluates each source as a
// function body, so `?raw` text is the transport format.
//
// `core` (anchor generation, resolution, markers) loads whenever the page has
// annotations; `pick` (freeze, picker, composer, capture) only on first mode
// entry. Later slices append their files to the lists here.

export type AnnotateRuntimePart = "core" | "pick";

/** Load one runtime part's sources in evaluation order. */
export async function loadRuntimeSources(part: AnnotateRuntimePart): Promise<string[]> {
  if (part === "core") {
    const [finder, anchor, resolver, markers] = await Promise.all([
      import("./runtime/finder.js?raw"),
      import("./runtime/anchor.js?raw"),
      import("./runtime/resolver.js?raw"),
      import("./runtime/markers.js?raw"),
    ]);
    return [finder.default, anchor.default, resolver.default, markers.default];
  }
  const [freeze, picker, snapdom, crop] = await Promise.all([
    import("./runtime/freeze.js?raw"),
    import("./runtime/picker.js?raw"),
    // The package's exports map hides `dist/snapdom.js`, so the IIFE that sets
    // `window.snapdom` is reached by filesystem path. `vite/client` types the
    // `?raw` suffix.
    import("../../../node_modules/@zumer/snapdom/dist/snapdom.js?raw"),
    import("./runtime/crop.js?raw"),
  ]);
  // Page scripts share the frame's realm and may overwrite `window.snapdom`
  // after load; pin the IIFE's export for crop.js now.
  const snapdomWithRef = snapdom.default + "\n;ns.snapdom = window.snapdom;";
  return [freeze.default, picker.default, snapdomWithRef, crop.default];
}
