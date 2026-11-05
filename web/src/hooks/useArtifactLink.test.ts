import { afterEach, describe, expect, it, vi } from "vitest";
import { ArtifactLinkError, artifactErrorMessage, fetchArtifactSource } from "./useArtifactLink";

afterEach(() => vi.unstubAllGlobals());

describe("artifact size errors", () => {
  it("shows the effective server limit from a refused preview", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response("too large", {
          status: 413,
          headers: { "X-Omnigent-Artifact-Max-Bytes": "16777216" },
        }),
      ),
    );
    const error = await fetchArtifactSource("/v1/artifacts/test/report.html", "nonce").catch(
      (err: unknown) => err,
    );
    expect(artifactErrorMessage(error)).toBe("File too large to preview (16777216 bytes limit)");
  });

  it.each([null, "bad", "0", "-1"])(
    "keeps older or malformed responses usable (%s)",
    async (limit) => {
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue(
          new Response("too large", {
            status: 413,
            headers: limit === null ? {} : { "X-Omnigent-Artifact-Max-Bytes": limit },
          }),
        ),
      );
      const error = await fetchArtifactSource("/v1/artifacts/test/report.html", "nonce").catch(
        (err: unknown) => err,
      );
      expect(error).toBeInstanceOf(ArtifactLinkError);
      expect(artifactErrorMessage(error)).toBe("File too large to preview");
    },
  );
});
