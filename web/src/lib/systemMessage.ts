// Detects and parses the `[System: ...]` user-role messages the runtime
// injects into conversations (task completion/failure/cancellation,
// timer firings, terminal-idle notifications, sub-agent wake notices).
// These are sent as
// role="user" because OpenAI-style chat formats lack a mid-conversation
// system-event role; the UI re-classifies them so they render as muted
// markers instead of normal user bubbles.

import type { MessageContentBlock } from "@/lib/blocks";
import { isTextBlock } from "@/lib/blocks";

const TEAMMATE_MESSAGE_RE =
  /^<teammate-message\s+[^>]*\bteammate_id="[^"]+"[^>]*>[\s\S]*?<\/teammate-message>/;
const AGENT_MESSAGE_RE = /^<agent-message\s+[^>]*\bfrom="[^"]+"[^>]*>[\s\S]*?<\/agent-message>/;
const TEAMMATE_MESSAGE_PREFIXES = [
  "Another Claude session sent a message:\n",
  "Another Claude session sent a message while you were working:\n",
  "A peer session sent a message while you were working:\n",
];
const TEAMMATE_DELIVERY_GUIDANCE = [
  "This came from another Claude session — not typed by your user,",
  'That "other Claude session" is an agent working inside this same session —',
];

/** Recognize older Claude task/team context that was persisted without is_meta. */
export function isClaudeAgentMessageContent(content: MessageContentBlock[]): boolean {
  if (content.some((block) => block.type !== "input_text")) return false;
  const texts = content.map((block) => ("text" in block ? block.text.trim() : "")).filter(Boolean);
  return texts.length > 0 && texts.every(isClaudeAgentMessageText);
}

function isClaudeAgentMessageText(text: string): boolean {
  if (isClaudeTaskNotificationText(text)) {
    return /<summary>\s*Agent\b/.test(text) || /<result>[\s\S]*<\/result>/.test(text);
  }
  const prefix = TEAMMATE_MESSAGE_PREFIXES.find((candidate) => text.startsWith(candidate));
  let remaining = prefix ? text.slice(prefix.length).trimStart() : text;
  let match = TEAMMATE_MESSAGE_RE.exec(remaining) ?? AGENT_MESSAGE_RE.exec(remaining);
  if (!match) return false;
  while (match) {
    remaining = remaining.slice(match[0].length).trimStart();
    if (!remaining) return true;
    match = TEAMMATE_MESSAGE_RE.exec(remaining) ?? AGENT_MESSAGE_RE.exec(remaining);
  }
  // Peer wrappers can append native delivery instructions after the envelopes.
  return Boolean(
    prefix && TEAMMATE_DELIVERY_GUIDANCE.some((guidance) => remaining.startsWith(guidance)),
  );
}

export type SystemMessageKind =
  | "task_completed"
  | "task_failed"
  | "task_cancelled"
  | "timer_fired"
  | "terminal_idle"
  | "subagent_wake"
  | "interrupted"
  | "peer_outcome"
  | "question_answer"
  | "generic";

export interface QuestionAnswer {
  question: string;
  answer: string;
  id?: string;
}

export interface ParsedSystemMessage {
  kind: SystemMessageKind;
  /** Human-readable label, no opaque ids. e.g. "Sub-agent completed". */
  label: string;
  /** Everything after the header line. Empty for headers without a body. */
  body: string;
  answers?: QuestionAnswer[];
  elicitationId?: string;
  cardRef?: string;
}

const HEADER_RE = /^\[System: (.+)\]$/;
// Claude Code's own interrupt record (Escape), mirrored from its transcript.
// Not a `[System: ...]` marker, but we re-classify it the same way: a muted
// "Interrupted" indicator instead of a raw user bubble. Keep this exact so a
// user's bracketed question such as `[Request interrupted by user?]` still
// renders as normal text.
const INTERRUPT_RE = /^\[Request interrupted by user(?: for tool use)?\]$/;
const TASK_RE = /^task (\S+) \((tool|sub_agent|client_tool)\) (completed|failed|cancelled)$/;
const TIMER_RE = /^timer (\S+) fired$/;
const TERMINAL_RE = /^terminal (\S+) is idle$/;
const SUBAGENT_WAKE_RE =
  /^sub-agent .+ finished \((completed|failed|cancelled)\) — \d+ results? waiting in inbox\. Call sys_read_inbox to collect\.$/;

