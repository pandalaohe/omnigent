import { describe, expect, it } from "vitest";

import {
  CODEX_NATIVE_APPROVAL_MODE_LABEL_KEY,
  CODEX_NATIVE_RUNTIME_APPROVAL_PRESETS,
  codexApprovalModeFromSession,
  codexApprovalModeLabel,
  decodeCodexPermissionObservation,
  encodeCodexPermissionObservation,
  codexPermissionObservationFromEvent as selectObservation,
  codexPermissionObservationFromWire,
  codexPermissionReadback,
} from "@/lib/codexApprovalMode";

describe("native permission observation", () => {
  const encoded = "c1:0,1,2,1,1,2,2,0,0,1792749199,cnVubmVy";
  const observed = {
    version: 1,
    source: "native-settings",
    origin: "settings-updated",
    mode: "ask-for-approval",
    approval: "on-request",
    reviewer: "user",
    sandbox: "workspace-write",
    profile: "workspace",
    uncertainty: null,
    requested_mode: null,
    received_at: "1792749199",
    runner_id: "runner",
  };
  it("decodes the frozen c1 fixture without inventing defaults", () => {
    expect(decodeCodexPermissionObservation(encoded)).toEqual(observed);
    expect(encodeCodexPermissionObservation(decodeCodexPermissionObservation(encoded)!)).toBe(
      encoded,
    );
    expect(decodeCodexPermissionObservation("c1:1,2,0,0,0,0,0,5,3,-,-")).toEqual({
      ...observed,
      source: "application-receipt",
      origin: "web-switch",
      mode: "unknown",
      approval: null,
      reviewer: null,
      sandbox: null,
      profile: null,
      uncertainty: "application-receipt",
      requested_mode: "full-access",
      received_at: null,
      runner_id: null,
    });
    expect(decodeCodexPermissionObservation("c1:0,0,0,0,0,0,0,4,0,-,-")).toEqual({
      ...observed,
      origin: "resume",
      mode: "unknown",
      approval: null,
      reviewer: null,
      sandbox: null,
      profile: null,
      uncertainty: "no-settings",
      received_at: null,
      runner_id: null,
    });
  });
  it("keeps all 19 timestamp digits and the 128 byte Unicode binding", () => {
    const id = "界".repeat(42) + "ab";
    const binding = btoa(String.fromCharCode(...new TextEncoder().encode(id)))
      .replaceAll("+", "-")
      .replaceAll("/", "_")
      .replace(/=+$/, "");
    const maximum = `c1:0,1,1,2,0,1,4,0,0,9223372036854775807,${binding}`;
    expect(maximum).toHaveLength(212);
    expect(decodeCodexPermissionObservation(maximum)).toMatchObject({
      received_at: "9223372036854775807",
      runner_id: id,
      mode: "custom",
    });
    expect(encodeCodexPermissionObservation(decodeCodexPermissionObservation(maximum)!)).toBe(
      maximum,
    );
  });
  it.each(["\n", "\r", "\r\n"])(
    "rejects trailing line endings in every c1 field (%j)",
    (suffix) => {
      const fields = encoded.slice(3).split(",");
      for (let index = 0; index < fields.length; index++) {
        const malformed = [...fields];
        malformed[index] += suffix;
        expect(
          decodeCodexPermissionObservation("c1:" + malformed.join(",")),
          `field ${index}`,
        ).toBeNull();
      }
    },
  );
  it.each([
    "",
    "{}",
    "c2:0,1,2,1,1,2,2,0,0,1,cnVubmVy",
    encoded + ",0",
    encoded.replace("c1:0", "c1:00"),
    encoded.replace(",1792749199,", ",01,"),
    encoded.replace(",1792749199,", ",9223372036854775808,"),
    encoded.replace("cnVubmVy", "YQ="),
    encoded.replace("cnVubmVy", "YR"),
    encoded.replace("cnVubmVy", "_w"),
    encoded.replace("cnVubmVy", "AA"),
    encoded.replace("c1:0,1,2", "c1:0,1,4"),
    "c1:1,2,4,0,0,0,0,5,3,-,-",
    "c1:0,2,0,0,0,0,0,4,0,-,-",
    "c1:0,1,1,0,0,0,0,2,0,-,-",
  ])("rejects malformed or inconsistent stored reports: %s", (value) => {
    expect(decodeCodexPermissionObservation(value)).toBeNull();
  });
  it("rejects unsafe readable JSON numbers instead of rounding into an identity", () => {
    expect(codexPermissionObservationFromWire({ ...observed, received_at: 1792749199 })).toEqual(
      observed,
    );
    for (const received_at of [Number("9223372036854775807"), true, 1.5, "1792749199"]) {
      expect(codexPermissionObservationFromWire({ ...observed, received_at })).toBeNull();
    }
  });
  it("rejects oversized or unencodable bindings and extra wire fields", () => {
    for (const runner_id of ["x".repeat(129), "\u0080", "\ud800", ""]) {
      expect(
        codexPermissionObservationFromWire({ ...observed, received_at: 0, runner_id }),
      ).toBeNull();
    }
    expect(
      codexPermissionObservationFromWire({ ...observed, received_at: 0, extra: true }),
    ).toBeNull();
    expect(
      decodeCodexPermissionObservation(encoded.replace("cnVubmVy", btoa("x".repeat(129)))),
    ).toBeNull();
    expect(decodeCodexPermissionObservation(encoded + "é")).toBeNull();
  });
  it("projects receipts, offline and mismatched bindings conservatively", () => {
    const observation = decodeCodexPermissionObservation(encoded);
    const session = { runnerId: "runner", labels: {} };
    expect(codexPermissionReadback(session, observation, {}).value).toBe(
      "Ask for approval · Reported",
    );
    expect(codexPermissionReadback(session, observation, { offline: true }).value).toBe(
      "Unavailable",
    );
    expect(codexPermissionReadback({ ...session, runnerId: "other" }, observation, {}).value).toBe(
      "Unconfirmed",
    );
    expect(codexPermissionReadback(session, observation, { stale: true }).selectedValue).toBeNull();
    const receipt = decodeCodexPermissionObservation("c1:1,2,0,0,0,0,0,5,3,-,-");
    expect(codexPermissionReadback(session, receipt, {})).toMatchObject({
      value: "Unconfirmed",
      selectedValue: null,
      heading: "Application response received",
    });
  });
});

