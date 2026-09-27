/** UI-only session capability gates, derived from the live session snapshot. */

const CLAUDE_NATIVE_WRAPPER = "claude-code-native-ui";
const CODEX_NATIVE_WRAPPER = "codex-native-ui";
const PI_NATIVE_WRAPPER = "pi-native-ui";
const DEVIN_NATIVE_WRAPPER = "devin-native-ui";

export function isSdkHarnessSession(
  session:
    | {
        labels?: Record<string, string | null> | null;
        harness?: string | null;
        inferenceConfigured?: boolean;
      }
    | null
    | undefined,
): boolean {
  return (
    !session?.inferenceConfigured &&
    session?.labels?.["omnigent.wrapper"] == null &&
    (session?.harness === "claude-sdk" || session?.harness === "codex")
  );
}

/**
 * Fail-closed gate for Web UI reasoning-effort controls.
 *
 * :param session: Session or sidebar row carrying labels and harness.
 * :returns: True for sessions with Web UI effort controls.
 *     cursor-native is intentionally excluded: its effort lives on the /model
 *     picker's per-model "Tab to modify" axis and a model switch resets it to
 *     that model's default, so a Web UI effort dial would silently diverge from
 *     the TUI. cursor-native supports model switching only for now.
 */
export function supportsEffortControl(
  session:
    | {
        labels?: Record<string, string | null> | null;
        harness?: string | null;
        inferenceConfigured?: boolean;
      }
    | null
    | undefined,
): boolean {
  const wrapper = session?.labels?.["omnigent.wrapper"];
  return (
    wrapper === CLAUDE_NATIVE_WRAPPER ||
    wrapper === CODEX_NATIVE_WRAPPER ||
    wrapper === PI_NATIVE_WRAPPER ||
    // Devin has no --effort flag: effort is a model-variant suffix the executor
    // recombines and re-applies via /model, so the in-chat effort dial is live.
    wrapper === DEVIN_NATIVE_WRAPPER ||
    (wrapper == null && session?.harness === "codex-native") ||
    isSdkHarnessSession(session)
  );
}
