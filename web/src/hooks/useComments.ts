import {
  isLocalReviewComment,
  sameLocalReviewAnchor,
  type LocalReviewAnchor,
} from "@/lib/localReviewComments";
import { removeLocalReviewContext, updateLocalReviewContext } from "@/lib/localReviewContext";
// TanStack Query hooks for the comments API:
// POST/GET/PATCH/DELETE /v1/sessions/{id}/comments
// POST /v1/sessions/{id}/comments/send
//
// `useSendCommentsToAgent` calls `useChatStore.send()` directly on success
// so the message is submitted immediately without requiring a manual send.
// It requires a non-null `agentId`; for the FileViewer case where an
// agent may not be registered, see `CommentSenderProvider` /
// `useOptionalCommentSender` in `CommentSenderContext.tsx`.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { readAlwaysSteer } from "@/lib/alwaysSteerPreferences";
import { authenticatedFetch } from "@/lib/identity";
import { shouldQueueSend } from "@/lib/messageQueue";
import { captureQuestionCardInputTarget, insertIntoQuestionCard } from "@/lib/questionCardInput";
import { useChatStore } from "@/store/chatStore";

export interface Comment {
  id: string;
  conversation_id: string;
  path: string;
  /** 0-based absolute character offset (inclusive) within the file. */
  start_index: number;
  /** 0-based absolute character offset (exclusive) within the file. */
  end_index: number;
  body: string;
  status: "draft" | "addressed";
  created_at: number;
  /**
   * Unix **microseconds** of the last body/status mutation (set at creation
   * when never edited). Used for change detection, not display; divide by
   * 1000 before passing to `new Date()` if it's ever rendered.
   */
  updated_at: number;
  anchor_content: string | null;
  created_by: string | null;
  screenshot_expires_at?: number;
  anchor_metadata?: LocalReviewAnchor | null;
}

// ── Query helpers ────────────────────────────────────────────────────────────

// Exported for `useCommentInbox`, which mounts the same queries for
// every session with comments — sharing this key keeps its cache and
// the SessionUpdatesProvider fingerprint invalidation in sync.
export function commentsQueryKey(sessionId: string, path?: string) {
  return path ? ["comments", sessionId, path] : ["comments", sessionId];
}

export async function fetchComments(
  sessionId: string,
  path?: string,
  signal?: AbortSignal,
): Promise<Comment[]> {
  // Visitor drafts are hidden from the agent-facing read path unless the
  // owner's client asks for them; the owner's panels and inbox need to see
  // them before they are sent.
  const url = path
    ? `/v1/sessions/${encodeURIComponent(sessionId)}/comments?include_visitor_drafts=1&path=${encodeURIComponent(path)}`
    : `/v1/sessions/${encodeURIComponent(sessionId)}/comments?include_visitor_drafts=1`;
  const pageUrl = `${url}&include_pr_comments=true&limit=100`;
  const comments: Comment[] = [];
  let cursor: string | null = null;
  do {
    // eslint-disable-next-line no-await-in-loop -- Each page needs the previous cursor.
    const res = await authenticatedFetch(
      `${pageUrl}${cursor ? `&after=${encodeURIComponent(cursor)}` : ""}`,
      { signal },
    );
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    // eslint-disable-next-line no-await-in-loop -- Decode this page before following its cursor.
    const page: Comment[] | { comments: Comment[]; next_cursor: string | null } = await res.json();
    // Older servers ignore the opt-in and return their Files-only array.
    if (Array.isArray(page)) return page;
    comments.push(...page.comments);
    cursor = page.next_cursor;
  } while (cursor);
  return comments.sort(
    (left, right) => left.created_at - right.created_at || left.id.localeCompare(right.id),
  );
}

// ── Hooks ────────────────────────────────────────────────────────────────────

/**
 * Fetch comments for a session, optionally filtered to a single file.
 *
 * Disabled when `sessionId` is falsy.
 */
