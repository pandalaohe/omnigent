// Running-session approval switcher for codex-native. Unlike the create-time
// presets in `nativeHarnessModes` (which map to `terminal_launch_args`), the
// live switch drives Codex's own `/permissions` popup by keystroke injection,
// so its options and labels mirror that popup. Read Only isn't offered: the
// popup lists it only for sessions already running a read-only permission
// profile, so users start a new read-only session instead. The full-bypass
// sandbox stance stays launch-only (the server 400s it on a runtime switch).

export interface CodexRuntimeApprovalPreset {
  value: string;
  label: string;
  description: string;
}

/** Conversation-label key the server writes for the live approval mode. */
export const CODEX_NATIVE_APPROVAL_MODE_LABEL_KEY = "omnigent.codex_native.approval_mode";

/**
 * The three runtime approval stances the picker can switch into, in Codex's
 * `/permissions` popup order.
 */
export const CODEX_APPROVAL_PRESETS: readonly CodexRuntimeApprovalPreset[] = [
  {
    value: "ask-for-approval",
    label: "Ask for approval",
    description: "Read/edit/run in the workspace; approval for the internet or external edits",
  },
  {
    value: "approve-for-me",
    label: "Approve for me",
    description: "Only asks for actions detected as potentially unsafe",
  },
  {
    value: "full-access",
    label: "Full Access",
    description: "Edit any file and access the internet without approval",
  },
  {
    value: "read-only",
    label: "Read Only",
    description: "Read workspace files, with approval required for edits or internet access",
  },
];

/** Runtime picker uses the same list; the UI disables Read Only transitions. */
export const CODEX_NATIVE_RUNTIME_APPROVAL_PRESETS = CODEX_APPROVAL_PRESETS;

// A session launched read-only still reports it as its live mode, so it keeps
// a label even though the picker can't switch into it.

/** Human label for an approval-mode value, falling back to the raw value. */
export function codexApprovalModeLabel(mode: string | null | undefined): string {
  if (!mode) return "";
  return CODEX_APPROVAL_PRESETS.find((m) => m.value === mode)?.label ?? mode;
}

/**
 * The stored scalar selection, retained for compatibility and historical detail.
 *
 * This label alone cannot confirm native settings. The native picker uses the
 * validated observation and its binding; SDK consumers keep their scalar flow.
 */
export function codexApprovalModeFromSession(
  session: { labels?: Record<string, string | null> | null } | null | undefined,
): string | null {
  const labelled = session?.labels?.[CODEX_NATIVE_APPROVAL_MODE_LABEL_KEY];
  return typeof labelled === "string" && labelled ? labelled : null;
}

export const CODEX_PERMISSION_OBSERVATION_LABEL_KEY =
  "omnigent.codex_native.permission_observation";

const OBSERVATION_CODES = {
  source: ["native-settings", "application-receipt"],
  origin: ["resume", "settings-updated", "web-switch"],
  mode: ["unknown", "custom", "ask-for-approval", "approve-for-me", "full-access", "read-only"],
  approval: [null, "on-request", "never", "on-failure", "untrusted", "granular"],
  reviewer: [null, "user", "auto_review", "guardian_subagent"],
  sandbox: [null, "read-only", "workspace-write", "danger-full-access", "external-sandbox"],
  profile: [null, "read-only", "workspace", "full-access", "custom"],
  uncertainty: [
    null,
    "missing-fields",
    "unsupported-value",
    "conflicting-fields",
    "no-settings",
    "application-receipt",
  ],
  requested_mode: [null, "ask-for-approval", "approve-for-me", "full-access", "read-only"],
} as const;

type ObservationCodes = typeof OBSERVATION_CODES;
type ObservationAxes = { readonly [K in keyof ObservationCodes]: ObservationCodes[K][number] };
export type CodexPermissionObservation = ObservationAxes & {
  readonly version: 1;
  /** Canonical epoch seconds as decimal text: c1 permits integers beyond JS's safe range. */
  readonly received_at: string | null;
  readonly runner_id: string | null;
};

