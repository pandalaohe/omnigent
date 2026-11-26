import type { Comment } from "@/hooks/useComments";
import {
  expandMentionTokens,
  withSessionMentionHint,
  type InlineMention,
} from "@/lib/composerMentions";
import { formatLocalReviewComments } from "@/lib/localReviewComments";
import { planComposerSend } from "@/lib/composerTokens";
import {
  appendReplyDraft,
  restoreReplyDraft,
  serializeReplyDraft,
  snapshotReplyDraft,
  type ReplyDraft,
} from "@/lib/replyDraft";

/** What a composer send carries besides its text. */
export interface ComposerSendContext {
  /** Tagged files and sessions whose tokens are still in the draft. */
  mentions: readonly InlineMention[];
  /** Local review comments attached to the draft. */
  reviewComments: Comment[];
}

/**
 * Whether a send carries more than a bare command: a quote, an attachment, a
 * tag or a review comment makes slash-command text an ordinary message.
 */
export function carriesMessageContext(
  draft: ReplyDraft,
  files: readonly File[],
  { mentions, reviewComments }: ComposerSendContext,
): boolean {
  return (
    draft.quotes.length > 0 || files.length > 0 || mentions.length > 0 || reviewComments.length > 0
  );
}

/**
 * The message a composer sends: each tag token becomes its marker where it
 * sits, review comments follow the authored text, and a tagged session adds
 * one tool hint at the end. *plainText* is the text sent when there are no
 * quotes; with quotes, the draft's own text and quote preambles are used.
 */
export function buildOutgoingMessage(
  draft: ReplyDraft,
  plainText: string,
  { mentions, reviewComments }: ComposerSendContext,
  files: readonly File[],
): ReturnType<typeof planComposerSend> {
  const hasAuthoredText = !!draft.text.trim() || draft.quotes.some((quote) => quote.before.trim());
  const reviewContext =
    reviewComments.length > 0
      ? formatLocalReviewComments(reviewComments, { includeInstruction: !hasAuthoredText })
      : "";
  const tagsSessions = mentions.some((mention) => mention.marker.startsWith("[Session: "));
  const plan = planComposerSend(
    { ...draft, text: withSessionMentionHint(draft.text, tagsSessions) },
    files,
    "",
    withSessionMentionHint(plainText, tagsSessions),
    { transformText: (text) => expandMentionTokens(text, mentions) },
  );
  if (!reviewContext) return plan;
  // Generated review text cannot claim an authored attachment token.
  const outgoing = appendReplyDraft(restoreReplyDraft(plan.text, plan.replyDraft), {
    quotes: [],
    text: reviewContext,
  });
  const text = serializeReplyDraft(outgoing);
  return {
    ...plan,
    text,
    replyDraft: snapshotReplyDraft(outgoing),
    parts: plan.parts
      ? [...plan.parts, { type: "text", text: text.slice(plan.text.length) }]
      : undefined,
  };
}
