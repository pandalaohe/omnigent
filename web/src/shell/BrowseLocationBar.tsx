import { ArrowLeftIcon, FolderDotIcon, FolderSearchIcon } from "lucide-react";
import { type KeyboardEvent, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import type { WorkspaceReach } from "@/hooks/useWorkspaceChangedFiles";
import { WorkspacePickerDialog } from "./WorkspacePickerDialog";

interface BrowseLocationBarProps {
  /** Absolute path currently shown. */
  current: string;
  /** Absolute workspace root, so the picker can offer a one-click return. */
  workspace: string;
  /** Host whose filesystem the picker browses, or null when not host-bound. */
  hostId: string | null;
  /**
   * Whether THIS viewer may browse outside the workspace. Distinct from
   * {@link reach}, which describes what the *environment* can reach and is
   * identical for every viewer of a session — a collaborator who is not the
   * owner is refused (403) however wide the environment's own reach is.
   */
  canBrowseOutside: boolean;
  /** The session's reported reach, or null while the metadata loads. */
  reach: WorkspaceReach | null;
  /** Navigate to an absolute path. */
  onNavigate: (absolutePath: string) => void;
  /**
   * Commit a typed file or folder path. Resolves to the error message to show
   * under the field (which stays open with the text), or null once the path
   * opened or navigated, which closes the field.
   */
  onOpenPath: (text: string) => Promise<string | null>;
  /** Message shown under the path when the last navigation was refused. */
  error?: string | null;
}

/**
 * Working-folder path in the files header: an editable field plus navigation.
 *
 * Clicking the path swaps it for a text input seeded with the current absolute
 * path and fully selected; Enter commits it as a file or folder path, Escape or
 * blur restores. The folder picker lives on its own trailing button, shown only
 * where roaming is allowed; the field itself is offered to every viewer. The
 * parent button stays enabled only while moving upward remains inside the
 * workspace, avoiding an owner-scoped host browse that would be refused.
 */
export function BrowseLocationBar({
  current,
  workspace,
  hostId,
  canBrowseOutside,
  reach,
  onNavigate,
  onOpenPath,
  error,
}: BrowseLocationBarProps) {
  const [pickerOpen, setPickerOpen] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(current);
  const [commitError, setCommitError] = useState<string | null>(null);
  // Identifies the live edit attempt: a commit's late result applies only
  // while its own attempt is still current.
  const attempt = useRef(0);
  // Ref, not state: a second Enter in the same event loop tick must not slip
  // past a guard that only takes effect on the next render.
  const committing = useRef(false);
  const canRoam = canBrowseOutside && (reach?.unconfined ?? false) && hostId !== null;
  const parent = parentPath(current);
  const navigableParent =
    parent !== null && (canRoam || pathIsWithin(parent, workspace)) ? parent : null;
  const shownError = commitError ?? error;

  function startEditing() {
    attempt.current += 1;
    committing.current = false;
    setDraft(current);
    setCommitError(null);
    setEditing(true);
  }

  function cancelEditing() {
    attempt.current += 1;
    committing.current = false;
    setEditing(false);
    setCommitError(null);
  }

  async function commit() {
    if (committing.current) return;
    const id = attempt.current;
    committing.current = true;
    try {
      const message = await onOpenPath(draft);
      // A cancelled or restarted edit owns the bar now; its promise decides.
      if (attempt.current !== id) return;
      if (message === null) {
        setEditing(false);
        setCommitError(null);
      } else {
        setCommitError(message);
      }
    } finally {
      if (attempt.current === id) committing.current = false;
    }
  }

  function handleKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Enter") {
      event.preventDefault();
      // An IME's confirming Enter must not commit a half-composed path.
      if (!event.nativeEvent.isComposing && draft.trim() !== "") void commit();
      return;
    }
    if (event.key === "Escape") {
      // A drawer hosted around this bar closes on a window-level Escape;
      // cancelling the edit must not also dismiss that panel.
      event.stopPropagation();
      cancelEditing();
    }
  }

  return (
    <span className="flex min-w-0 flex-1 flex-col">
      <span className="flex min-w-0 items-center gap-[2px]">
        <ParentFolderButton parent={navigableParent} onNavigate={onNavigate} />
        <WorkspaceRootButton current={current} workspace={workspace} onNavigate={onNavigate} />
        {editing ? (
          <input
            autoFocus
            aria-label="File or folder path"
            className="min-w-0 flex-1 rounded border border-border bg-transparent px-1 py-0.5 font-medium text-ui outline-none focus:border-ring"
            onBlur={cancelEditing}
            onChange={(event) => setDraft(event.target.value)}
            onFocus={(event) => event.currentTarget.select()}
            onKeyDown={handleKeyDown}
            value={draft}
          />
        ) : (
          <button
            type="button"
            title={current}
            aria-label={`Working folder: ${current}. Click to edit the path.`}
            onClick={startEditing}
            className="min-w-0 flex-1 cursor-pointer rounded px-1 py-0.5 text-left hover:bg-muted hover:text-foreground"
            data-testid="browse-location-path"
          >
            <PathText path={current} />
          </button>
        )}
        {canRoam && (
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-sm"
                  aria-label="Browse folders"
                  aria-expanded={pickerOpen}
                  className="shrink-0 text-muted-foreground hover:text-foreground"
                  onClick={() => setPickerOpen(true)}
                >
                  <FolderSearchIcon />
                </Button>
              </TooltipTrigger>
              <TooltipContent side="bottom">Browse folders</TooltipContent>
            </Tooltip>
          </TooltipProvider>
        )}
      </span>
      {shownError && (
        <span className="truncate text-[10px] text-destructive" data-testid="browse-location-error">
          {shownError}
        </span>
      )}
      {canRoam && (
        <WorkspacePickerDialog
          open={pickerOpen}
          onOpenChange={setPickerOpen}
          hostId={hostId}
          initialPath={current}
          workspacePath={workspace}
          onConfirm={onNavigate}
        />
      )}
    </span>
  );
}

