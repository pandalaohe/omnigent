import { comparableModelId, findNativeModelOption } from "./codexNativeModels";
import type { NativeModelOption } from "./types";

export interface ModelIdentity {
  provider: string | null;
  model: string;
}

export function splitModelId(id: string): ModelIdentity {
  const slash = id.indexOf("/");
  return slash < 0
    ? { provider: null, model: id }
    : { provider: id.slice(0, slash), model: id.slice(slash + 1) };
}

export interface ModelResolutionNote {
  setting: string;
  saved: string;
  model: string;
  outcome: string;
  host: string;
}

export function modelResolutionNotes(labels: Record<string, string> = {}): ModelResolutionNote[] {
  const key = "omnigent.model_resolution";
  try {
    let record = JSON.parse(labels[key] ?? "null");
    if (record && typeof record === "object" && !Array.isArray(record)) {
      const count = record.chunks;
      if (!Number.isSafeInteger(count) || count < 1 || count > Object.keys(labels).length)
        return [];
      let text = "";
      for (let index = 0; index < count; index++) {
        if (typeof labels[`${key}.${index}`] !== "string") return [];
        text += labels[`${key}.${index}`];
      }
      record = JSON.parse(text);
    }
    const notes = Array.isArray(record)
      ? record.filter(
          (note): note is ModelResolutionNote =>
            note &&
            typeof note.saved === "string" &&
            typeof note.model === "string" &&
            typeof note.outcome === "string" &&
            typeof note.host === "string" &&
            typeof note.setting === "string",
        )
      : [];
    const seen = new Set<string>();
    return notes.filter((note) => {
      const dedupeKey = JSON.stringify([note.host, note.saved, note.model, note.outcome]);
      if (seen.has(dedupeKey)) return false;
      seen.add(dedupeKey);
      return true;
    });
  } catch {
    return [];
  }
}

export interface SessionHeaderModelState {
  requested: string | null;
  launched: string | null;
  running: string | null;
  requestedDiffers: boolean;
  runningDiffers: boolean;
  hasDifference: boolean;
}

function trimOrNull(value: string | null | undefined): string | null {
  const trimmed = typeof value === "string" ? value.trim() : "";
  return trimmed ? trimmed : null;
}

function antigravityModelIdentity(value: string): string {
  return value
    .trim()
    .replace(/\s*\((?:low|medium|high|xhigh|max)\)$/i, "")
    .replace(/[-_](?:low|medium|high|xhigh|max)$/i, "")
    .toLowerCase()
    .replace(/[^a-z0-9]/g, "");
}

const CLAUDE_MODEL_ALIASES = new Set(["fable", "opus", "sonnet", "haiku"]);
const CLAUDE_CATALOG_PREFIXES = ["databricks-", "system.ai."] as const;
const CLAUDE_CONTEXT_SUFFIX_RE = /\[[^[\]\s]+\]$/;
const CLAUDE_FALLBACK_MODEL_RE =
  /^(?:(?:databricks-|system\.ai\.)|(?:(?:us|eu|ap|au|ca|jp|global)\.)?anthropic\.)?claude-([a-z0-9._:-]+)$/;

function isClaudeHarness(harness: string | null | undefined): boolean {
  return (
    harness === "claude-native" ||
    harness === "native-claude" ||
    harness === "claude-sdk" ||
    harness === "claude_sdk" ||
    harness === "claude"
  );
}

function claudeContextSuffix(bareModel: string): string | null {
  return CLAUDE_CONTEXT_SUFFIX_RE.exec(bareModel.trim().toLowerCase())?.[0] ?? null;
}

function normalizeClaudeContext(bareModel: string, stripContext: boolean): string {
  const lower = bareModel.trim().toLowerCase();
  return stripContext ? lower.replace(CLAUDE_CONTEXT_SUFFIX_RE, "") : lower;
}