/** Client cache state belongs to the session identity, outside the twelve wire fields. */
export interface CodexPermissionCachedReport {
  readonly encoded: string;
  readonly observation: CodexPermissionObservation | null;
  readonly stale: boolean;
}

const PROFILE_SANDBOX = {
  "read-only": "read-only",
  workspace: "workspace-write",
  "full-access": "danger-full-access",
} as const;

function validObservationCombination(value: CodexPermissionObservation): boolean {
  const {
    source,
    origin,
    mode,
    requested_mode,
    approval,
    reviewer,
    sandbox,
    profile,
    uncertainty,
  } = value;
  const axes = [approval, reviewer, sandbox, profile];
  if (source === "application-receipt") {
    return (
      origin === "web-switch" &&
      mode === "unknown" &&
      requested_mode !== null &&
      axes.every((axis) => axis === null) &&
      (uncertainty === "application-receipt" || uncertainty === "unsupported-value")
    );
  }
  if (origin === "web-switch" || requested_mode !== null) return false;
  const profileSandbox = profile && profile !== "custom" ? PROFILE_SANDBOX[profile] : null;
  const conflict = sandbox !== null && profileSandbox != null && sandbox !== profileSandbox;
  // This validates the frozen normalized contract; it never classifies raw native settings.
  if (uncertainty === "no-settings")
    return mode === "unknown" && axes.every((axis) => axis === null);
  if (uncertainty === "conflicting-fields") return mode === "unknown";
  if (uncertainty === "unsupported-value") {
    return (
      mode === "unknown" ||
      (mode === "custom" &&
        approval !== null &&
        (sandbox !== null || profile === "custom") &&
        !conflict)
    );
  }
  if (conflict) return false;
  const boundary = sandbox ?? profileSandbox;
  if (
    approval === null ||
    (boundary == null && profile !== "custom") ||
    (profile !== "custom" &&
      approval === "on-request" &&
      (boundary === "read-only" || boundary === "workspace-write") &&
      reviewer === null)
  ) {
    return mode === "unknown" && uncertainty === "missing-fields";
  }
  if (uncertainty !== null) return false;
  if (profile === "custom") return mode === "custom";
  if (approval === "never" && boundary === "danger-full-access") return mode === "full-access";
  if (approval === "on-request" && (boundary === "read-only" || boundary === "workspace-write")) {
    if (reviewer === "user")
      return mode === (boundary === "read-only" ? "read-only" : "ask-for-approval");
    if (reviewer === "auto_review" && boundary === "workspace-write")
      return mode === "approve-for-me";
  }
  return mode === "custom";
}

function canonicalTimestamp(value: string): boolean {
  const match = /^(0|[1-9][0-9]{0,18})$/.exec(value);
  return match?.[0] === value && (value.length < 19 || value <= "9223372036854775807");
}

function runnerBytes(value: string): Uint8Array | null {
  if (
    !value ||
    Array.from(value).some(
      (char) =>
        char.charCodeAt(0) <= 31 || (char.charCodeAt(0) >= 127 && char.charCodeAt(0) <= 159),
    )
  )
    return null;
  const bytes = new TextEncoder().encode(value);
  // TextEncoder replaces lone surrogates; they must not become another binding.
  if (
    bytes.length > 128 ||
    new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes) !== value
  )
    return null;
  return bytes;
}

function bindingCode(bytes: Uint8Array): string {
  return btoa(String.fromCharCode(...bytes))
    .replaceAll("+", "-")
    .replaceAll("/", "_")
    .replace(/=+$/, "");
}

/** Only validated finite observations reach the cache's existing label representation. */
export function encodeCodexPermissionObservation(value: CodexPermissionObservation): string {
  return (
    "c1:" +
    [
      ...Object.keys(OBSERVATION_CODES).map((key) => {
        const field = key as keyof ObservationCodes;
        return (OBSERVATION_CODES[field] as readonly (string | null)[]).indexOf(value[field]);
      }),
      value.received_at ?? "-",
      value.runner_id === null ? "-" : bindingCode(new TextEncoder().encode(value.runner_id)),
    ].join(",")
  );
}