describe("exact observation companion", () => {
  // Matrix rows state presence by a defined value; own-key presence is a wire
  // fact and goes through the real parseEvent entry (see sse.test.ts).
  const codexPermissionObservationFromEvent = (
    raw: unknown,
    companion: unknown,
    approvalMode: string,
  ) => selectObservation(raw, companion, approvalMode, companion !== undefined);
  // Supplemental G1 literals, not captured server packets: companion strings
  // use the existing strict c1 form and the readable values are independent
  // literals (the unsafe timestamps are the old capture's own readable text).
  const readable = {
    version: 1,
    source: "native-settings",
    origin: "settings-updated",
    mode: "ask-for-approval",
    approval: "on-request",
    reviewer: "user",
    sandbox: "workspace-write",
    profile: "workspace",
    uncertainty: null,
    requested_mode: null,
    received_at: 1792749199,
    runner_id: "runner",
  };
  const exact = "c1:0,1,2,1,1,2,2,0,0,1792749199,cnVubmVy";
  const maximum = "c1:0,1,2,1,1,2,2,0,0,9223372036854775807,cnVubmVy";
  const nullTime = "c1:0,1,2,1,1,2,2,0,0,-,-";
  const maxReadable = { ...readable, received_at: Number("9223372036854775807") };
  const nullReadable = { ...readable, received_at: null, runner_id: null };
  const missingUncertainty = {
    version: 1,
    source: "native-settings",
    origin: "settings-updated",
    mode: "ask-for-approval",
    approval: "on-request",
    reviewer: "user",
    sandbox: "workspace-write",
    profile: "workspace",
    requested_mode: null,
    received_at: 1792749199,
    runner_id: "runner",
  };
  const bindingUrl = (value: string) =>
    btoa(String.fromCharCode(...new TextEncoder().encode(value)))
      .replaceAll("+", "-")
      .replaceAll("/", "_")
      .replace(/=+$/, "");

  it("takes the decoded companion for the safe and the int63 maximum range", () => {
    expect(codexPermissionObservationFromEvent(readable, exact, "ask-for-approval")).toEqual({
      ...readable,
      received_at: "1792749199",
    });
    expect(codexPermissionObservationFromEvent(maxReadable, maximum, "ask-for-approval")).toEqual({
      ...readable,
      received_at: "9223372036854775807",
    });
  });

  it("keeps valid nullable metadata both projections agree on", () => {
    expect(codexPermissionObservationFromEvent(nullReadable, nullTime, "ask-for-approval")).toEqual(
      { ...readable, received_at: null, runner_id: null },
    );
    expect(
      codexPermissionObservationFromEvent(
        { ...readable, received_at: 0 },
        "c1:0,1,2,1,1,2,2,0,0,0,cnVubmVy",
        "ask-for-approval",
      )?.received_at,
    ).toBe("0");
  });

  it("keeps the two max-adjacent texts distinct although one Number carries both", () => {
    expect(Number("9223372036854775806")).toBe(Number("9223372036854775807"));
    expect(
      codexPermissionObservationFromEvent(
        { ...readable, received_at: Number("9223372036854775806") },
        "c1:0,1,2,1,1,2,2,0,0,9223372036854775806,cnVubmVy",
        "ask-for-approval",
      )?.received_at,
    ).toBe("9223372036854775806");
    expect(
      codexPermissionObservationFromEvent(maxReadable, maximum, "ask-for-approval")?.received_at,
    ).toBe("9223372036854775807");
  });

  it("keeps a 128 byte Unicode binding inside a 212 ASCII companion", () => {
    const runnerId = "界".repeat(42) + "ab";
    const companion = `c1:0,1,1,2,0,1,4,0,0,9223372036854775807,${bindingUrl(runnerId)}`;
    expect(new TextEncoder().encode(runnerId)).toHaveLength(128);
    expect(companion).toHaveLength(212);
    expect(
      codexPermissionObservationFromEvent(
        {
          ...readable,
          mode: "custom",
          approval: "never",
          reviewer: null,
          sandbox: "read-only",
          profile: "custom",
          received_at: Number("9223372036854775807"),
          runner_id: runnerId,
        },
        companion,
        "custom",
      ),
    ).toMatchObject({ received_at: "9223372036854775807", runner_id: runnerId, mode: "custom" });
    const ascii = `c1:0,1,2,1,1,2,2,0,0,9223372036854775807,${bindingUrl("r".repeat(128))}`;
    expect(ascii).toHaveLength(212);
    expect(
      codexPermissionObservationFromEvent(
        { ...maxReadable, runner_id: "r".repeat(128) },
        ascii,
        "ask-for-approval",
      )?.runner_id,
    ).toBe("r".repeat(128));
  });

  it("leaves the readable route unchanged when the companion is absent", () => {
    expect(codexPermissionObservationFromEvent(readable, undefined, "ask-for-approval")).toEqual({
      ...readable,
      received_at: "1792749199",
    });
    expect(
      codexPermissionObservationFromEvent(maxReadable, undefined, "ask-for-approval"),
    ).toBeNull();
  });

  it("treats an own companion key with an undefined value as present and malformed", () => {
    expect(selectObservation(readable, undefined, "ask-for-approval", false)).toEqual({
      ...readable,
      received_at: "1792749199",
    });
    expect(selectObservation(readable, undefined, "ask-for-approval", true)).toBeNull();
    expect(selectObservation(maxReadable, undefined, "ask-for-approval", true)).toBeNull();
  });

  const rejects: [string, unknown, unknown, string][] = [
    [
      "max text against a 2^53 readable",
      { ...maxReadable, received_at: 9007199254740992 },
      maximum,
      "ask-for-approval",
    ],
    ["max text against 1e300", { ...maxReadable, received_at: 1e300 }, maximum, "ask-for-approval"],
    [
      "max text against Infinity",
      { ...maxReadable, received_at: Infinity },
      maximum,
      "ask-for-approval",
    ],
    ["max text against NaN", { ...maxReadable, received_at: NaN }, maximum, "ask-for-approval"],
    [
      "max text against a negative integer",
      { ...maxReadable, received_at: -1 },
      maximum,
      "ask-for-approval",
    ],
    [
      "max text against a fraction",
      { ...maxReadable, received_at: 1792749199.5 },
      maximum,
      "ask-for-approval",
    ],
    [
      "max text against a decimal string",
      { ...maxReadable, received_at: "9223372036854775807" },
      maximum,
      "ask-for-approval",
    ],
    [
      "a different rounding bucket",
      { ...readable, received_at: 9007199254740992 },
      "c1:0,1,2,1,1,2,2,0,0,9007199254740994,cnVubmVy",
      "ask-for-approval",
    ],
    [
      "a safe value against its next integer",
      { ...readable, received_at: 9007199254740992 },
      "c1:0,1,2,1,1,2,2,0,0,9007199254740991,cnVubmVy",
      "ask-for-approval",
    ],
    [
      "a companion past the int63 maximum",
      { ...maxReadable, received_at: Number("9223372036854775808") },
      "c1:0,1,2,1,1,2,2,0,0,9223372036854775808,cnVubmVy",
      "ask-for-approval",
    ],
    [
      "a null companion time against a zero readable",
      { ...readable, received_at: 0, runner_id: null },
      nullTime,
      "ask-for-approval",
    ],
    [
      "a zero companion time against a null readable",
      { ...readable, received_at: null },
      "c1:0,1,2,1,1,2,2,0,0,0,cnVubmVy",
      "ask-for-approval",
    ],
    [
      "a readable mode that disagrees with the companion",
      { ...readable, mode: "full-access" },
      exact,
      "ask-for-approval",
    ],
    ["a readable version other than 1", { ...readable, version: 2 }, exact, "ask-for-approval"],
    ["a readable extra field", { ...readable, extra: true }, exact, "ask-for-approval"],
    ["a readable missing field", missingUncertainty, exact, "ask-for-approval"],
    [
      "a readable binding that disagrees with the companion",
      { ...readable, runner_id: "other" },
      exact,
      "ask-for-approval",
    ],
    ["an absent readable object", null, exact, "ask-for-approval"],
    ["a non-object readable", "native-settings", exact, "ask-for-approval"],
    [
      "an unknown companion prefix",
      readable,
      "c2:0,1,2,1,1,2,2,0,0,1792749199,cnVubmVy",
      "ask-for-approval",
    ],
    ["an overlong companion", readable, "c1:" + "0".repeat(210), "ask-for-approval"],
    ["a truncated companion", readable, exact.slice(0, -1), "ask-for-approval"],
    ["a null companion value", readable, null, "ask-for-approval"],
    ["a numeric companion value", readable, 1792749199, "ask-for-approval"],
    ["an outer mode that disagrees with the companion", readable, exact, "custom"],
    ["a companion without its readable projection", undefined, exact, "ask-for-approval"],
  ];
  it.each(rejects)("rejects %s", (_name, raw, companion, approvalMode) => {
    expect(codexPermissionObservationFromEvent(raw, companion, approvalMode)).toBeNull();
  });

  it("cannot refute a readable token that rounds into the same Number", () => {
    // Limitation, not a raw equality proof: the old capture's out-of-range
    // readable token and the canonical maximum both project to 2^63, so this
    // route accepts. Single-source server construction owns that guarantee.
    const rawToken = (JSON.parse('{"received_at":9223372036854775808}') as { received_at: number })
      .received_at;
    expect(rawToken).toBe(Number("9223372036854775807"));
    expect(
      codexPermissionObservationFromEvent(
        { ...maxReadable, received_at: rawToken },
        maximum,
        "ask-for-approval",
      )?.received_at,
    ).toBe("9223372036854775807");
  });
});

