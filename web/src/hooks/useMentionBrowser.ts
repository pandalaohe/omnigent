import { type RefObject, useRef, useState } from "react";

import {
  memberMentionInsertion,
  type MentionItem,
  type MentionState,
} from "@/lib/composerMentions";
import type { SessionMember } from "@/lib/sessionMembers";
import { eventMatchesShortcutAction } from "@/lib/keyboardShortcutPreferences";
import { composerAttachmentKey } from "@/store/chatStore";
import type { WorkspaceFile } from "@/hooks/useWorkspaceChangedFiles";

/**
 * Inputs the host composer supplies. The data source (workspace API vs. host
 * filesystem) and the mention-token state live in the composer — only the
 * stateful glue (selection index, tagged chips, attach/drill/remove handlers,
 * keyboard navigation, top-row preselect) is shared here, so the two composers
 * can't drift.
 */
export interface MentionBrowserParams {
  /** Active mention token, owned by the composer (recomputed on text change). */
  mention: MentionState | null;
  /** Clear or replace the active token (e.g. on attach, drill, or dismiss). */
  setMention: (next: MentionState | null) => void;
  /** Current directory's entries — already filtered, folders-first, capped. */
  mentionEntries: WorkspaceFile[];
  /**
   * Member rows offered ahead of the file rows (2+ member sessions, already
   * filtered). Picking one inserts ``@role `` as plain text — a routing name
   * for the lead, not an attachment chip.
   */
  mentionMembers?: readonly SessionMember[];
  /** The textarea value and a setter (which may also flag the draft dirty). */
  text: string;
  setText: (next: string) => void;
  textareaRef: RefObject<HTMLTextAreaElement | null>;
  /** On mobile, Enter inserts a newline rather than acting on the menu. */
  isMobile?: boolean;
}

export interface MentionBrowser {
  mentionIndex: number;
  mentionOpen: boolean;
  mentionedItems: MentionItem[];
  setMentionedItems: React.Dispatch<React.SetStateAction<MentionItem[]>>;
  /** Attach a file (isDir=false) or whole folder (isDir=true) as a chip. */
  attachMention: (path: string, isDir: boolean) => void;
  /** Insert ``@role `` for a member row, closing the menu. */
  attachMember: (role: string) => void;
  /** Drill into a folder: rewrite the token to ``@<dir>/`` and keep browsing. */
  openMentionDir: (path: string) => void;
  removeMentionedItem: (index: number) => void;
  /** Handle a key event for the open menu; returns true when it consumed it. */
  handleKeyDown: (e: MentionKeyboardEvent) => boolean;
  /** Dismiss the menu (e.g. on blur). */
  dismiss: () => void;
}

export type MentionKeyboardEvent = Pick<
  KeyboardEvent,
  "code" | "key" | "ctrlKey" | "metaKey" | "altKey" | "shiftKey" | "preventDefault"
>;

/**
 * Shared ``@``-mention controller for the in-session composer and the
 * new-session launcher. Owns the selection index, the tagged-chip list, and
 * the attach/drill/remove + keyboard behaviour; the composer owns the token
 * state and supplies the listings (its data sources differ). Member rows, when
 * supplied, sit ahead of the file rows in the same index space so one
 * Arrow/Enter path covers both sections.
 */
