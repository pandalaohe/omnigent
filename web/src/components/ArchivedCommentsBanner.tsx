// Banner above the composer of an archived session that still holds
// unhandled comments. An archived session's link is dead and its runner is
// stopped, so the comments can only move forward by hand: copy the formatted
// prompt, or start a fresh session continuing the archived one and deliver
// the prompt there. Comments are marked addressed only after the agent
// actually received the message.

import { useCallback, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { copyText } from "@/lib/clipboard";
import { unhandledCommentCounts } from "@/lib/comments";
import { authenticatedFetch } from "@/lib/identity";
import { useNavigate } from "@/lib/routing";
import { continueArchivedSession } from "@/lib/sessionsApi";
import { useChatStore } from "@/store/chatStore";
import type { Comment } from "@/hooks/useComments";

interface ArchivedCommentsBannerProps {
  sessionId: string;
  /** Session title, quoted in the continuation prefix. */
  title: string;
  /** Launch directory the new session continues in. */
  directory: string;
  /** Bound agent of the archived session; null hides nothing but disables Continue. */
  agentId: string | null;
  comments: Comment[];
}

/** Mark the delivered comments addressed on the archived session. */
async function markAddressed(sessionId: string, commentIds: string[]): Promise<void> {
  await Promise.all(
    commentIds.map(async (commentId) => {
      const res = await authenticatedFetch(
        `/v1/sessions/${encodeURIComponent(sessionId)}/comments/${encodeURIComponent(commentId)}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ status: "addressed" }),
        },
      );
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    }),
  );
}

export function ArchivedCommentsBanner({
  sessionId,
  title,
  directory,
  agentId,
  comments,
}: ArchivedCommentsBannerProps) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const { total, visitors } = unhandledCommentCounts(comments);

  const formatPrompt = useCallback(async () => {
    const draftIds = comments.filter((comment) => comment.status === "draft").map((c) => c.id);
    const res = await authenticatedFetch(
      `/v1/sessions/${encodeURIComponent(sessionId)}/comments/send`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        // Format without marking: the comments become addressed only after
        // the agent actually received the message.
        body: JSON.stringify({ comment_ids: draftIds, mark_addressed: false }),
      },
    );
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    const data = (await res.json()) as {
      formatted_message: string;
      sent_comment_ids: string[];
    };
    return {
      prompt: `Continuing from archived session "${title}" (${sessionId}) in ${directory}. ${data.formatted_message}`,
      sentCommentIds: data.sent_comment_ids,
    };
  }, [comments, directory, sessionId, title]);

  if (total === 0) return null;

  const handleCopy = async () => {
    if (busy) return;
    setBusy(true);
    try {
      const { prompt } = await formatPrompt();
      await copyText(prompt);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      toast.error("Couldn't copy the comments as a prompt");
    } finally {
      setBusy(false);
    }
  };

  const handleContinue = async () => {
    if (busy || agentId === null) return;
    setBusy(true);
    try {
      const { prompt, sentCommentIds } = await formatPrompt();
      const session = await continueArchivedSession(sessionId);
      navigate(`/c/${session.id}`);
      // Pin delivery to the new session: this send outlives the navigation,
      // and the archived session's comments must only be marked once it
      // landed. A failed send leaves the prompt in the new session's
      // composer, so the user can resend it.
      const delivered = await useChatStore
        .getState()
        .send(prompt, agentId, [], { pinnedConversationId: session.id });
      if (!delivered) {
        toast.error("The message didn't reach the agent; the comments stay unhandled.");
        return;
      }
      await markAddressed(sessionId, sentCommentIds);
      await queryClient.invalidateQueries({ queryKey: ["comments", sessionId] });
    } catch {
      // Nothing was marked: the comments stay unhandled and Continue can be
      // pressed again (the server returns the same new session).
      toast.error("Couldn't continue in a new session");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mx-3 mb-2 flex flex-wrap items-center gap-2 rounded-lg border border-border bg-muted/40 px-3 py-2">
      <span className="min-w-0 flex-1 text-sm text-muted-foreground">
        {total} unhandled comments ({visitors} from visitors)
      </span>
      <Button
        type="button"
        variant="outline"
        size="sm"
        disabled={busy}
        onClick={() => void handleCopy()}
      >
        {copied ? "Copied" : "Copy as prompt"}
      </Button>
      <Button
        type="button"
        size="sm"
        disabled={busy || agentId === null}
        onClick={() => void handleContinue()}
      >
        Continue in a new session
      </Button>
    </div>
  );
}