function claudeContextMatches(
  leftModel: string,
  rightModel: string,
  allowContextOmission: boolean,
): boolean {
  const leftSuffix = claudeContextSuffix(leftModel);
  const rightSuffix = claudeContextSuffix(rightModel);
  if (leftSuffix === rightSuffix) return true;
  if (!allowContextOmission) return false;
  // Running reports may omit [1m] or add the default [1m], but conflicting explicit suffixes warn.
  return leftSuffix === null || rightSuffix === null;
}

function parseClaudeAlias(bareModel: string): string | null {
  const stripped = normalizeClaudeContext(bareModel, true);
  return CLAUDE_MODEL_ALIASES.has(stripped) ? stripped : null;
}

function claudeComparableId(bareModel: string, stripContext: boolean): string {
  let bare = normalizeClaudeContext(bareModel, stripContext);
  for (const prefix of CLAUDE_CATALOG_PREFIXES) {
    if (bare.startsWith(prefix)) {
      bare = bare.slice(prefix.length);
      break;
    }
  }
  return bare;
}

function canonicalClaudeId(bareModel: string, stripContext: boolean): string | null {
  const normalized = normalizeClaudeContext(bareModel, stripContext);
  const index = normalized.lastIndexOf("claude-");
  if (index === -1) return null;
  if (index > 0 && !/[._:-]/.test(normalized[index - 1])) return null;
  return normalized.slice(index);
}

function claudeConcreteFamily(bareModel: string): string | null {
  const stripped = normalizeClaudeContext(bareModel, true);
  const match = CLAUDE_FALLBACK_MODEL_RE.exec(stripped);
  if (!match) return null;
  let found: string | null = null;
  for (const segment of match[1].split(/[^a-z0-9]+/)) {
    if (!CLAUDE_MODEL_ALIASES.has(segment)) continue;
    if (found !== null && found !== segment) return null;
    found = segment;
  }
  return found;
}

function resolveClaudeCatalogTarget(
  value: string,
  options?: readonly NativeModelOption[] | null,
): string | null {
  if (!options || options.length === 0) return null;
  const lower = value.trim().toLowerCase();
  const suffix = claudeContextSuffix(lower);
  const withPreservedSuffix = (target: string): string =>
    suffix !== null ? `${target.replace(CLAUDE_CONTEXT_SUFFIX_RE, "")}${suffix}` : target;
  const findConcrete = (idLower: string): string | null => {
    for (const option of options) {
      if (option.id.trim().toLowerCase() !== idLower) continue;
      const target = typeof option.model === "string" ? option.model.trim() : "";
      if (!target) continue;
      if (parseClaudeAlias(splitModelId(target).model) === null) return target;
    }
    return null;
  };
  const exact = findConcrete(lower);
  if (exact !== null) return withPreservedSuffix(exact);
  if (suffix !== null) {
    const base = lower.slice(0, -suffix.length);
    if (CLAUDE_MODEL_ALIASES.has(splitModelId(base).model)) {
      const target = findConcrete(base);
      if (target !== null) return withPreservedSuffix(target);
    }
  }
  return null;
}

function hasClaudeCatalogModel(
  value: string,
  options?: readonly NativeModelOption[] | null,
): boolean {
  if (!options || options.length === 0) return false;
  const target = splitModelId(value);
  const targetModel = normalizeClaudeContext(target.model, true);
  return options.some((option) => {
    if (typeof option.model !== "string") return false;
    const optionSplit = splitModelId(option.model);
    return (
      optionSplit.provider === target.provider &&
      normalizeClaudeContext(optionSplit.model, true) === targetModel
    );
  });
}