export function useMentionBrowser(params: MentionBrowserParams): MentionBrowser {
  const {
    mention,
    setMention,
    mentionEntries,
    mentionMembers = [],
    text,
    setText,
    textareaRef,
    isMobile = false,
  } = params;
  const [mentionIndex, setMentionIndex] = useState(-1);
  const [mentionedItems, setMentionedItems] = useState<MentionItem[]>([]);
  const memberRowCount = mentionMembers.length;
  const rowCount = memberRowCount + mentionEntries.length;
  const mentionOpen = rowCount > 0;

  // Pre-select the top row whenever the listing changes — lets Enter/Tab act on
  // the top hit without arrowing first. Keyed by type+path so a file and a dir
  // of the same name stay distinct, and by role so member rows can't collide
  // with a file named like a role. (Render-phase state adjustment, the React
  // "store-previous-props" pattern — mirrors the slash menu's reset.)
  const prevMentionMatchesRef = useRef<string[]>([]);
  const mentionEntryKeys = [
    ...mentionMembers.map((member) => `member:${member.role}`),
    ...mentionEntries.map((e) => `${e.type}:${e.path}`),
  ];
  if (
    mentionEntryKeys.length !== prevMentionMatchesRef.current.length ||
    mentionEntryKeys.some((k, i) => k !== prevMentionMatchesRef.current[i])
  ) {
    prevMentionMatchesRef.current = mentionEntryKeys;
    setMentionIndex(mentionEntryKeys.length > 0 ? 0 : -1);
  }

  const attachMention = (path: string, isDir: boolean) => {
    if (!mention) return;
    setText(text.slice(0, mention.start) + text.slice(mention.end));
    // Dedup on the shared attachment key (path + dir-ness + range) — the same
    // identity the store queue uses — so the "@" menu and the file viewer's
    // "Attach to agent" never disagree about what counts as a duplicate.
    const item: MentionItem = { path, isDir };
    const itemKey = composerAttachmentKey(item);
    setMentionedItems((prev) =>
      prev.some((it) => composerAttachmentKey(it) === itemKey) ? prev : [...prev, item],
    );
    setMention(null);
    setMentionIndex(-1);
    // Restore the caret to where the token was so typing continues naturally.
    queueMicrotask(() => {
      const ta = textareaRef.current;
      if (ta) ta.setSelectionRange(mention.start, mention.start);
      ta?.focus();
    });
  };

  const attachMember = (role: string) => {
    if (!mention) return;
    const inserted = memberMentionInsertion(role);
    setText(text.slice(0, mention.start) + inserted + text.slice(mention.end));
    setMention(null);
    setMentionIndex(-1);
    // The caret lands after the inserted token so the next word keeps flowing.
    queueMicrotask(() => {
      const ta = textareaRef.current;
      if (ta) {
        const caret = mention.start + inserted.length;
        ta.setSelectionRange(caret, caret);
        ta.focus();
      }
    });
  };

  const openMentionDir = (path: string) => {
    if (!mention) return;
    const inserted = `@${path}/`;
    const next = text.slice(0, mention.start) + inserted + text.slice(mention.end);
    setText(next);
    const caret = mention.start + inserted.length;
    setMention({ query: `${path}/`, start: mention.start, end: caret });
    setMentionIndex(0);
    queueMicrotask(() => {
      const ta = textareaRef.current;
      if (ta) ta.setSelectionRange(caret, caret);
      ta?.focus();
    });
  };

  const removeMentionedItem = (index: number) =>
    setMentionedItems((prev) => prev.filter((_, i) => i !== index));

  const dismiss = () => {
    if (!mention) return;
    setMention(null);
    setMentionIndex(-1);
  };

  const handleKeyDown = (e: MentionKeyboardEvent): boolean => {
    if (!mentionOpen) return false;
    const activeMember =
      mentionIndex >= 0 && mentionIndex < memberRowCount ? mentionMembers[mentionIndex] : undefined;
    const activeEntry =
      mentionIndex >= memberRowCount ? mentionEntries[mentionIndex - memberRowCount] : undefined;
    if (eventMatchesShortcutAction(e, "nextSuggestion")) {
      e.preventDefault();
      setMentionIndex((i) => (i + 1) % rowCount);
      return true;
    }
    if (eventMatchesShortcutAction(e, "previousSuggestion")) {
      e.preventDefault();
      setMentionIndex((i) => (i <= 0 ? rowCount - 1 : i - 1));
      return true;
    }
    // Enter and Tab pick a member row (plain-text routing name; there is no
    // chip to attach). File rows keep the drill/attach split below.
    if (eventMatchesShortcutAction(e, "applySuggestion") && activeMember) {
      e.preventDefault();
      attachMember(activeMember.role);
      return true;
    }
    // Enter: open a folder (drill in) or attach a file. Tab: attach the
    // highlighted row as a unit — whole folder or file — without drilling.
    if (
      eventMatchesShortcutAction(e, "applySuggestion") &&
      e.key === "Enter" &&
      !isMobile &&
      activeEntry
    ) {
      e.preventDefault();
      if (activeEntry.type === "directory") openMentionDir(activeEntry.path);
      else attachMention(activeEntry.path, false);
      return true;
    }
    if (eventMatchesShortcutAction(e, "applySuggestion") && activeEntry) {
      e.preventDefault();
      attachMention(activeEntry.path, activeEntry.type === "directory");
      return true;
    }
    if (eventMatchesShortcutAction(e, "dismissSuggestions")) {
      e.preventDefault();
      dismiss();
      return true;
    }
    return false;
  };

  return {
    mentionIndex,
    mentionOpen,
    mentionedItems,
    setMentionedItems,
    attachMention,
    attachMember,
    openMentionDir,
    removeMentionedItem,
    handleKeyDown,
    dismiss,
  };
}