// Claude Code's background-task wake, re-labelled by the client from the raw
// `<task-notification>` block the CLI injects (see `claudeTaskNotificationMarker`).
const BACKGROUND_TASK_RE = /^background task (\S+) (completed|failed|cancelled|finished)$/;
const TASK_NOTIFICATION_MARKERS = ["<task-notification>", "<task-id>", "</task-notification>"];

const TASK_KIND_LABEL: Record<string, string> = {
  tool: "Tool",
  sub_agent: "Sub-agent",
  client_tool: "Client tool",
};

// Peer-messaging back-notice: `[System: peer message <peer_id> to session
// <receiver_id> "<title>" <state>[ (<reason>)]]`. A batched notice is N of
// these full markers joined by "\n" — each one closes its own `]` — not one
// wrapper around several clauses (`format_peer_back_notice` /
// `_maybe_flush` in peer_sweeper.py).
const PEER_OUTCOME_CLAUSE_RE =
  /^peer message (\S+) to session (\S+) "([^"]*)" (delivered|failed|expired|refused_by_user)(?: \(([^)]*)\))?$/;
const PEER_OUTCOME_STATE_LABEL: Record<string, string> = {
  delivered: "delivered",
  failed: "failed",
  expired: "expired",
  refused_by_user: "refused by user",
};

function parsePeerOutcome(text: string): ParsedSystemMessage | null {
  const lines = text.split("\n").filter((line) => line.length > 0);
  if (lines.length === 0) return null;
  const inners: string[] = [];
  for (const line of lines) {
    const headerMatch = HEADER_RE.exec(line);
    if (!headerMatch || !PEER_OUTCOME_CLAUSE_RE.test(headerMatch[1])) return null;
    inners.push(headerMatch[1]);
  }
  const clauseMatch = PEER_OUTCOME_CLAUSE_RE.exec(inners[0]);
  if (!clauseMatch) return null;
  const [, , , , state, reason] = clauseMatch;
  const label = `Peer message ${PEER_OUTCOME_STATE_LABEL[state]}${reason ? ` (${reason})` : ""}`;
  return { kind: "peer_outcome", label, body: inners.slice(1).join("\n") };
}

export function parseSystemMessage(text: string): ParsedSystemMessage | null {
  const peerOutcome = parsePeerOutcome(text);
  if (peerOutcome) return peerOutcome;

  const newlineIdx = text.indexOf("\n");
  const firstLine = newlineIdx === -1 ? text : text.slice(0, newlineIdx);
  const body = newlineIdx === -1 ? "" : text.slice(newlineIdx + 1);

  if (INTERRUPT_RE.test(firstLine)) {
    return { kind: "interrupted", label: "Interrupted", body: "" };
  }

  const headerMatch = HEADER_RE.exec(firstLine);
  if (!headerMatch) return null;
  const inner = headerMatch[1];

  if (inner === "Question answered" || /^answers to your question card #q[\da-f]+$/.test(inner)) {
    const parsed = parseQuestionAnswers(body);
    if (parsed) {
      return {
        kind: "question_answer",
        label: "Question answered",
        body: "",
        answers: parsed.answers,
        ...(parsed.elicitationId ? { elicitationId: parsed.elicitationId } : {}),
        ...(inner.startsWith("answers to your question card #")
          ? { cardRef: inner.slice("answers to your question card #".length) }
          : {}),
      };
    }
    const cardRef = inner.startsWith("answers to your question card #")
      ? inner.slice("answers to your question card #".length)
      : undefined;
    const singleLine =
      !body.includes("\n") && body.split(" → ").length === 2 ? /^(.+?) → (.*)$/.exec(body) : null;
    return {
      kind: "question_answer",
      label: "Question answered",
      body,
      ...(cardRef ? { cardRef } : {}),
      ...(!cardRef && singleLine
        ? { answers: [{ question: singleLine[1]!, answer: singleLine[2]! }] }
        : {}),
    };
  }

  // The runner synthesizes `[System: interrupted]` for codex-native (which
  // writes no interrupt record of its own); render it the same as Claude's.
  if (inner === "interrupted") {
    return { kind: "interrupted", label: "Interrupted", body };
  }

  const taskMatch = TASK_RE.exec(inner);
  if (taskMatch) {
    const [, taskId, taskKind, status] = taskMatch;
    const kindLabel = TASK_KIND_LABEL[taskKind] ?? taskKind;
    if (status === "completed") {
      return {
        kind: "task_completed",
        label: `${kindLabel} ${taskId} completed`,
        body,
      };
    }
    if (status === "failed") {
      return {
        kind: "task_failed",
        label: `${kindLabel} ${taskId} failed`,
        body,
      };
    }
    return {
      kind: "task_cancelled",
      label: `${kindLabel} ${taskId} cancelled`,
      body: "",
    };
  }
  const backgroundMatch = BACKGROUND_TASK_RE.exec(inner);
  if (backgroundMatch) {
    const status = backgroundMatch[2] ?? "finished";
    const kind: SystemMessageKind =
      status === "completed"
        ? "task_completed"
        : status === "failed"
          ? "task_failed"
          : status === "cancelled"
            ? "task_cancelled"
            : "generic";
    return { kind, label: `Background task ${status}`, body };
  }
  const timerMatch = TIMER_RE.exec(inner);
  if (timerMatch) {
    return {
      kind: "timer_fired",
      label: `Timer ${timerMatch[1]} fired`,
      body,
    };
  }
  const terminalMatch = TERMINAL_RE.exec(inner);
  if (terminalMatch) {
    return {
      kind: "terminal_idle",
      label: `Terminal ${terminalMatch[1]} idle`,
      body: "",
    };
  }
  if (SUBAGENT_WAKE_RE.test(inner)) {
    return {
      kind: "subagent_wake",
      label: "Sub-agent result ready",
      body,
    };
  }
  // Known prefix, unknown pattern — still treat as a system marker so new
  // producers get the muted styling without an web change.
  return { kind: "generic", label: inner, body };
}