function claudeModelsEquivalent(
  left: string,
  right: string,
  options: readonly NativeModelOption[] | null | undefined,
  allowContextOmission: boolean,
): boolean {
  if (left === right) return true;
  const leftSplit = splitModelId(left);
  const rightSplit = splitModelId(right);
  const leftAlias = parseClaudeAlias(leftSplit.model);
  const rightAlias = parseClaudeAlias(rightSplit.model);
  // Keep context-suffix drops (e.g. opus[1m] -> opus) and family swaps visible between two aliases.
  if (leftAlias !== null && rightAlias !== null) {
    return (
      leftSplit.provider === rightSplit.provider &&
      leftSplit.model.toLowerCase() === rightSplit.model.toLowerCase()
    );
  }
  const leftTarget = resolveClaudeCatalogTarget(left, options);
  const rightTarget = resolveClaudeCatalogTarget(right, options);
  const effectiveLeft = leftTarget ?? left;
  const effectiveRight = rightTarget ?? right;
  if (effectiveLeft === effectiveRight) return true;
  const effLeftSplit = splitModelId(effectiveLeft);
  const effRightSplit = splitModelId(effectiveRight);
  if (effLeftSplit.provider !== effRightSplit.provider) return false;
  const effLeftAlias = parseClaudeAlias(effLeftSplit.model);
  const effRightAlias = parseClaudeAlias(effRightSplit.model);
  if (effLeftAlias === null && effRightAlias === null) {
    if (!claudeContextMatches(effLeftSplit.model, effRightSplit.model, allowContextOmission)) {
      return false;
    }
    const leftComparable = claudeComparableId(effLeftSplit.model, true);
    const rightComparable = claudeComparableId(effRightSplit.model, true);
    if (leftComparable !== "" && leftComparable === rightComparable) return true;
    const matchesPinnedCanonical = (pinnedModel: string, candidateModel: string): boolean => {
      const candidate = normalizeClaudeContext(candidateModel, true);
      return candidate.startsWith("claude-") && canonicalClaudeId(pinnedModel, true) === candidate;
    };
    const leftPinned = leftTarget !== null || hasClaudeCatalogModel(effectiveLeft, options);
    const rightPinned = rightTarget !== null || hasClaudeCatalogModel(effectiveRight, options);
    return (
      (leftPinned && matchesPinnedCanonical(effLeftSplit.model, effRightSplit.model)) ||
      (rightPinned && matchesPinnedCanonical(effRightSplit.model, effLeftSplit.model))
    );
  }
  const aliasFamily = effLeftAlias ?? effRightAlias;
  const concreteModel = effLeftAlias !== null ? effRightSplit.model : effLeftSplit.model;
  if (aliasFamily === null || claudeConcreteFamily(concreteModel) !== aliasFamily) return false;
  const leftSuffix = claudeContextSuffix(effLeftSplit.model);
  const rightSuffix = claudeContextSuffix(effRightSplit.model);
  if (allowContextOmission) {
    return leftSuffix === null || rightSuffix === null || leftSuffix === rightSuffix;
  }
  return leftSuffix === null || leftSuffix === rightSuffix;
}

function codexModelsEquivalent(
  launched: string,
  running: string,
  options?: readonly NativeModelOption[] | null,
): boolean {
  if (launched === running) return true;
  // Keep provider substitutions visible even when the model suffix matches.
  if (splitModelId(launched).provider !== splitModelId(running).provider) return false;
  const left = comparableModelId(launched);
  const right = comparableModelId(running);
  if (left !== "" && left === right) return true;
  if (options && options.length > 0) {
    const launchedOption = findNativeModelOption(options, launched);
    const runningOption = findNativeModelOption(options, running);
    if (launchedOption !== null && launchedOption === runningOption) return true;
  }
  return false;
}

export interface StreamModelDisplayRevisions {
  requested: number;
  reported: number;
  launched: number;
}

const streamRequestedModels = new Map<string, { revision: number; requested: string | null }>();
const streamReportedModels = new Map<string, { revision: number; model: string }>();
const streamLaunchedModels = new Map<string, { revision: number; launched: string | null }>();

