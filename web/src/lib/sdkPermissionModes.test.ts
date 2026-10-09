import { describe, expect, it } from "vitest";

import {
  normalizeSdkPermissionMode,
  sdkInitialPermissionMode,
  sdkPermissionModeFromSession,
  sdkPermissionOptions,
} from "@/lib/sdkPermissionModes";

describe("sdkPermissionModes", () => {
  describe("normalizeSdkPermissionMode", () => {
    it("maps legacy Codex default to ask-for-approval", () => {
      expect(normalizeSdkPermissionMode("codex", "default")).toBe("ask-for-approval");
    });

    it("leaves current Codex presets unchanged", () => {
      expect(normalizeSdkPermissionMode("codex", "approve-for-me")).toBe("approve-for-me");
    });

    it("leaves Claude SDK default unchanged", () => {
      expect(normalizeSdkPermissionMode("claude-sdk", "default")).toBe("default");
    });
  });

  it("offers Codex's /permissions presets in popup order, plus Read Only", () => {
    expect(sdkPermissionOptions("codex")?.map(({ value, label }) => ({ value, label }))).toEqual([
      { value: "ask-for-approval", label: "Ask for approval" },
      { value: "approve-for-me", label: "Approve for me" },
      { value: "full-access", label: "Full Access" },
      { value: "read-only", label: "Read Only" },
    ]);
  });

  it("starts a new Codex SDK session on ask-for-approval", () => {
    expect(sdkInitialPermissionMode("codex")).toBe("ask-for-approval");
  });

  describe("sdkPermissionModeFromSession", () => {
    it("reads the legacy default Codex label back as ask-for-approval", () => {
      expect(
        sdkPermissionModeFromSession(
          {
            harness: "codex",
            labels: { "omnigent.codex_sdk.approval_mode": "default" },
          },
          "codex",
        ),
      ).toBe("ask-for-approval");
    });

    it("passes the current Codex presets through", () => {
      expect(
        sdkPermissionModeFromSession(
          {
            harness: "codex",
            labels: { "omnigent.codex_sdk.approval_mode": "approve-for-me" },
          },
          "codex",
        ),
      ).toBe("approve-for-me");
    });

    it("leaves Claude SDK's own default mode unchanged", () => {
      expect(
        sdkPermissionModeFromSession(
          {
            harness: "claude-sdk",
            labels: { "omnigent.claude_sdk.permission_mode": "default" },
          },
          "claude-sdk",
        ),
      ).toBe("default");
    });
  });
});
