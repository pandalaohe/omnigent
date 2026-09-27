import { useEffect, useRef } from "react";
import { FileTextIcon, FolderIcon, PlusIcon, StarIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import type { WorkspaceFile } from "@/hooks/useWorkspaceChangedFiles";
import { NO_SESSION_MEMBERS, type SessionMember } from "@/lib/sessionMembers";

interface FileMentionMenuProps {
  /** Directory currently being browsed ("" = workspace root). */
  currentDir: string;
  /** Index of the highlighted row (-1 = none) across members + entries. */
  activeIndex: number;
  /** Entries of the current directory (folders first), already filtered + capped. */
  entries: WorkspaceFile[];
  /**
   * Member rows offered ahead of the file rows in a 2+ member session, already
   * filtered. All members are listed — the lead first — including unavailable
   * ones; picking one inserts ``@role ``.
   */
  members?: readonly SessionMember[];
  /**
   * True while the directory listing is still fetching (cold-boot root load or
   * a sub-directory's first load). Renders a loading row so "@" gives feedback
   * instead of appearing dead while ``entries`` is transiently empty.
   */
  loading?: boolean;
  /** Open (drill into) a folder by its workspace-relative path. */
  onOpenDir: (path: string) => void;
  /** Attach a file (isDir=false) or whole folder (isDir=true) as a unit. */
  onAttach: (path: string, isDir: boolean) => void;
  /** Insert ``@role `` for a member row. */
  onAttachMember?: (role: string) => void;
}

/**
 * Floating drill-down file/folder browser shown when the user types ``@`` in
 * a native coding-agent session. Folders open (drill in) on click so nested
 * files are reachable; a file attaches on click, and a folder's ``+`` button
 * attaches the whole directory as a unit. In a 2+ member session a Members
 * section leads the menu with one row per role (routing names, not files).
 * Mirrors {@link SlashCommandMenu}'s keep-the-active-row-visible behaviour.
 * Exported for direct unit testing.
 */
export function FileMentionMenu({
  currentDir,
  activeIndex,
  entries,
  members = NO_SESSION_MEMBERS,
  loading = false,
  onOpenDir,
  onAttach,
  onAttachMember,
}: FileMentionMenuProps) {
  const listRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (activeIndex < 0 || !listRef.current) return;
    listRef.current.querySelector('[data-active="true"]')?.scrollIntoView({ block: "nearest" });
  }, [activeIndex]);
  const memberCount = members.length;
  const showFiles = entries.length > 0 || loading;
  if (memberCount === 0 && !showFiles) return null;

  return (
    <div className="absolute bottom-full left-0 z-10 mb-2 flex items-end gap-2">
      <div className="w-80 max-w-[calc(100vw-2rem)] shrink-0 overflow-hidden rounded-[12px] border border-border bg-popover p-2 shadow-menu">
        {/* One scroll container over both sections so the arrow-keyed row —
            member or file — is scrolled into view either way. */}
        <div ref={listRef} className="max-h-80 overflow-y-auto">
          {memberCount > 0 && (
            <>
              <div className="px-1.5 py-1 text-sm font-medium text-muted-foreground">Members</div>
              <div role="listbox" aria-label="Members">
                {members.map((member, i) => (
                  <button
                    key={member.role}
                    type="button"
                    role="option"
                    aria-selected={i === activeIndex}
                    data-testid={`member-mention-item-${i}`}
                    data-active={i === activeIndex ? "true" : undefined}
                    // preventDefault keeps the textarea focused while clicking.
                    onMouseDown={(e) => e.preventDefault()}
                    onClick={() => onAttachMember?.(member.role)}
                    title={`Mention @${member.role}`}
                    className={cn(
                      "flex w-full items-center gap-2 rounded-md px-1.5 py-1 text-left text-ui text-foreground",
                      i === activeIndex && "bg-muted dark:bg-muted/50",
                      member.unavailable !== null && "text-muted-foreground",
                    )}
                  >
                    <StarIcon
                      className={cn(
                        "size-3.5 shrink-0",
                        member.lead ? "fill-current text-amber-500" : "opacity-0",
                      )}
                      aria-hidden="true"
                    />
                    <span className="min-w-0 flex-1 truncate">@{member.role}</span>
                    {member.lead && (
                      <span className="shrink-0 text-sm text-muted-foreground">Lead</span>
                    )}
                    {member.unavailable !== null && (
                      <span className="shrink-0 text-sm text-warning">unavailable</span>
                    )}
                  </button>
                ))}
              </div>
            </>
          )}
          {showFiles && (
            <>
              <div className="flex items-center justify-between gap-2 px-1.5 py-1 text-sm font-medium text-muted-foreground">
                <span className="truncate">{currentDir ? `/${currentDir}` : "Workspace"}</span>
                <span className="shrink-0 text-[10px]">↵ open · ⇥ attach</span>
              </div>
              {entries.length === 0 && loading ? (
                <div className="px-1.5 py-1 text-ui text-muted-foreground">Loading…</div>
              ) : (
                <div role="listbox">
                  {entries.map((entry, i) => {
                    const isDir = entry.type === "directory";
                    const rowIndex = memberCount + i;
                    return (
                      <div
                        key={`${entry.type}:${entry.path}`}
                        role="option"
                        aria-selected={rowIndex === activeIndex}
                        data-testid={`file-mention-item-${rowIndex}`}
                        data-active={rowIndex === activeIndex ? "true" : undefined}
                        className={cn(
                          "flex w-full items-center gap-2 rounded-md px-1.5 py-1 text-left text-ui text-foreground",
                          rowIndex === activeIndex && "bg-muted dark:bg-muted/50",
                        )}
                      >
                        <button
                          type="button"
                          // preventDefault keeps the textarea focused while clicking.
                          onMouseDown={(e) => e.preventDefault()}
                          onClick={() =>
                            isDir ? onOpenDir(entry.path) : onAttach(entry.path, false)
                          }
                          className="flex min-w-0 flex-1 items-center gap-2 hover:text-foreground"
                          title={isDir ? `Open ${entry.name}` : `Attach ${entry.name}`}
                        >
                          {isDir ? (
                            <FolderIcon className="size-3.5 shrink-0 text-slate-500 dark:text-slate-400" />
                          ) : (
                            <FileTextIcon className="size-3.5 shrink-0 text-slate-500 dark:text-slate-400" />
                          )}
                          <span className="truncate">
                            {entry.name}
                            {isDir ? "/" : ""}
                          </span>
                        </button>
                        {isDir && (
                          <button
                            type="button"
                            onMouseDown={(e) => e.preventDefault()}
                            onClick={() => onAttach(entry.path, true)}
                            className="flex shrink-0 items-center gap-0.5 rounded-md border border-border px-1.5 py-0.5 text-sm text-muted-foreground hover:bg-accent hover:text-foreground"
                            aria-label={`Attach whole folder ${entry.name}`}
                            title={`Attach whole folder ${entry.name}`}
                          >
                            <PlusIcon className="size-3" />
                            folder
                          </button>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
