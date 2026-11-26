import { describe, expect, it } from "vitest";
import { modelResolutionNotes, sessionHeaderModelState, splitModelId } from "./modelReference";

describe("saved model identity", () => {
  it("keeps the complete model suffix and leaves plain ids alone", () => {
    expect(splitModelId("provider/family/model")).toEqual({
      provider: "provider",
      model: "family/model",
    });
    expect(splitModelId("plain")).toEqual({ provider: null, model: "plain" });
  });
  it("deduplicates resolution notes that differ only by setting", () => {
    const notes = [
      {
        setting: "session_choice",
        saved: "gemini-4-neon",
        model: "gemini-4-neon",
        outcome: "missing",
        host: "host-a",
      },
      {
        setting: "session",
        saved: "gemini-4-neon",
        model: "gemini-4-neon",
        outcome: "missing",
        host: "host-a",
      },
    ];
    expect(modelResolutionNotes({ "omnigent.model_resolution": JSON.stringify(notes) })).toEqual([
      notes[0],
    ]);
  });
  it("reads a resolution note spanning bounded labels and rejects missing chunks", () => {
    const notes = [
      { setting: "explicit", saved: "old/x", model: "new/x", outcome: "renamed", host: "host-a" },
    ];
    const text = JSON.stringify(notes);
    const labels = {
      "omnigent.model_resolution": JSON.stringify({ chunks: 2 }),
      "omnigent.model_resolution.0": text.slice(0, 50),
      "omnigent.model_resolution.1": text.slice(50),
    };
    expect(modelResolutionNotes(labels)).toEqual(notes);
    expect(modelResolutionNotes({ ...labels, "omnigent.model_resolution.1": "invalid" })).toEqual(
      [],
    );
    expect(
      modelResolutionNotes({ "omnigent.model_resolution": JSON.stringify({ chunks: 1000000 }) }),
    ).toEqual([]);
  });
});