/** Unknown, damaged or noncanonical reports fail closed, including complete combination validation. */
export function decodeCodexPermissionObservation(
  encoded: unknown,
): CodexPermissionObservation | null {
  if (
    typeof encoded !== "string" ||
    encoded.length > 212 ||
    Array.from(encoded).some((char) => char.charCodeAt(0) > 127) ||
    !encoded.startsWith("c1:")
  )
    return null;
  const fields = encoded.slice(3).split(",");
  if (fields.length !== 11) return null;
  const axes: Record<string, string | null> = {};
  for (const [index, [field, codes]] of Object.entries(OBSERVATION_CODES).entries()) {
    const code = fields[index];
    if (code.length !== 1 || !/^[0-9]$/.test(code) || Number(code) >= codes.length) return null;
    axes[field] = codes[Number(code)];
  }
  if (fields[9] !== "-" && !canonicalTimestamp(fields[9])) return null;
  let runner: string | null = null;
  if (fields[10] !== "-") {
    if (!/^[A-Za-z0-9_-]{2,171}$/.test(fields[10])) return null;
    try {
      const raw = atob(fields[10].replaceAll("-", "+").replaceAll("_", "/"));
      const bytes = Uint8Array.from(raw, (char) => char.charCodeAt(0));
      runner = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes);
      if (!runnerBytes(runner) || bindingCode(bytes) !== fields[10]) return null;
    } catch {
      return null;
    }
  }
  const value = {
    ...axes,
    version: 1,
    received_at: fields[9] === "-" ? null : fields[9],
    runner_id: runner,
  } as CodexPermissionObservation;
  return validObservationCombination(value) && encodeCodexPermissionObservation(value) === encoded
    ? value
    : null;
}

/** The readable projection's own field values, before timestamp text normalization. */
type ReadableObservationFields = Omit<CodexPermissionObservation, "received_at"> & {
  readonly received_at: number | null;
};

/**
 * Validates the twelve readable fields without requiring a safe-integer
 * timestamp: a live event may legitimately carry the int63 range, which only
 * the companion can pin down exactly.
 */
function readableObservationFields(raw: unknown): ReadableObservationFields | null {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) return null;
  const value = raw as Record<string, unknown>;
  const keys = [...Object.keys(OBSERVATION_CODES), "version", "received_at", "runner_id"];
  if (
    Object.keys(value).length !== keys.length ||
    !keys.every((key) => Object.hasOwn(value, key)) ||
    value.version !== 1
  )
    return null;
  for (const [field, codes] of Object.entries(OBSERVATION_CODES)) {
    if (!(codes as readonly unknown[]).includes(value[field])) return null;
  }
  if (value.received_at !== null) {
    if (
      typeof value.received_at !== "number" ||
      !Number.isFinite(value.received_at) ||
      !Number.isInteger(value.received_at) ||
      value.received_at < 0
    )
      return null;
  }
  if (
    value.runner_id !== null &&
    (typeof value.runner_id !== "string" || runnerBytes(value.runner_id) === null)
  )
    return null;
  return value as unknown as ReadableObservationFields;
}

/** Readable JSON must preserve the declared integer exactly before it becomes a report. */
export function codexPermissionObservationFromWire(
  raw: unknown,
): CodexPermissionObservation | null {
  const value = readableObservationFields(raw);
  if (value === null) return null;
  if (value.received_at !== null && !Number.isSafeInteger(value.received_at)) return null;
  const observation = {
    ...value,
    received_at: value.received_at === null ? null : String(value.received_at),
  } as CodexPermissionObservation;
  return validObservationCombination(observation) ? observation : null;
}

/**
 * Selects the exact observation for a live event. Presence is a separate wire
 * fact: only a truly absent key may fall back to the readable projection, and a
 * present field is present even when its value is `undefined` (JSON cannot
 * carry that, a direct object call can). An absent companion keeps the legacy
 * readable route, which rejects timestamps past the safe integer range; any
 * unusable present companion leaves the observation Unconfirmed.
 */
