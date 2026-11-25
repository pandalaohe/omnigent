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
        running: "claude-opus-4-8[1m]",
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
