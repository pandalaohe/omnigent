// Cards for one sent batch of element annotations. `UserBubble` swaps its
// ordinary block list for these when `parseAnnotationMessage` matches, so the
// raw `_format_message` envelope (untrusted evidence included) stays behind
// the "Show full message" disclosure. Labels and bodies render as plain text;
// only `fullText` goes through the Markdown renderer, with the same
// text-treatment options `UserBubble` uses.
import { useState } from "react";
import { ImageIcon } from "lucide-react";
import { FilePathAwareMessageResponse } from "@/components/blocks/BlockRenderer";
import { InlineImage, SessionImage } from "@/components/SessionImage";
import { imagePreview, type ImageContentBlock } from "@/lib/blocks";
import type { MessageResponseProps } from "@/components/ai-elements/message";
import type { ParsedAnnotationItem } from "@/lib/annotationMessage";

function thumbnailPath(sessionId: string, fileId: string): string {
  return `/v1/sessions/${encodeURIComponent(sessionId)}/resources/files/${encodeURIComponent(fileId)}/content`;
}

function ImageThumbnail({ block, sessionId }: { block: ImageContentBlock; sessionId?: string }) {
  const preview = imagePreview(block);
  if (preview.kind === "uploaded") {
    return (
      <SessionImage
        path={sessionId ? thumbnailPath(sessionId, preview.fileId) : undefined}
        alt={preview.alt}
        className="max-h-24 rounded-md object-contain"
      />
    );
  }
  if (preview.kind === "inline") {
    return (
      <InlineImage
        src={preview.src}
        alt={preview.alt}
        className="max-h-24 rounded-md object-contain"
      />
    );
  }
  return (
    <span className="flex items-center gap-1 rounded-md border border-border bg-muted px-2 py-1 text-xs text-muted-foreground">
      <ImageIcon className="size-3 shrink-0" />
      <span className="max-w-[180px] truncate">{preview.label}</span>
    </span>
  );
}

/**
 * The per-annotation card list plus the disclosure for the raw message.
 *
 * :param items: Parsed element annotations, in message order.
 * :param images: The bubble's `input_image` blocks; `item.imageIndex` is
 *   1-based into this list.
 * :param sessionId: Session the screenshots are fetched from, when known.
 * :param pending: True while the message is still sending.
 * :param fullText: The original message, shown by the disclosure.
 * :param remarkRehypeOptions: Text-treatment options for `fullText`, supplied
 *   by the caller so user-authored markup renders literally.
 */
export function AnnotationCards({
  items,
  images,
  sessionId,
  pending,
  fullText,
  remarkRehypeOptions,
}: {
  items: ParsedAnnotationItem[];
  images: ImageContentBlock[];
  sessionId?: string;
  pending: boolean;
  fullText: string;
  remarkRehypeOptions: MessageResponseProps["remarkRehypeOptions"];
}) {
  const [showFull, setShowFull] = useState(false);
  return (
    <div>
      <div className="flex flex-col gap-1.5">
        {items.map((item) => {
          const image = item.imageIndex === null ? undefined : images[item.imageIndex - 1];
          return (
            <div
              key={item.n}
              data-testid="annotation-card"
              className="flex items-start gap-2 rounded-md border border-border px-2 py-1.5"
            >
              {image && (
                <div className="shrink-0">
                  <ImageThumbnail block={image} sessionId={sessionId} />
                </div>
              )}
              <div className="min-w-0 flex-1">
                <div className="flex items-baseline gap-2">
                  <code
                    data-testid="annotation-label"
                    title={item.label}
                    className="min-w-0 flex-1 truncate font-mono text-xs text-muted-foreground"
                  >
                    {item.label}
                  </code>
                  <span className="shrink-0 text-[11px] text-muted-foreground">
                    {pending ? "Sending" : "Sent"}
                  </span>
                </div>
                <div data-testid="annotation-body" className="whitespace-pre-wrap text-sm">
                  {item.body}
                </div>
              </div>
            </div>
          );
        })}
      </div>
      <button
        type="button"
        onClick={() => setShowFull((current) => !current)}
        className="mt-1.5 text-xs text-muted-foreground hover:text-foreground transition-colors"
      >
        {showFull ? "Hide full message" : "Show full message"}
      </button>
      {showFull && (
        <FilePathAwareMessageResponse
          breaks
          mode="static"
          remarkRehypeOptions={remarkRehypeOptions}
        >
          {fullText}
        </FilePathAwareMessageResponse>
      )}
    </div>
  );
}
