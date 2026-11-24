import { describe, expect, it } from "vitest";
import { modelResolutionNotes, splitModelId } from "./modelReference";

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