export function streamModelDisplayRevisions(conversationId: string): StreamModelDisplayRevisions {
  return {
    requested: streamRequestedModels.get(conversationId)?.revision ?? 0,
    reported: streamReportedModels.get(conversationId)?.revision ?? 0,
    launched: streamLaunchedModels.get(conversationId)?.revision ?? 0,
  };
}

export function recordStreamRequestedModel(conversationId: string, requested: string | null): void {
  streamRequestedModels.set(conversationId, {
    revision: (streamRequestedModels.get(conversationId)?.revision ?? 0) + 1,
    requested,
  });
}

export function recordStreamReportedModel(conversationId: string, model: string): void {
  streamReportedModels.set(conversationId, {
    revision: (streamReportedModels.get(conversationId)?.revision ?? 0) + 1,
    model,
  });
}

export function recordStreamLaunchedModel(conversationId: string, launched: string | null): void {
  streamLaunchedModels.set(conversationId, {
    revision: (streamLaunchedModels.get(conversationId)?.revision ?? 0) + 1,
    launched,
  });
}

export function clearStreamModelDisplay(conversationId: string): void {
  streamRequestedModels.delete(conversationId);
  streamReportedModels.delete(conversationId);
  streamLaunchedModels.delete(conversationId);
}

/** Preserve newer confirmed live requested/running/launched updates over an older in-flight snapshot. */
export function preserveStreamModelDisplay<
  T extends {
    id: string;
    requested?: string | null;
    running?: string | null;
    launched?: string | null;
  },
>(session: T, before: StreamModelDisplayRevisions | undefined): T {
  if (before === undefined) return session;
  let next = session;
  const latestRequested = streamRequestedModels.get(session.id);
  if (latestRequested !== undefined && latestRequested.revision !== before.requested) {
    next = { ...next, requested: latestRequested.requested };
  }
  const latestReported = streamReportedModels.get(session.id);
  if (latestReported !== undefined && latestReported.revision !== before.reported) {
    next = { ...next, running: latestReported.model };
  }
  const latestLaunched = streamLaunchedModels.get(session.id);
  if (latestLaunched !== undefined && latestLaunched.revision !== before.launched) {
    next = { ...next, launched: latestLaunched.launched };
  }
  return next;
}

export function sessionHeaderModelState(
  session:
    | {
        harness?: string | null;
        requested?: string | null;
        launched?: string | null;
        running?: string | null;
        codexModelOptions?: readonly NativeModelOption[] | null;
      }
    | null
    | undefined,
): SessionHeaderModelState {
  const requested = trimOrNull(session?.requested);
  const launched = trimOrNull(session?.launched);
  const running = trimOrNull(session?.running);
  const claudeHarness = isClaudeHarness(session?.harness);
  const requestedDiffers = Boolean(
    requested &&
    launched &&
    (claudeHarness
      ? !claudeModelsEquivalent(requested, launched, session?.codexModelOptions, false)
      : requested !== launched),
  );
  let runningDiffers = false;
  if (launched && running) {
    if (session?.harness === "codex-native" || session?.harness === "codex") {
      runningDiffers = !codexModelsEquivalent(launched, running, session.codexModelOptions);
    } else if (claudeHarness) {
      runningDiffers = !claudeModelsEquivalent(launched, running, session?.codexModelOptions, true);
      // Do not let an unresolved alias bridge hide a concrete requested -> running version change.
      if (
        !runningDiffers &&
        !requestedDiffers &&
        requested &&
        !claudeModelsEquivalent(requested, running, session?.codexModelOptions, true)
      ) {
        runningDiffers = true;
      }
    } else {
      runningDiffers = running !== launched;
    }
  } else if (!launched && session?.harness === "antigravity-native" && requested && running) {
    runningDiffers = antigravityModelIdentity(requested) !== antigravityModelIdentity(running);
  }
  return {
    requested,
    launched,
    running,
    requestedDiffers,
    runningDiffers,
    hasDifference: requestedDiffers || runningDiffers,
  };
}
