// Renders an inbound peer message (`web/src/lib/peerMessage.ts`) — a chat
// turn injected by another Omnigent session via `sys_session_send`. A real
// turn input (the receiving agent replies to it), so it keeps the ordinary
// user-bubble shape; only the origin badge distinguishes it from a message
// the session's own user typed.

import { Message, MessageContent } from "@/components/ai-elements/message";
import { FilePathAwareMessageResponse } from "@/components/blocks/BlockRenderer";
import type { ParsedPeerMessage } from "@/lib/peerMessage";

const TITLE_TRUNCATE_LENGTH = 48;

function truncateTitle(title: string): string {
  return title.length > TITLE_TRUNCATE_LENGTH
    ? `${title.slice(0, TITLE_TRUNCATE_LENGTH)}…`
    : title;
}

interface PeerMessageViewProps {
  message: ParsedPeerMessage;
}

export function PeerMessageView({ message }: PeerMessageViewProps) {
  const badge = [truncateTitle(message.title), message.agent, message.projectId]
    .filter((part): part is string => Boolean(part))
    .join(" · ");
  return (
    <Message
      from="user"
      data-testid="peer-message"
      data-peer-sender={message.senderId}
      className="max-w-[640px]"
    >
      <div className="ml-auto flex w-fit max-w-full flex-col items-end">
        <span
          title={message.senderId}
          className="mb-1 mr-1 text-sm text-muted-foreground"
        >
          From session {badge}
        </span>
        <MessageContent>
          <FilePathAwareMessageResponse breaks>{message.body}</FilePathAwareMessageResponse>
        </MessageContent>
      </div>
    </Message>
  );
}
