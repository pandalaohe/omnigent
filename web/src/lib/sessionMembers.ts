/**
 * One member of a joint Agent session, projected from the session's member
 * labels. The server (F1a) writes one label per member at launch —
 * ``omnigent.member.<role>`` → compact JSON
 * ``{host, harness, model, effort, lead, unavailable?}`` — and every member
 * surface (composer menu, ``@`` typeahead, unavailable banner) reads this
 * parser instead of re-decoding labels.
 */

/** Label-key prefix carrying one member snapshot per role. */
export const SESSION_MEMBER_LABEL_PREFIX = "omnigent.member.";

export interface SessionMember {
  /** Sub-agent role name, from the label key suffix, e.g. ``"reviewer"``. */
  readonly role: string;
  /** Member host id, or ``null`` when the member runs on the session host. */
  readonly host: string | null;
  readonly harness: string;
  readonly model: string | null;
  readonly effort: string | null;
  /** The lead, who receives prompts that name no member. Exactly one per Agent. */
  readonly lead: boolean;
  /** Reason code when the member cannot run, else ``null``. */
  readonly unavailable: string | null;
}

/** A member that cannot run; ``unavailable`` carries its reason code. */
export type UnavailableSessionMember = SessionMember & { readonly unavailable: string };

/** Stable empty roster for default props (a fresh ``[]`` re-renders each time). */
export const NO_SESSION_MEMBERS: readonly SessionMember[] = [];

function optionalString(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

/**
 * Parse a session's labels into its member roster: lead first, then the
 * remaining members in label order. Malformed values (unparseable JSON, a
 * non-object, a missing harness, an empty role) are ignored — a session with
 * no valid member labels renders exactly like a single-member session.
 */
export function parseSessionMembers(
  labels: Record<string, string> | null | undefined,
): SessionMember[] {
  if (!labels) return [];
  const members: SessionMember[] = [];
  for (const [key, raw] of Object.entries(labels)) {
    if (!key.startsWith(SESSION_MEMBER_LABEL_PREFIX) || typeof raw !== "string") continue;
    const role = key.slice(SESSION_MEMBER_LABEL_PREFIX.length);
    if (role === "") continue;
    let parsed: unknown;
    try {
      parsed = JSON.parse(raw);
    } catch {
      continue;
    }
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) continue;
    const value = parsed as Record<string, unknown>;
    const harness = optionalString(value.harness);
    if (harness === null) continue;
    members.push({
      role,
      host: optionalString(value.host),
      harness,
      model: optionalString(value.model),
      effort: optionalString(value.effort),
      lead: value.lead === true,
      unavailable: optionalString(value.unavailable),
    });
  }
  return [...members.filter((member) => member.lead), ...members.filter((member) => !member.lead)];
}

/** True when this session carries a 2+ member roster. */
export function hasMultipleMembers(members: readonly SessionMember[]): boolean {
  return members.length >= 2;
}

const UNAVAILABLE_REASONS: Record<string, string> = {
  host_offline: "host offline",
  harness_not_configured: "harness not set up on the host",
  "binary-missing": "CLI missing",
  "needs-auth": "sign-in needed",
  "version-too-low": "CLI too old",
  model_missing: "model not offered by the host",
};

/** Human-readable reason for a member's ``unavailable`` reason code. */
export function memberUnavailableReason(code: string): string {
  return UNAVAILABLE_REASONS[code] ?? code;
}

/** The members of a roster that cannot run, narrowed to a reason code. */
export function unavailableMembers(members: readonly SessionMember[]): UnavailableSessionMember[] {
  return members.filter(
    (member): member is UnavailableSessionMember => member.unavailable !== null,
  );
}
