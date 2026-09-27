import { describe, expect, it } from "vitest";
import { artifactCommentsEndpoint, framePagePath, readVisitConfig } from "./visitConfig";

function documentWithConfig(json: string): Document {
  document.body.innerHTML = `<script type="application/json" id="omni-visit-config">${json}</script>`;
  return document;
}

describe("readVisitConfig", () => {
  it("parses the server's config block, grant included", () => {
    const config = {
      frameUrl: "/proxy/v1/artifacts/h-token/reports/index.html",
      nonce: "n1",
      token: "g-token",
      grant: "grant-1",
      path: "reports/index.html",
      commentsEnabled: true,
    };
    expect(readVisitConfig(documentWithConfig(JSON.stringify(config)))).toEqual(config);
  });

  it("accepts a null grant and disabled comments", () => {
    const config = {
      frameUrl: "/v1/artifacts/h/x.html",
      nonce: "n1",
      token: "g",
      grant: null,
      path: "x.html",
      commentsEnabled: false,
    };
    expect(readVisitConfig(documentWithConfig(JSON.stringify(config)))).toEqual(config);
  });

  it("returns null when the block is absent", () => {
    document.body.innerHTML = "";
    expect(readVisitConfig(document)).toBeNull();
  });

  it("returns null for malformed JSON or a missing/mistyped field", () => {
    expect(readVisitConfig(documentWithConfig("{not json"))).toBeNull();
    expect(
      readVisitConfig(
        documentWithConfig(
          JSON.stringify({ frameUrl: "/v1/artifacts/h/x.html", nonce: "n", token: "g" }),
        ),
      ),
    ).toBeNull();
    expect(
      readVisitConfig(
        documentWithConfig(
          JSON.stringify({
            frameUrl: "/v1/artifacts/h/x.html",
            nonce: "n",
            token: "g",
            grant: "grant",
            path: "x.html",
            commentsEnabled: "yes",
          }),
        ),
      ),
    ).toBeNull();
  });
});

describe("framePagePath", () => {
  it("returns the decoded bundle-relative path under a base prefix", () => {
    expect(framePagePath("/proxy/6767/v1/artifacts/h-token/reports/page%20two.html")).toBe(
      "reports/page two.html",
    );
  });

  it("uses the route's first marker when the bundle path nests v1/artifacts", () => {
    expect(framePagePath("/v1/artifacts/T/dir/v1/artifacts/page.html")).toBe(
      "dir/v1/artifacts/page.html",
    );
  });

  it("returns null for a token-only path or an unrelated pathname", () => {
    expect(framePagePath("/v1/artifacts/h-token")).toBeNull();
    expect(framePagePath("/c/session-1")).toBeNull();
  });
});

describe("artifactCommentsEndpoint", () => {
  it("keeps the deployment base path the frame was served under", () => {
    expect(artifactCommentsEndpoint("/proxy/6767/v1/artifacts/h/x.html")).toBe(
      "/proxy/6767/v1/artifact-comments",
    );
  });

  it("posts at the root for a root deployment", () => {
    expect(artifactCommentsEndpoint("/v1/artifacts/h/x.html")).toBe("/v1/artifact-comments");
  });
});