function WorkspaceRootButton({
  current,
  workspace,
  onNavigate,
}: {
  current: string;
  workspace: string;
  onNavigate: (absolutePath: string) => void;
}) {
  if (current === workspace) return null;

  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label="Back to working folder"
            className="shrink-0 text-muted-foreground hover:text-foreground"
            onClick={() => onNavigate(workspace)}
          >
            <FolderDotIcon />
          </Button>
        </TooltipTrigger>
        <TooltipContent side="bottom">Back to working folder</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

function ParentFolderButton({
  parent,
  onNavigate,
}: {
  parent: string | null;
  onNavigate: (absolutePath: string) => void;
}) {
  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label="Go to parent folder"
            disabled={parent === null}
            className="shrink-0 text-muted-foreground hover:text-foreground"
            onClick={() => parent && onNavigate(parent)}
          >
            <ArrowLeftIcon />
          </Button>
        </TooltipTrigger>
        <TooltipContent side="bottom">Back one folder</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

/**
 * Render an absolute path, truncating the *start* when it doesn't fit.
 *
 * A path's tail is the informative part — which folder you are in — so the
 * usual end-ellipsis would hide exactly what the user needs to read. The RTL
 * container moves the ellipsis to the front; the inner ``bdi`` keeps the path
 * itself reading left-to-right so the leading slash stays where it belongs.
 *
 * @param path Absolute path to display.
 */
function PathText({ path }: { path: string }) {
  return (
    <span dir="rtl" className="block truncate text-left font-medium text-ui">
      <bdi dir="ltr">{path}</bdi>
    </span>
  );
}

function parentPath(absolutePath: string): string | null {
  const usesBackslashes = absolutePath.includes("\\");
  const normalized = absolutePath.replaceAll("\\", "/").replace(/\/+$/, "");
  if (normalized === "" || normalized === "/" || /^[A-Za-z]:$/.test(normalized)) return null;

  const separator = normalized.lastIndexOf("/");
  if (separator < 0) return null;
  let parent = separator === 0 ? "/" : normalized.slice(0, separator);
  if (/^[A-Za-z]:$/.test(parent)) parent += "/";
  return usesBackslashes ? parent.replaceAll("/", "\\") : parent;
}

function pathIsWithin(path: string, root: string): boolean {
  const normalize = (value: string) => value.replaceAll("\\", "/").replace(/\/+$/, "");
  const normalizedPath = normalize(path);
  const normalizedRoot = normalize(root);
  return normalizedPath === normalizedRoot || normalizedPath.startsWith(`${normalizedRoot}/`);
}