describe("sessionHeaderModelState", () => {
  it("reports no difference when requested, launched, and running match", () => {
    expect(
      sessionHeaderModelState({
        harness: "codex-native",
        requested: "gpt-6.2-sol",
        launched: "gpt-6.2-sol",
        running: "gpt-6.2-sol",
      }),
    ).toEqual({
      requested: "gpt-6.2-sol",
      launched: "gpt-6.2-sol",
      running: "gpt-6.2-sol",
      requestedDiffers: false,
      runningDiffers: false,
      hasDifference: false,
    });
  });

  it("flags requested differing from launched after provider rename", () => {
    expect(
      sessionHeaderModelState({
        harness: "codex-native",
        requested: "ccs-krill/gpt-6.2-sol",
        launched: "krill/gpt-6.2-sol",
        running: "krill/gpt-6.2-sol",
      }),
    ).toEqual({
      requested: "ccs-krill/gpt-6.2-sol",
      launched: "krill/gpt-6.2-sol",
      running: "krill/gpt-6.2-sol",
      requestedDiffers: true,
      runningDiffers: false,
      hasDifference: true,
    });
  });

  it("flags running differing from launched and ignores missing running", () => {
    expect(
      sessionHeaderModelState({
        harness: "claude-native",
        requested: "opus",
        launched: "opus",
        running: "claude-sonnet-5-5",
      }),
    ).toMatchObject({ requestedDiffers: false, runningDiffers: true, hasDifference: true });

    expect(
      sessionHeaderModelState({
        harness: "codex-native",
        requested: "gpt-6.2-sol",
        launched: "gpt-6.2-sol",
        running: null,
      }),
    ).toMatchObject({ requestedDiffers: false, runningDiffers: false, hasDifference: false });
  });

  it("treats Claude family aliases as equivalent to concrete ids of the same family", () => {
    for (const [alias, concrete] of [
      ["opus", "claude-opus-5-5"],
      ["opus", "claude-opus-4-8[1m]"],
      ["opus[1m]", "claude-opus-5-5[1m]"],
      ["opus[1m]", "claude-opus-5-5"],
      ["sonnet", "claude-sonnet-5-5"],
      ["sonnet[1m]", "claude-3-5-sonnet-20241022"],
      ["haiku", "claude-haiku-4-5"],
      ["fable", "claude-fable-5-1"],
      ["opus", "databricks-claude-opus-5"],
      ["sonnet", "system.ai.claude-sonnet-4-6[1m]"],
      ["opus", "us.anthropic.claude-opus-4-8-v1:0"],
    ] as const) {
      expect(
        sessionHeaderModelState({
          harness: "claude-native",
          requested: alias,
          launched: alias,
          running: concrete,
        }),
      ).toMatchObject({ requestedDiffers: false, runningDiffers: false, hasDifference: false });
    }

    // Requested vs launched alias comparisons follow the same rule, including claude-sdk.
    expect(
      sessionHeaderModelState({
        harness: "claude-sdk",
        requested: "opus",
        launched: "claude-opus-5-5",
        running: "claude-opus-5-5",
      }),
    ).toMatchObject({ requestedDiffers: false, runningDiffers: false, hasDifference: false });

    expect(
      sessionHeaderModelState({
        harness: "claude-native",
        requested: "claude-opus-5-5",
        launched: "opus",
        running: "claude-opus-5-5",
      }),
    ).toMatchObject({ requestedDiffers: false, runningDiffers: false, hasDifference: false });
  });

  it("prefers Claude native model catalog mappings over family heuristics", () => {
    const options = [
      { id: "opus", model: "claude-opus-5-5", displayName: "Opus 5.5" },
      { id: "sonnet", model: "system.ai.claude-sonnet-4-6[1m]", displayName: "Sonnet 4.6" },
      { id: "sonnet_5", model: "databricks-claude-sonnet-5", displayName: "Sonnet 5" },
      { id: "haiku", model: "provider-a/claude-haiku-4-5", displayName: "Haiku 4.5" },
    ];

    expect(
      sessionHeaderModelState({
        harness: "claude-native",
        requested: "opus",
        launched: "opus",
        running: "claude-opus-5-5",
        codexModelOptions: options,
      }),
    ).toMatchObject({ requestedDiffers: false, runningDiffers: false, hasDifference: false });

    // Catalog pins opus to 5-5, so running 4-8 is a real substitution and must warn.
    expect(
      sessionHeaderModelState({
        harness: "claude-native",
        requested: "opus",
        launched: "opus",
        running: "claude-opus-4-8",
        codexModelOptions: options,
      }),
    ).toMatchObject({ requestedDiffers: false, runningDiffers: true, hasDifference: true });

    // Mechanical gateway prefixes and [1m] suffixes fold against the catalog target.
    expect(
      sessionHeaderModelState({
        harness: "claude-native",
        requested: "sonnet",
        launched: "sonnet",
        running: "claude-sonnet-4-6",
        codexModelOptions: options,
      }),
    ).toMatchObject({ requestedDiffers: false, runningDiffers: false, hasDifference: false });

    expect(
      sessionHeaderModelState({
        harness: "claude-native",
        requested: "sonnet_5",
        launched: "sonnet_5",
        running: "claude-sonnet-5",
        codexModelOptions: options,
      }),
    ).toMatchObject({ requestedDiffers: false, runningDiffers: false, hasDifference: false });

    // Provider-qualified catalog target accepts its own provider and flags another provider.
    expect(
      sessionHeaderModelState({
        harness: "claude-native",
        requested: "haiku",
        launched: "haiku",
        running: "provider-a/claude-haiku-4-5",
        codexModelOptions: options,
      }),
    ).toMatchObject({ requestedDiffers: false, runningDiffers: false, hasDifference: false });

    expect(
      sessionHeaderModelState({
        harness: "claude-native",
        requested: "haiku",
        launched: "haiku",
        running: "provider-b/claude-haiku-4-5",
        codexModelOptions: options,
      }),
    ).toMatchObject({ requestedDiffers: false, runningDiffers: true, hasDifference: true });

    // Unresolved alias rows (no concrete target) fall back to family matching.
    expect(
      sessionHeaderModelState({
        harness: "claude-native",
        requested: "opus",
        launched: "opus",
        running: "claude-opus-5-5",
        codexModelOptions: [{ id: "opus", model: "opus", displayName: "Opus" }],
      }),
    ).toMatchObject({ requestedDiffers: false, runningDiffers: false, hasDifference: false });

    // Catalog pin canonicalized by Claude Code (proxy-a.claude-opus-5-5 -> claude-opus-5-5) matches,
    // whether launched by alias or by the concrete pinned id.
    expect(
      sessionHeaderModelState({
        harness: "claude-native",
        requested: "opus",
        launched: "opus",
        running: "claude-opus-5-5",
        codexModelOptions: [{ id: "opus", model: "proxy-a.claude-opus-5-5" }],
      }),
    ).toMatchObject({ requestedDiffers: false, runningDiffers: false, hasDifference: false });

    expect(
      sessionHeaderModelState({
        harness: "claude-native",
        requested: "opus",
        launched: "proxy-a.claude-opus-5-5",
        running: "claude-opus-5-5",
        codexModelOptions: [{ id: "opus", model: "proxy-a.claude-opus-5-5" }],
      }),
    ).toMatchObject({ requestedDiffers: false, runningDiffers: false, hasDifference: false });

    // Catalog membership ignores context suffixes when checking canonical proxy pins.
    expect(
      sessionHeaderModelState({
        harness: "claude-native",
        requested: "opus[1m]",
        launched: "proxy-a.claude-opus-5-5[1m]",
        running: "claude-opus-5-5",
        codexModelOptions: [{ id: "opus[1m]", model: "proxy-a.claude-opus-5-5[200k]" }],
      }),
    ).toMatchObject({ requestedDiffers: false, runningDiffers: false, hasDifference: false });

    // Provider-qualified suffixed alias looks up its provider-qualified base pin.
    expect(
      sessionHeaderModelState({
        harness: "claude-native",
        requested: "provider-a/opus[1m]",
        launched: "provider-a/opus[1m]",
        running: "provider-a/claude-opus-4-8",
        codexModelOptions: [{ id: "provider-a/opus", model: "provider-a/claude-opus-5-5" }],
      }),
    ).toMatchObject({ requestedDiffers: false, runningDiffers: true, hasDifference: true });
  });

  it("keeps real Claude substitutions and other harnesses visible", () => {
    // Different Claude family.
    expect(
      sessionHeaderModelState({
        harness: "claude-native",
        requested: "opus",
        launched: "claude-sonnet-5-5",
        running: "claude-sonnet-5-5",
      }),
    ).toMatchObject({ requestedDiffers: true, runningDiffers: false, hasDifference: true });

    // Context suffix dropped between alias request and alias launch.
    expect(
      sessionHeaderModelState({
        harness: "claude-native",
        requested: "opus[1m]",
        launched: "opus",
        running: "claude-opus-5-5",
      }),
    ).toMatchObject({ requestedDiffers: true, runningDiffers: false, hasDifference: true });

    // Explicit [1m] dropped between concrete request and launch still warns, while running omission is allowed.
    expect(
      sessionHeaderModelState({
        harness: "claude-native",
        requested: "claude-opus-5-5[1m]",
        launched: "claude-opus-5-5",
        running: "claude-opus-5-5",
      }),
    ).toMatchObject({ requestedDiffers: true, runningDiffers: false, hasDifference: true });

    expect(
      sessionHeaderModelState({
        harness: "claude-native",
        requested: "claude-opus-5-5[1m]",
        launched: "claude-opus-5-5[1m]",
        running: "claude-opus-5-5",
      }),
    ).toMatchObject({ requestedDiffers: false, runningDiffers: false, hasDifference: false });

    // Unresolved alias bridge does not hide a concrete requested -> running version downgrade.
    expect(
      sessionHeaderModelState({
        harness: "claude-native",
        requested: "claude-opus-5-5",
        launched: "opus",
        running: "claude-opus-4-8",
      }),
    ).toMatchObject({ requestedDiffers: false, runningDiffers: true, hasDifference: true });

    // Unknown dotted proxy namespace or bogus Bedrock prefix without a catalog pin still warns.
    expect(
      sessionHeaderModelState({
        harness: "claude-native",
        requested: "opus",
        launched: "opus",
        running: "proxy-b.claude-opus-5-5",
      }),
    ).toMatchObject({ requestedDiffers: false, runningDiffers: true, hasDifference: true });

    expect(
      sessionHeaderModelState({
        harness: "claude-native",
        requested: "opus",
        launched: "opus",
        running: "zz.anthropic.claude-opus-5-5",
      }),
    ).toMatchObject({ requestedDiffers: false, runningDiffers: true, hasDifference: true });

    // Exact catalog rows preserve explicit [1m] suffixes on request -> launch, and conflicting running suffixes warn.
    expect(
      sessionHeaderModelState({
        harness: "claude-native",
        requested: "provider-a/opus[1m]",
        launched: "provider-a/claude-opus-5-5",
        running: "provider-a/claude-opus-5-5",
        codexModelOptions: [{ id: "provider-a/opus[1m]", model: "provider-a/claude-opus-5-5" }],
      }),
    ).toMatchObject({ requestedDiffers: true, runningDiffers: false, hasDifference: true });

    expect(
      sessionHeaderModelState({
        harness: "claude-native",
        requested: "opus[1m]",
        launched: "claude-opus-5-5[200k]",
        running: "claude-opus-5-5[200k]",
        codexModelOptions: [{ id: "opus[1m]", model: "claude-opus-5-5[200k]" }],
      }),
    ).toMatchObject({ requestedDiffers: true, runningDiffers: false, hasDifference: true });

    expect(
      sessionHeaderModelState({
        harness: "claude-native",
        requested: "claude-opus-5-5[1m]",
        launched: "claude-opus-5-5[1m]",
        running: "claude-opus-5-5[200k]",
      }),
    ).toMatchObject({ requestedDiffers: false, runningDiffers: true, hasDifference: true });

    // Codex requested -> launched spelling rewrite remains visible as before.
    expect(
      sessionHeaderModelState({
        harness: "codex-native",
        requested: "databricks-gpt-5-6-luna",
        launched: "gpt-5.6-luna",
        running: "gpt-5.6-luna",
      }),
    ).toMatchObject({ requestedDiffers: true, runningDiffers: false, hasDifference: true });

    // Provider substitution without catalog vouching.
    expect(
      sessionHeaderModelState({
        harness: "claude-native",
        requested: "opus",
        launched: "opus",
        running: "other-provider/claude-opus-5-5",
      }),
    ).toMatchObject({ requestedDiffers: false, runningDiffers: true, hasDifference: true });

    expect(
      sessionHeaderModelState({
        harness: "claude-native",
        requested: "provider-a/opus",
        launched: "provider-a/opus",
        running: "provider-b/claude-opus-5-5",
      }),
    ).toMatchObject({ requestedDiffers: false, runningDiffers: true, hasDifference: true });

    // Non-Claude model id or concrete version downgrade.
    expect(
      sessionHeaderModelState({
        harness: "claude-native",
        requested: "opus",
        launched: "opus",
        running: "vendor-opus-1",
      }),
    ).toMatchObject({ requestedDiffers: false, runningDiffers: true, hasDifference: true });

    expect(
      sessionHeaderModelState({
        harness: "claude-native",
        requested: "claude-opus-5-5",
        launched: "claude-opus-5-5",
        running: "claude-opus-4-8",
      }),
    ).toMatchObject({ requestedDiffers: false, runningDiffers: true, hasDifference: true });

    // Other harnesses without verified alias mappings remain strict.
    expect(
      sessionHeaderModelState({
        harness: "pi-native",
        requested: "provider-a/claude-opus-5-5",
        launched: "provider-a/claude-opus-5-5",
        running: "provider-b/claude-opus-5-5",
      }),
    ).toMatchObject({ requestedDiffers: false, runningDiffers: true, hasDifference: true });

    expect(
      sessionHeaderModelState({
        harness: "kimi-native",
        requested: "kimi-alias",
        launched: "kimi-alias",
        running: "system.ai.kimi-k3",
      }),
    ).toMatchObject({ requestedDiffers: false, runningDiffers: true, hasDifference: true });
  });

  it("compares antigravity slug vs display name without false positives", () => {
    expect(
      sessionHeaderModelState({
        harness: "antigravity-native",
        requested: "gemini-4-neon",
        launched: null,
        running: "Gemini 4 Neon (High)",
      }),
    ).toMatchObject({ launched: null, runningDiffers: false, hasDifference: false });

    expect(
      sessionHeaderModelState({
        harness: "antigravity-native",
        requested: "gemini-4-neon",
        launched: null,
        running: "Gemini 2.5 Flash",
      }),
    ).toMatchObject({ launched: null, runningDiffers: true, hasDifference: true });
  });

  it("reports no difference on default sessions with no launched model", () => {
    expect(
      sessionHeaderModelState({
        harness: "claude-native",
        requested: null,
        launched: null,
        running: "claude-sonnet-4-6",
      }),
    ).toMatchObject({
      launched: null,
      requestedDiffers: false,
      runningDiffers: false,
      hasDifference: false,
    });
  });

  it("treats verified equivalent Codex spellings as matching while surfacing real changes", () => {
    expect(
      sessionHeaderModelState({
        harness: "codex-native",
        requested: "databricks-gpt-5-6-luna",
        launched: "databricks-gpt-5-6-luna",
        running: "gpt-5.6-luna",
      }),
    ).toMatchObject({ requestedDiffers: false, runningDiffers: false, hasDifference: false });

    expect(
      sessionHeaderModelState({
        harness: "codex-native",
        requested: "system.ai.gpt-5-6-luna",
        launched: "system.ai.gpt-5-6-luna",
        running: "gpt-5.6-luna",
      }),
    ).toMatchObject({ requestedDiffers: false, runningDiffers: false, hasDifference: false });

    // Real version change stays visible.
    expect(
      sessionHeaderModelState({
        harness: "codex-native",
        requested: "databricks-gpt-5-6-luna",
        launched: "databricks-gpt-5-6-luna",
        running: "gpt-5.5-luna",
      }),
    ).toMatchObject({ requestedDiffers: false, runningDiffers: true, hasDifference: true });

    // Real provider substitution stays visible.
    expect(
      sessionHeaderModelState({
        harness: "codex-native",
        requested: "krill/gpt-6.2-sol",
        launched: "krill/gpt-6.2-sol",
        running: "gpt-6.2-sol",
      }),
    ).toMatchObject({ requestedDiffers: false, runningDiffers: true, hasDifference: true });

    // Explicit provider rename still flags requestedDiffers even when running matches launched.
    expect(
      sessionHeaderModelState({
        harness: "codex-native",
        requested: "ccs-krill/gpt-6.2-sol",
        launched: "krill/gpt-6.2-sol",
        running: "krill/gpt-6.2-sol",
      }),
    ).toMatchObject({ requestedDiffers: true, runningDiffers: false, hasDifference: true });
  });
});