describe("codexApprovalMode", () => {
  it("uses all four presets in popup order; the runtime UI disables Read Only transitions", () => {
    expect(CODEX_NATIVE_RUNTIME_APPROVAL_PRESETS.map((m) => m.value)).toEqual([
      "ask-for-approval",
      "approve-for-me",
      "full-access",
      "read-only",
    ]);
  });

  describe("codexApprovalModeLabel", () => {
    it("labels the runtime presets", () => {
      expect(codexApprovalModeLabel("ask-for-approval")).toBe("Ask for approval");
      expect(codexApprovalModeLabel("approve-for-me")).toBe("Approve for me");
      expect(codexApprovalModeLabel("full-access")).toBe("Full Access");
    });

    it("still labels a read-only session the picker can't switch into", () => {
      expect(codexApprovalModeLabel("read-only")).toBe("Read Only");
    });

    it("falls back to the raw value for an unknown mode and empty for none", () => {
      expect(codexApprovalModeLabel("someFutureMode")).toBe("someFutureMode");
      expect(codexApprovalModeLabel(null)).toBe("");
      expect(codexApprovalModeLabel("")).toBe("");
    });
  });

  describe("codexApprovalModeFromSession", () => {
    it("returns the label the server stamps after a confirmed switch", () => {
      expect(
        codexApprovalModeFromSession({
          labels: { [CODEX_NATIVE_APPROVAL_MODE_LABEL_KEY]: "approve-for-me" },
        }),
      ).toBe("approve-for-me");
    });

    it("returns null when the label is absent — it never guesses from launch args", () => {
      // Runtime approval no longer rides terminal_launch_args, so args that
      // look like a preset must not resolve to one; the picker stays unset
      // until a real switch or a TUI-observed value arrives.
      expect(codexApprovalModeFromSession({})).toBeNull();
      expect(codexApprovalModeFromSession(null)).toBeNull();
      expect(codexApprovalModeFromSession({ labels: {} })).toBeNull();
      expect(
        codexApprovalModeFromSession({
          labels: { "omnigent.codex_native.bypass_sandbox": "1" },
        }),
      ).toBeNull();
    });
  });
});