export function useComments(sessionId: string | undefined, path?: string) {
  return useQuery({
    queryKey: commentsQueryKey(sessionId ?? "", path),
    queryFn: ({ signal }) => fetchComments(sessionId!, path, signal),
    enabled: !!sessionId,
    staleTime: 2_000,
  });
}

export interface AddCommentPayload {
  path: string;
  start_index: number;
  end_index: number;
  body: string;
  anchor_content?: string | null;
  anchor_metadata?: LocalReviewAnchor;
}

export async function createComment(
  sessionId: string,
  payload: AddCommentPayload,
): Promise<Comment> {
  const response = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(sessionId)}/comments`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  const comment: Comment = await response.json();
  if (
    payload.anchor_metadata &&
    (!isLocalReviewComment(comment) ||
      comment.conversation_id !== sessionId ||
      comment.path !== payload.path ||
      !sameLocalReviewAnchor(payload.anchor_metadata, comment.anchor_metadata!))
  ) {
    const message = "This server did not preserve the review location. Your draft is saved.";
    // A mixed-version deployment may accept the request as an ordinary Files comment.
    try {
      if (!comment?.id) throw new Error("Missing comment ID");
      await deleteComment(sessionId, comment.id);
    } catch {
      throw new Error(`${message} A comment may remain in Files; remove it before retrying.`);
    }
    throw new Error(`${message} Update the server before retrying.`);
  }
  return comment;
}

async function deleteComment(sessionId: string, commentId: string): Promise<void> {
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(sessionId)}/comments/${encodeURIComponent(commentId)}`,
    { method: "DELETE" },
  );
  if (!res.ok && res.status !== 404) throw new Error(`${res.status} ${res.statusText}`);
}

/** POST /v1/sessions/{id}/comments */
export function useAddComment(sessionId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: AddCommentPayload) => createComment(sessionId, payload),
    onSuccess: (comment) => {
      // Invalidate both the full-session list and the per-file list
      // so the sidebar and any per-file views refresh.
      void queryClient.invalidateQueries({
        queryKey: ["comments", sessionId],
      });
      void queryClient.invalidateQueries({
        queryKey: ["comments", sessionId, comment.path],
      });
    },
  });
}

/** DELETE /v1/sessions/{id}/comments/{commentId} */
export function useDeleteComment(sessionId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (commentId: string) => deleteComment(sessionId, commentId),
    onSuccess: (_, commentId) => {
      removeLocalReviewContext(sessionId, commentId);
      void queryClient.invalidateQueries({
        queryKey: ["comments", sessionId],
      });
    },
  });
}

