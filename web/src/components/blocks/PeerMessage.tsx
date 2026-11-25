// Renders an inbound peer message (`web/src/lib/peerMessage.ts`) — a chat
// turn injected by another Omnigent session via `sys_session_send`. A real
// turn input (the receiving agent replies to it), so it keeps the ordinary
// user-bubble shape; only the origin badge distinguishes it from a message
// the session's own user typed.

import { Message, MessageContent } from "@/components/ai-elements/message";
import { FilePathAwareMessageResponse } from "@/components/blocks/BlockRenderer";
import type { ParsedPeerMessage } from "@/lib/peerMessage";
import { ChevronUpIcon } from "lucide-react";

const TITLE_TRUNCATE_LENGTH = 48;

function truncateTitle(title: string): string {
  return title.length > TITLE_TRUNCATE_LENGTH ? `${title.slice(0, TITLE_TRUNCATE_LENGTH)}…` : title;
}

interface PeerMessageViewProps {
  message: ParsedPeerMessage;
  onCollapse?: () => void;
}

export function PeerMessageView({ message, onCollapse }: PeerMessageViewProps) {
  const badge = [truncateTitle(message.title), message.agent, message.projectId]
    .filter((part): part is string => Boolean(part))
    .join(" · ");
  const label = `From session ${badge}`;
  return (
    <Message
      from="user"
      data-testid="peer-message"
      data-peer-sender={message.senderId}
      className="max-w-[640px]"
    >
      <div className="ml-auto flex w-fit max-w-full flex-col items-end">
        <div className="mb-1 mr-1 flex items-center gap-2 text-sm text-muted-foreground">
          <span title={message.senderId}>{label}</span>
          {onCollapse && (
            <button
              type="button"
              aria-expanded
              aria-label="Collapse message"
              data-testid="collapse-session-message"
              className="transition-colors hover:text-foreground"
              onClick={onCollapse}
            >
              <ChevronUpIcon className="size-3" />
            </button>
          )}
        </div>
        <MessageContent>
          <FilePathAwareMessageResponse breaks>{message.body}</FilePathAwareMessageResponse>
        </MessageContent>
      </div>
    </Message>
  );
}