function parseQuestionAnswers(
  body: string,
): { answers: QuestionAnswer[]; elicitationId?: string } | null {
  try {
    const value: unknown = JSON.parse(body);
    if (value && typeof value === "object" && !Array.isArray(value)) {
      const record = value as Record<string, unknown>;
      if (
        Array.isArray(record.answers) &&
        record.answers.length > 0 &&
        record.answers.every(
          (answer) =>
            answer &&
            typeof answer === "object" &&
            typeof answer.question === "string" &&
            typeof answer.answer === "string" &&
            (answer.id === undefined || typeof answer.id === "string"),
        )
      ) {
        return {
          answers: record.answers as QuestionAnswer[],
          ...(typeof record.elicitation_id === "string"
            ? { elicitationId: record.elicitation_id }
            : {}),
        };
      }
    }
  } catch {
    // Older receipts use readable question/answer lines.
  }
  return null;
}

/** Correlate a durable receipt with a later native echo or older delivery marker. */
export function sameQuestionAnswer(a: ParsedSystemMessage, b: ParsedSystemMessage): boolean {
  if (a.kind !== "question_answer" || b.kind !== "question_answer") return false;
  if (a.elicitationId && b.elicitationId && a.elicitationId !== b.elicitationId) return false;
  const aRef =
    a.cardRef ??
    (a.elicitationId?.startsWith("elicit_")
      ? `q${a.elicitationId.slice("elicit_".length, "elicit_".length + 6)}`
      : null);
  const bRef =
    b.cardRef ??
    (b.elicitationId?.startsWith("elicit_")
      ? `q${b.elicitationId.slice("elicit_".length, "elicit_".length + 6)}`
      : null);
  if (aRef && bRef && aRef !== bRef) return false;
  if (a.cardRef || b.cardRef) {
    if (!aRef || !bRef) return false;
    const rendered = (message: ParsedSystemMessage) =>
      message.cardRef
        ? message.body
        : message.answers?.map(({ question, answer }) => `${question} → ${answer}`).join("\n");
    return rendered(a) === rendered(b);
  }
  const keyed = Boolean(
    (a.elicitationId && b.elicitationId) ||
    (aRef && bRef) ||
    a.answers?.some((answer) => answer.id && b.answers?.some((other) => other.id === answer.id)),
  );
  if (!keyed) return false;
  return Boolean(
    a.answers?.length &&
    a.answers.length === b.answers?.length &&
    a.answers.every((answer) =>
      b.answers?.some((other) => answerMatches(answer, other, Boolean(aRef && bRef))),
    ),
  );
}

