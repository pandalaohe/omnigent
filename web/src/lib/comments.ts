// Owner-facing comment display helpers. Visitor comments carry a
// `visitor:<name>` author marker (the server stamps it on posts through a
// shared artifact link); these helpers map that marker to the label the
// owner sees and derive the counts shown before a delete.

const VISITOR_AUTHOR_PREFIX = "visitor:";

/** Whether a comment's `created_by` marks it as visitor-authored. */
export function isVisitorAuthor(createdBy: string | null | undefined): boolean {
  return typeof createdBy === "string" && createdBy.startsWith(VISITOR_AUTHOR_PREFIX);
}

/**
 * Display label for a comment author. Visitor markers render as
 * `"Visitor · <name>"` (or plain `"Visitor"` when unnamed); every other
 * author is shown as-is, and an authorless row falls back to `"You"` —
 * the single-user/local display the panel has always used.
 */
export function commentAuthorLabel(createdBy: string | null | undefined): string {
  if (isVisitorAuthor(createdBy)) {
    const name = (createdBy as string).slice(VISITOR_AUTHOR_PREFIX.length);
    return name ? `Visitor · ${name}` : "Visitor";
  }
  return createdBy ?? "You";
}

interface CommentStatusRow {
  status: string;
  created_by: string | null;
}

/** Draft total and visitor-draft subtotal for a comment list. */
export function unhandledCommentCounts(comments: readonly CommentStatusRow[]): {
  total: number;
  visitors: number;
} {
  const drafts = comments.filter((comment) => comment.status === "draft");
  return {
    total: drafts.length,
    visitors: drafts.filter((comment) => isVisitorAuthor(comment.created_by)).length,
  };
}

/**
 * The single-session delete warning, or `null` when the session holds no
 * draft comments (the caller then omits the line entirely).
 */
export function unhandledCommentsDeleteLine(comments: readonly CommentStatusRow[]): string | null {
  const { total, visitors } = unhandledCommentCounts(comments);
  if (total === 0) return null;
  return `${total} unhandled comments (${visitors} from visitors) will be deleted.`;
}

/** The bulk-delete warning, or `null` when the sessions hold no comments. */
export function bulkCommentsDeleteLine(totalComments: number): string | null {
  if (totalComments <= 0) return null;
  return `These sessions hold ${totalComments} comments; they will be deleted.`;
}
