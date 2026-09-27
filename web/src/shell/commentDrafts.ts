// Unsent comment drafts, keyed by conversation and the page the comment will be
// filed under. Module state, not React state: the composer unmounts when the
// viewer switches files, and the draft must survive that switch.

import type { ActiveSelection } from "./codeViewerHelpers";

export interface CommentDraft {
  start_index: number;
  end_index: number;
  anchor_content: string;
  body: string;
}

const drafts = new Map<string, CommentDraft>();

function draftKey(conversationId: string, path: string): string {
  return `${conversationId}\u0000${path}`;
}

/** The unsent draft for one page, or undefined when there is none. */
export function getCommentDraft(conversationId: string, path: string): CommentDraft | undefined {
  return drafts.get(draftKey(conversationId, path));
}

/** Record the draft for one page; an empty body drops it instead. */
export function setCommentDraft(conversationId: string, path: string, draft: CommentDraft): void {
  const key = draftKey(conversationId, path);
  if (draft.body.trim()) drafts.set(key, draft);
  else drafts.delete(key);
}

/** Drop the draft — its comment was posted, or the composer went away. */
export function clearCommentDraft(conversationId: string, path: string): void {
  drafts.delete(draftKey(conversationId, path));
}

/** The composer anchor a draft was typed against. */
export function draftSelection(draft: CommentDraft): ActiveSelection {
  return {
    start_index: draft.start_index,
    end_index: draft.end_index,
    anchor_content: draft.anchor_content,
  };
}