function answerMatches(a: QuestionAnswer, b: QuestionAnswer, allowMissingId = false): boolean {
  return (
    a.question === b.question &&
    a.answer === b.answer &&
    (allowMissingId ? !a.id || !b.id || a.id === b.id : a.id === b.id)
  );
}

/** A batched native echo is redundant only after every entry has a durable receipt. */
export function questionAnswerCoveredByReceipts(
  message: ParsedSystemMessage,
  receipts: ParsedSystemMessage[],
): boolean {
  if (message.cardRef) return receipts.some((receipt) => sameQuestionAnswer(message, receipt));
  if (message.kind !== "question_answer" || !message.answers?.length) return false;
  return message.answers.every((answer) =>
    receipts.some((receipt) => {
      if (receipt.kind !== "question_answer" || !receipt.elicitationId) return false;
      if (message.cardRef) {
        const ref = `q${receipt.elicitationId.replace(/^elicit_/, "").slice(0, 6)}`;
        if (ref !== message.cardRef) return false;
      }
      return receipt.answers?.some((candidate) =>
        answerMatches(answer, candidate, Boolean(message.cardRef)),
      );
    }),
  );
}

export function questionAnswerFromContent(
  content: MessageContentBlock[],
): ParsedSystemMessage | null {
  const marker = codexQuestionReplyMarkerContent(content);
  const text = (marker ?? content)
    .filter(isTextBlock)
    .map((block) => block.text)
    .join("");
  const parsed = parseSystemMessage(text);
  return parsed?.kind === "question_answer" ? parsed : null;
}

// Matches ChatPage's inline "[Attached: …]" marker stripper; the header check
// must see the same text extraction the bubble render uses.
const ATTACHED_RE = /\[Attached(?: file)?:\s*([^\]]*)\]\s*/g;

/**
 * True when a user-role message is actually a runtime `[System: …]` marker
 * (task/timer/sub-agent notice, interrupt record) rather than a real user
 * turn. Shared by the transcript's turn derivation and the rail's eager
 * history loader so both agree on what counts as a rail tick — a mismatch
 * (loader counting markers the rail drops) can wedge the rail hidden.
 *
 * :param content: A user message block's content array.
 * :returns: ``true`` for a system marker, ``false`` for a real user message.
 */
export function isSystemUserContent(content: MessageContentBlock[]): boolean {
  const hasAttachments = content.some((c) => c.type === "input_image" || c.type === "input_file");
  if (hasAttachments) return false;
  const text = content
    .filter(isTextBlock)
    .map((c) => c.text)
    .join("")
    .replace(ATTACHED_RE, "")
    .trim();
  return parseSystemMessage(text) !== null;
}

/**
 * Whether ``text`` is the ``<task-notification>`` block Claude Code injects as
 * a user-role entry when a background task finishes.
 *
 * :param text: One user message text block.
 * :returns: ``true`` for a task notification, ``false`` otherwise.
 */
export function isClaudeTaskNotificationText(text: string): boolean {
  const trimmed = text.trim();
  return (
    /^<task-notification>[\s\S]*<\/task-notification>$/.test(trimmed) &&
    TASK_NOTIFICATION_MARKERS.every((marker) => trimmed.includes(marker))
  );
}

/**
 * Re-label a Claude Code ``<task-notification>`` block as a ``[System: …]``
 * marker. Claude resumes on the notification with no human message in
 * between; the marker keeps that resume visible as a turn boundary, so the
 * answer Claude had already finished is not folded into the follow-up work.
 *
 * :param text: One user message text block.
 * :returns: Marker text (header plus the notification's summary as the
 *   body), or ``null`` when ``text`` is not a task notification.
 */
export function claudeTaskNotificationMarker(text: string): string | null {
  if (!isClaudeTaskNotificationText(text)) return null;
  // The header regex takes the id as one token; an id with inner whitespace
  // would otherwise stop the marker parsing as a background task.
  const rawId = /<task-id>([^<]*)<\/task-id>/.exec(text)?.[1]?.trim() ?? "";
  const taskId = rawId !== "" && !/\s/.test(rawId) ? rawId : "unknown";
  const rawStatus = /<status>([^<]*)<\/status>/.exec(text)?.[1]?.trim().toLowerCase();
  const status =
    rawStatus === "completed" || rawStatus === "failed" || rawStatus === "cancelled"
      ? rawStatus
      : "finished";
  const summary = /<summary>([\s\S]*?)<\/summary>/.exec(text)?.[1]?.trim() ?? "";
  const header = `[System: background task ${taskId} ${status}]`;
  return summary ? `${header}\n${summary}` : header;
}

