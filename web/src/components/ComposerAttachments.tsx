import { useEffect, useState } from "react";
import {
  Database,
  File as FileIcon,
  FileArchive,
  FileAudio,
  FileCode2,
  FileImage,
  FileJson,
  FileSpreadsheet,
  FileText,
  FileType,
  FileVideo,
  Presentation,
  XIcon,
  type LucideIcon,
} from "lucide-react";

import { attachmentFilename, attachmentKey } from "@/lib/attachments";
import { cn } from "@/lib/utils";
import { ZoomableImage } from "@/components/ImageLightbox";
import { ComposerChipRow } from "@/components/composer/ChatComposer";

/**
 * Pending (pre-send) attachments shown under the composer textarea. A supported
 * image renders as a square thumbnail you can click to view full-screen (via
 * the shared lightbox); anything else — and an image whose thumbnail fails to
 * load — renders as a card with a file-type icon, the filename, and a
 * "TYPE · SIZE" line. Shared by the chat composer and the new-chat dialog.
 */
export function ComposerAttachments({
  files,
  onRemove,
  className,
  badges,
  activeIndex,
  onTileHover,
  onBadgeClick,
}: {
  files: File[];
  onRemove: (index: number) => void;
  className?: string;
  /** Per-file token legend; a null entry renders no badge for that tile. */
  badges?: readonly ({ label: string; unreferenced: boolean } | null)[];
  /** Index of the tile whose token holds the caret; rings that tile. */
  activeIndex?: number | null;
  /** Hovering a tile highlights its token in the composer backdrop. */
  onTileHover?: (index: number | null) => void;
  /** Clicking a badge focuses the field holding its token. */
  onBadgeClick?: (index: number) => void;
}) {
  if (files.length === 0) return null;
  return (
    <ComposerChipRow className={className}>
      {files.map((file, i) => (
        <AttachmentTile
          key={attachmentKey(file)}
          file={file}
          onRemove={() => onRemove(i)}
          badge={badges?.[i] ?? null}
          active={activeIndex === i}
          onHover={onTileHover ? (hovering) => onTileHover(hovering ? i : null) : undefined}
          onBadgeClick={onBadgeClick ? () => onBadgeClick(i) : undefined}
        />
      ))}
    </ComposerChipRow>
  );
}

/** Human-readable file size, e.g. 6815744 -> "6.5 MB". */
export function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const kb = bytes / 1024;
  if (kb < 1024) return `${Math.round(kb)} KB`;
  return `${(kb / 1024).toFixed(1)} MB`;
}

// Lowercase extension -> Lucide icon. Grouped to match the design spec.
const EXTENSION_ICONS: Record<string, LucideIcon> = {
  pdf: FileText,
  doc: FileText,
  docx: FileText,
  txt: FileText,
  md: FileText,
  rtf: FileText,
  xls: FileSpreadsheet,
  xlsx: FileSpreadsheet,
  csv: FileSpreadsheet,
  tsv: FileSpreadsheet,
  ppt: Presentation,
  pptx: Presentation,
  key: Presentation,
  zip: FileArchive,
  rar: FileArchive,
  "7z": FileArchive,
  tar: FileArchive,
  gz: FileArchive,
  json: FileJson,
  jsonl: FileJson,
  js: FileCode2,
  jsx: FileCode2,
  ts: FileCode2,
  tsx: FileCode2,
  py: FileCode2,
  rb: FileCode2,
  go: FileCode2,
  rs: FileCode2,
  java: FileCode2,
  html: FileCode2,
  css: FileCode2,
  xml: FileCode2,
  yaml: FileCode2,
  yml: FileCode2,
  toml: FileCode2,
  sql: Database,
  db: Database,
  sqlite: Database,
  sqlite3: Database,
  ttf: FileType,
  otf: FileType,
  woff: FileType,
  woff2: FileType,
};

/** Icon for a file: match by extension first, then MIME prefix, else generic. */
export function iconForFile(file: File): LucideIcon {
  const name = file.name || "";
  const dot = name.lastIndexOf(".");
  const ext = dot >= 0 ? name.slice(dot + 1).toLowerCase() : "";
  if (ext && EXTENSION_ICONS[ext]) return EXTENSION_ICONS[ext];
  const type = file.type || "";
  if (type.startsWith("image/")) return FileImage;
  if (type.startsWith("video/")) return FileVideo;
  if (type.startsWith("audio/")) return FileAudio;
  return FileIcon;
}