export function codexPermissionObservationFromEvent(
  raw: unknown,
  companion: unknown,
  approvalMode: string,
  companionPresent: boolean,
): CodexPermissionObservation | null {
  if (!companionPresent) return codexPermissionObservationFromWire(raw);
  const exact = decodeCodexPermissionObservation(companion);
  if (exact === null || exact.mode !== approvalMode) return null;
  const readable = readableObservationFields(raw);
  if (readable === null) return null;
  for (const field of Object.keys(OBSERVATION_CODES) as (keyof ObservationCodes)[]) {
    if (readable[field] !== exact[field]) return null;
  }
  if (readable.runner_id !== exact.runner_id) return null;
  if (exact.received_at === null) return readable.received_at === null ? exact : null;
  // The comparison can only refute a projection; identity below stays `exact`.
  return readable.received_at === Number(exact.received_at) ? exact : null;
}

export function codexPermissionObservationFromSession(
  session: { labels?: Record<string, string | null> | null } | null | undefined,
): CodexPermissionObservation | null {
  return decodeCodexPermissionObservation(
    session?.labels?.[CODEX_PERMISSION_OBSERVATION_LABEL_KEY],
  );
}

export interface CodexPermissionReadback {
  value: string;
  selectedValue: string | null;
  heading: string;
  rows: readonly { label: string; value: string }[];
  note: string;
}

export function codexPermissionReadback(
  session: { runnerId?: string | null; labels?: Record<string, string | null> | null },
  observation: CodexPermissionObservation | null,
  connectivity: { offline?: boolean; stale?: boolean },
): CodexPermissionReadback {
  const stale = connectivity.stale;
  const contextMatches =
    observation?.runner_id != null && observation.runner_id === session.runnerId;
  const reported =
    !!observation &&
    observation.source === "native-settings" &&
    contextMatches &&
    !stale &&
    !connectivity.offline;
  const selectedValue =
    reported && CODEX_APPROVAL_PRESETS.some((preset) => preset.value === observation.mode)
      ? observation.mode
      : null;
  const reasons = {
    "missing-fields": "Some settings were not reported.",
    "unsupported-value": "Some reported settings or binding details are unsupported.",
    "conflicting-fields": "The reported settings conflict.",
    "no-settings": "No permission settings were reported.",
    "application-receipt": "Native settings not confirmed.",
  };
  const rows = [
    { label: "Approvals", value: observation?.approval ?? "Not reported" },
    {
      label: "Reviewer",
      value:
        observation?.reviewer == null
          ? "Not reported"
          : observation.reviewer +
            (observation.approval === "never" ? " · Current policy does not trigger approval" : ""),
    },
    { label: "Sandbox", value: observation?.sandbox ?? "Not reported" },
    { label: "Profile", value: observation?.profile ?? "Not reported" },
    {
      label: "Source",
      value: observation ? `${observation.source} · ${observation.origin}` : "Not reported",
    },
    {
      label: "Received",
      value:
        observation?.received_at === null || observation?.received_at === undefined
          ? "Not reported"
          : `${observation.received_at} (epoch seconds)`,
    },
    {
      label: "Context",
      value: connectivity.offline
        ? "Offline · Last reported"
        : stale || !contextMatches
          ? "Binding unconfirmed · Last reported"
          : "Matches session binding",
    },
  ];
  if (observation?.requested_mode)
    rows.push({ label: "Requested", value: codexApprovalModeLabel(observation.requested_mode) });
  const storedSelection = codexApprovalModeFromSession(session);
  if (!observation && storedSelection)
    rows.push({ label: "Stored selection", value: codexApprovalModeLabel(storedSelection) });
  return {
    value: connectivity.offline
      ? "Unavailable"
      : selectedValue
        ? `${codexApprovalModeLabel(selectedValue)} · Reported`
        : reported && observation.mode === "custom"
          ? "Custom"
          : "Unconfirmed",
    selectedValue,
    heading:
      observation?.source === "application-receipt"
        ? "Application response received"
        : reported
          ? "Last received settings"
          : "Last reported settings",
    rows,
    note: `${observation ? (observation.uncertainty ? reasons[observation.uncertainty] : "Settings as last received.") : "Stored report unavailable."} A settings report does not authorize a command.`,
  };
}