/**
 * System-marker content for a user message that is a Claude task notification.
 *
 * :param content: A user message block's content array.
 * :returns: One ``input_text`` block carrying the marker, or ``null`` for an
 *   ordinary user message.
 */
export function taskNotificationMarkerContent(
  content: MessageContentBlock[],
): MessageContentBlock[] | null {
  if (content.some((block) => !isTextBlock(block))) return null;
  if (isClaudeAgentMessageContent(content)) return null;
  if (content.some((block) => isTextBlock(block) && !isClaudeTaskNotificationText(block.text))) {
    return null;
  }
  for (const block of content) {
    if (!isTextBlock(block)) continue;
    const marker = claudeTaskNotificationMarker(block.text);
    if (marker !== null) return [{ type: "input_text", text: marker }];
  }
  return null;
}

// Codex's async-question answer contract: one tag pair around a JSON
// array, one entry per answered question, the answer verbatim. The TUI
// writes it and the forwarder mirrors a web answer back in the same bytes.
const QUESTION_REPLY_OPEN = "<send_user_message_question_reply>";
const QUESTION_REPLY_CLOSE = "</send_user_message_question_reply>";
const ASYNC_QUESTION_TOOL = "request_user_input_async";

/**
 * Re-label a Codex async-question reply as a ``[System: …]`` marker.
 *
 * The reply is protocol text, not prose — without this it renders as the
 * raw tags. Classify strictly (exactly one tag pair, every entry carrying
 * a string answer/question and a well-formed ``questionItemId``) so a
 * user message that merely mentions the tags stays a normal bubble.
 *
 * :param text: One user message text block.
 * :returns: Marker text with structured answers and stable question ids,
 *   or ``null`` when ``text`` is not a well-formed reply.
 */
export function codexQuestionReplyMarker(text: string): string | null {
  const trimmed = text.trim();
  if (!trimmed.startsWith(QUESTION_REPLY_OPEN) || !trimmed.endsWith(QUESTION_REPLY_CLOSE)) {
    return null;
  }
  const inner = trimmed.slice(QUESTION_REPLY_OPEN.length, -QUESTION_REPLY_CLOSE.length).trim();
  let rawEntries: unknown;
  try {
    rawEntries = JSON.parse(inner);
  } catch {
    return null;
  }
  if (!Array.isArray(rawEntries) || rawEntries.length === 0) return null;
  const answers: QuestionAnswer[] = [];
  for (const raw of rawEntries) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
    const { answer, question, questionItemId } = raw as Record<string, unknown>;
    if (typeof answer !== "string" || typeof question !== "string") return null;
    let decoded: unknown = questionItemId;
    if (typeof decoded === "string") {
      try {
        decoded = JSON.parse(decoded);
      } catch {
        return null;
      }
    }
    if (
      !Array.isArray(decoded) ||
      decoded.length !== 3 ||
      decoded[0] !== ASYNC_QUESTION_TOOL ||
      typeof decoded[1] !== "string" ||
      decoded[1] === "" ||
      typeof decoded[2] !== "number" ||
      !Number.isInteger(decoded[2])
    ) {
      return null;
    }
    answers.push({ question, answer, id: `${decoded[1]}:${decoded[2]}` });
  }
  return `[System: Question answered]\n${JSON.stringify({ answers })}`;
}

/**
 * System-marker content for a user message that is a Codex question reply.
 *
 * :param content: A user message block's content array.
 * :returns: One ``input_text`` block carrying the marker, or ``null`` for an
 *   ordinary user message.
 */
export function codexQuestionReplyMarkerContent(
  content: MessageContentBlock[],
): MessageContentBlock[] | null {
  for (const block of content) {
    if (!isTextBlock(block)) continue;
    const marker = codexQuestionReplyMarker(block.text);
    if (marker !== null) return [{ type: "input_text", text: marker }];
  }
  return null;
}