/** Blob URL for an image preview, created and revoked inside one effect so the
 *  URL the committed <img> points at is never revoked early (StrictMode double
 *  mount) and never leaks. Guarded: jsdom (tests) lacks createObjectURL. */
function useObjectUrl(file: File | null): string | undefined {
  const [url, setUrl] = useState<string>();
  useEffect(() => {
    if (!file || typeof URL.createObjectURL !== "function") {
      setUrl(undefined);
      return;
    }
    const objectUrl = URL.createObjectURL(file);
    setUrl(objectUrl);
    return () => URL.revokeObjectURL(objectUrl);
  }, [file]);
  return url;
}

function AttachmentTile({
  file,
  onRemove,
  badge = null,
  active = false,
  onHover,
  onBadgeClick,
}: {
  file: File;
  onRemove: () => void;
  badge?: { label: string; unreferenced: boolean } | null;
  active?: boolean;
  onHover?: (hovering: boolean) => void;
  onBadgeClick?: () => void;
}) {
  // An image whose blob can't decode falls back to the file card (spec rule 4).
  const [thumbFailed, setThumbFailed] = useState(false);
  const showThumb = file.type.startsWith("image/") && !thumbFailed;
  const name = attachmentFilename(file);
  const url = useObjectUrl(showThumb ? file : null);
  const hoverProps = onHover
    ? { onMouseEnter: () => onHover(true), onMouseLeave: () => onHover(false) }
    : undefined;

  if (showThumb) {
    return (
      <div className="relative shrink-0" {...hoverProps}>
        <div
          className={cn(
            "size-14 overflow-hidden rounded-xl border border-border bg-muted",
            active && "ring-2 ring-ring",
          )}
        >
          <ZoomableImage
            src={url}
            alt={name}
            className="size-14 object-cover"
            onError={() => setThumbFailed(true)}
          />
        </div>
        <RemoveButton name={name} onRemove={onRemove} />
        <AttachmentBadge badge={badge} onClick={onBadgeClick} />
      </div>
    );
  }

  const dot = name.lastIndexOf(".");
  const ext = dot >= 0 ? name.slice(dot + 1).toUpperCase() : "";
  const meta = ext ? `${ext} · ${formatFileSize(file.size)}` : formatFileSize(file.size);
  const Icon = iconForFile(file);
  return (
    <div className="relative shrink-0" {...hoverProps}>
      <div
        className={cn(
          "flex h-14 w-[180px] items-center gap-2 rounded-xl border border-border bg-background p-2",
          active && "ring-2 ring-ring",
        )}
      >
        <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-muted text-muted-foreground">
          <Icon className="size-5" />
        </span>
        <span className="flex min-w-0 flex-col">
          <span className="truncate text-sm font-medium text-foreground">{name}</span>
          <span className="truncate text-xs text-muted-foreground">{meta}</span>
        </span>
      </div>
      <RemoveButton name={name} onRemove={onRemove} />
      <AttachmentBadge badge={badge} onClick={onBadgeClick} />
    </div>
  );
}

/** Token legend for one tile, rendered outside `ZoomableImage`'s button so an
 *  image click still opens the lightbox instead of triggering this badge. */
function AttachmentBadge({
  badge,
  onClick,
}: {
  badge: { label: string; unreferenced: boolean } | null;
  onClick?: () => void;
}) {
  if (!badge) return null;
  return (
    <button
      type="button"
      onClick={onClick}
      className="absolute -bottom-1 left-1 rounded-full border border-border bg-background px-1.5 py-0 text-[10px] text-muted-foreground shadow-sm"
    >
      {badge.label}
      {badge.unreferenced && <span className="text-muted-foreground/70"> · not in text</span>}
    </button>
  );
}

/** The 24px circular remove control, overlapping the tile's top-right corner. */
function RemoveButton({ name, onRemove }: { name: string; onRemove: () => void }) {
  return (
    <button
      type="button"
      onClick={onRemove}
      aria-label={`Remove ${name}`}
      className="absolute -top-1 -right-1 grid size-6 cursor-pointer place-items-center rounded-full border border-border bg-background text-muted-foreground shadow-sm hover:text-foreground"
    >
      <XIcon className="size-3.5" />
    </button>
  );
}