/** PATCH /v1/sessions/{id}/comments/{commentId} */
export function useUpdateComment(sessionId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (payload: { commentId: string; status?: string; body?: string }) => {
      const { commentId, ...fields } = payload;
      const res = await authenticatedFetch(
        `/v1/sessions/${encodeURIComponent(sessionId)}/comments/${encodeURIComponent(commentId)}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(fields),
        },
      );
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      const comment: Comment = await res.json();
      if (comment.id !== commentId || comment.conversation_id !== sessionId) {
        throw new Error(
          "This server returned a different comment. Your attached context is unchanged.",
        );
      }
      return comment;
    },
    onSuccess: (comment) => {
      updateLocalReviewContext(comment);
      void queryClient.invalidateQueries({
        queryKey: ["comments", sessionId],
      });
    },
  });
}

export interface CommentAttachment {
  comment_id: string;
  file_id: string;
  filename: string;
}

export interface SendCommentsPayload {
  comment_ids: string[];
  instruction?: string;
  respectQueue?: boolean;
}

/**
 * POST /v1/sessions/{id}/comments/send
 *
 * Requires a non-null `agentId` — without an agent there is nowhere to
 * dispatch the formatted message. Callers that may not have an agent
 * should mount this hook only inside `CommentSenderProvider`, which
 * skips the mutation entirely when no agent is registered. Consumers
 * then read the sender via `useOptionalCommentSender()` and treat
 * `null` as "no agent, hide the button".
 *
 * `respectQueue` mirrors the composer's send decision for annotation batches:
 * it refuses a send from a session the user has navigated away from, and
 * queues when the agent is busy (or has queued messages). Without it the
 * behaviour is the historical direct send (Address All, Archived banner).
 */
export function useSendCommentsToAgent(sessionId: string, agentId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (payload: SendCommentsPayload) => {
      const { respectQueue, ...body } = payload;
      const intendedCard = captureQuestionCardInputTarget(sessionId);
      const res = await authenticatedFetch(
        `/v1/sessions/${encodeURIComponent(sessionId)}/comments/send`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          // Format the message without marking: the comments become
          // addressed only once the agent actually received it.
          body: JSON.stringify({ ...body, mark_addressed: false }),
        },
      );
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      const data = (await res.json()) as {
        formatted_message: string;
        sent_comment_ids: string[];
        attachments?: CommentAttachment[];
      };
      const attachments = data.attachments ?? [];
      // Fetch every stored screenshot before dispatching: a missing image
      // aborts the whole send, so numbering never drifts from what the agent
      // receives. Promise.all preserves the reply's image order in `files`.
      const files = await Promise.all(
        attachments.map(async (attachment, index) => {
          try {
            const fileRes = await authenticatedFetch(
              `/v1/sessions/${encodeURIComponent(sessionId)}/resources/files/${encodeURIComponent(attachment.file_id)}/content`,
            );
            if (!fileRes.ok) throw new Error(`${fileRes.status} ${fileRes.statusText}`);
            const blob = await fileRes.blob();
            return new File([blob], attachment.filename, {
              type: fileRes.headers.get("Content-Type") ?? "application/octet-stream",
            });
          } catch {
            throw new Error(`Couldn't attach screenshot ${index + 1}; nothing was sent`);
          }
        }),
      );
      const cardInsert = insertIntoQuestionCard(
        sessionId,
        data.formatted_message,
        files,
        data.sent_comment_ids,
        intendedCard,
      );
      if (cardInsert === "inserted") {
        return { ...data, attachments, delivered: true, staged: true };
      }
      if (cardInsert === "rejected") {
        throw new Error("The question answer could not accept these comments. Nothing was sent.");
      }
      const chat = useChatStore.getState();
      if (respectQueue) {
        if (chat.conversationId !== sessionId) {
          throw new Error("Return to this session before sending.");
        }
        if (
          shouldQueueSend(
            sessionId,
            chat.status,
            chat.sessionStatus,
            chat.queuedMessages,
            readAlwaysSteer(),
          )
        ) {
          chat.enqueueMessage(data.formatted_message, files);
          await markSentCommentsAddressed(sessionId, data.sent_comment_ids);
          await queryClient.invalidateQueries({ queryKey: ["comments", sessionId] });
          return { ...data, attachments, delivered: true };
        }
      }
      // Pin delivery to the session whose comments were formatted, so a send
      // that resolves after the user switched chats still marks the right
      // comments only when it landed there.
      const delivered = await chat.send(data.formatted_message, agentId, files, {
        pinnedConversationId: sessionId,
      });
      if (!delivered) return { ...data, attachments, delivered };
      await markSentCommentsAddressed(sessionId, data.sent_comment_ids);
      await queryClient.invalidateQueries({
        queryKey: ["comments", sessionId],
      });
      return { ...data, attachments, delivered };
    },
  });
}

async function markSentCommentsAddressed(sessionId: string, commentIds: string[]): Promise<void> {
  await Promise.all(
    commentIds.map(async (commentId) => {
      const patch = await authenticatedFetch(
        `/v1/sessions/${encodeURIComponent(sessionId)}/comments/${encodeURIComponent(commentId)}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ status: "addressed" }),
        },
      );
      if (!patch.ok) throw new Error(`${patch.status} ${patch.statusText}`);
    }),
  );
}
