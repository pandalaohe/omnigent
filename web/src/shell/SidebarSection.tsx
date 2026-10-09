// The thin per-section controller used by every sidebar branch: it owns the
// section options menu and the height-cap descriptor, and merges the options
// kebab into the branch's existing header-action cluster. Collapse state stays
// with the list owner (device-local, keyed by section id).

import { createContext, useContext, useRef, useState, type ReactNode, type RefObject } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  CheckIcon as CheckMarkIcon,
  FolderInputIcon,
  MoreHorizontalIcon,
  PencilIcon,
  PlusIcon,
  Trash2Icon,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { showToast } from "@/components/ui/toast";
import { resolveOrCreateProjectId } from "@/hooks/useConversations";
import type { SaveSidebarLayout } from "@/hooks/useSidebarLayout";
import {
  moveProjectToSection,
  moveSection,
  removeSection,
  sectionOfProject,
  type SidebarLayout,
  type SidebarSectionDef,
} from "@/lib/sidebarLayout";
import { cn } from "@/lib/utils";

import type { MenuComponents } from "./Sidebar";
import { NewSectionDialog } from "./NewSectionDialog";

export interface SidebarLayoutContextValue {
  layout: SidebarLayout;
  saveLayout: SaveSidebarLayout;
}

/** Null outside the sidebar list, where sections are not editable. */
export const SidebarLayoutContext = createContext<SidebarLayoutContextValue | null>(null);

export function useSidebarLayoutContext(): SidebarLayoutContextValue | null {
  return useContext(SidebarLayoutContext);
}

export interface SidebarSectionBody {
  /** The capped scroll container when the section has a max height. */
  bodyRef: RefObject<HTMLDivElement | null>;
  /** `maxRows * 29px`, or null when the section is uncapped. */
  maxHeight: number | null;
  /** The infinite-scroll root: the body when capped, else the sidebar. */
  scrollRoot: RefObject<HTMLElement | null>;
}

const MAX_ROWS_OPTIONS = [5, 10, 15, 20, 30];
const RECENT_COUNT_OPTIONS = Array.from({ length: 20 }, (_, index) => index + 1);

/**
 * Renders the section body. Always present so the branch has one place for the
 * body container; the cap only applies when `maxHeight` is set.
 */
export function SectionBody({
  body,
  className,
  children,
}: {
  body: SidebarSectionBody;
  className?: string;
  children: ReactNode;
}) {
  return (
    <div
      ref={body.bodyRef}
      data-testid="sidebar-section-body"
      className={cn(body.maxHeight !== null && "overflow-y-auto", className)}
      style={body.maxHeight !== null ? { maxHeight: body.maxHeight } : undefined}
    >
      {children}
    </div>
  );
}

/**
 * Wraps one layout section: computes the height-cap/scroll descriptor and hands
 * the branch the section's options menu to place in its header.
 */
export function SidebarSection({
  section,
  fallbackScrollRoot,
  children,
}: {
  section: SidebarSectionDef;
  /** The sidebar's scroll container, used when the section is uncapped. */
  fallbackScrollRoot: RefObject<HTMLElement | null>;
  children: (body: SidebarSectionBody, headerAction: ReactNode) => ReactNode;
}) {
  const bodyRef = useRef<HTMLDivElement | null>(null);
  const maxHeight = section.maxRows === null ? null : section.maxRows * 29;
  const options = <SectionOptionsMenu section={section} />;
  const body: SidebarSectionBody = {
    bodyRef,
    maxHeight,
    scrollRoot: maxHeight === null ? fallbackScrollRoot : bodyRef,
  };
  return <>{children(body, options)}</>;
}

function SectionOptionsMenu({ section }: { section: SidebarSectionDef }) {
  const context = useSidebarLayoutContext();
  const [newSectionOpen, setNewSectionOpen] = useState(false);
  const [renameOpen, setRenameOpen] = useState(false);
  const [removeOpen, setRemoveOpen] = useState(false);
  const [renameValue, setRenameValue] = useState(section.name);

  if (context === null) return null;
  const { layout, saveLayout } = context;

  const index = layout.sections.findIndex((candidate) => candidate.id === section.id);
  const first = index <= 0;
  const last = index === layout.sections.length - 1;

  const updateSection = (update: (current: SidebarSectionDef) => SidebarSectionDef) => {
    saveLayout({
      version: 1,
      sections: layout.sections.map((candidate) =>
        candidate.id === section.id ? update(candidate) : candidate,
      ),
    });
  };

  const remove = () => saveLayout(removeSection(layout, section.id));
  const requestRemove = () => {
    if (section.kind === "other_projects" || section.kind === "other_sessions") {
      setRemoveOpen(true);
      return;
    }
    remove();
  };

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            aria-label="Section options"
            data-testid="section-options"
            className="text-muted-foreground"
            onClick={(event) => event.stopPropagation()}
          >
            <MoreHorizontalIcon className="size-3.5" data-icon-size="14" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="min-w-44">
          <DropdownMenuItem data-testid="new-section" onSelect={() => setNewSectionOpen(true)}>
            <PlusIcon className="size-3.5" />
            New section…
          </DropdownMenuItem>
          <DropdownMenuItem
            data-testid="rename-section"
            onSelect={() => {
              setRenameValue(section.name);
              setRenameOpen(true);
            }}
          >
            <PencilIcon className="size-3.5" />
            Rename…
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuItem
            data-testid="move-section-up"
            disabled={first}
            onSelect={() => saveLayout(moveSection(layout, section.id, "up"))}
          >
            Move up
          </DropdownMenuItem>
          <DropdownMenuItem
            data-testid="move-section-down"
            disabled={last}
            onSelect={() => saveLayout(moveSection(layout, section.id, "down"))}
          >
            Move down
          </DropdownMenuItem>
          <DropdownMenuItem
            data-testid="move-section-top"
            disabled={first}
            onSelect={() => saveLayout(moveSection(layout, section.id, "top"))}
          >
            Move to top
          </DropdownMenuItem>
          <DropdownMenuItem
            data-testid="move-section-bottom"
            disabled={last}
            onSelect={() => saveLayout(moveSection(layout, section.id, "bottom"))}
          >
            Move to bottom
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuSub>
            <DropdownMenuSubTrigger data-testid="section-max-height">
              Max height
            </DropdownMenuSubTrigger>
            <DropdownMenuSubContent>
              <DropdownMenuRadioGroup
                value={section.maxRows === null ? "none" : String(section.maxRows)}
                onValueChange={(value) =>
                  updateSection((current) => ({
                    ...current,
                    maxRows: value === "none" ? null : Number(value),
                  }))
                }
              >
                <DropdownMenuRadioItem value="none">No limit</DropdownMenuRadioItem>
                {MAX_ROWS_OPTIONS.map((rows) => (
                  <DropdownMenuRadioItem key={rows} value={String(rows)}>
                    {rows} rows
                  </DropdownMenuRadioItem>
                ))}
              </DropdownMenuRadioGroup>
            </DropdownMenuSubContent>
          </DropdownMenuSub>
          {section.kind === "recent" && (
            <DropdownMenuSub>
              <DropdownMenuSubTrigger data-testid="section-show">Show</DropdownMenuSubTrigger>
              <DropdownMenuSubContent className="max-h-64 overflow-y-auto">
                <DropdownMenuRadioGroup
                  value={String(section.count ?? 5)}
                  onValueChange={(value) =>
                    updateSection((current) => ({ ...current, count: Number(value) }))
                  }
                >
                  {RECENT_COUNT_OPTIONS.map((count) => (
                    <DropdownMenuRadioItem
                      key={count}
                      value={String(count)}
                      data-testid={`section-show-${count}`}
                    >
                      {count}
                    </DropdownMenuRadioItem>
                  ))}
                </DropdownMenuRadioGroup>
              </DropdownMenuSubContent>
            </DropdownMenuSub>
          )}
          <DropdownMenuSeparator />
          <DropdownMenuItem
            data-testid="remove-section"
            variant="destructive"
            onSelect={requestRemove}
          >
            <Trash2Icon className="size-3.5" />
            Remove section
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      <Dialog open={renameOpen} onOpenChange={setRenameOpen}>
        <DialogContent data-testid="rename-section-dialog">
          <DialogHeader>
            <DialogTitle>Rename section</DialogTitle>
          </DialogHeader>
          <form
            onSubmit={(event) => {
              event.preventDefault();
              const next = renameValue.trim();
              if (next === "") return;
              updateSection((current) => ({ ...current, name: next }));
              setRenameOpen(false);
            }}
          >
            <Input
              data-testid="rename-section-input"
              value={renameValue}
              autoFocus
              onChange={(event) => setRenameValue(event.target.value)}
            />
            <DialogFooter className="border-t-0 bg-transparent">
              <Button type="button" variant="ghost" onClick={() => setRenameOpen(false)}>
                Cancel
              </Button>
              <Button type="submit" data-testid="rename-section-confirm">
                Confirm
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
      <Dialog open={removeOpen} onOpenChange={setRemoveOpen}>
        <DialogContent data-testid="remove-section-dialog">
          <DialogHeader>
            <DialogTitle>Remove section?</DialogTitle>
            <DialogDescription>
              {section.kind === "other_projects"
                ? "Unsectioned projects will not show in the sidebar until you add this section back."
                : "Unfiled sessions will not show in the sidebar until you add this section back."}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter className="border-t-0 bg-transparent">
            <Button type="button" variant="ghost" onClick={() => setRemoveOpen(false)}>
              Cancel
            </Button>
            <Button
              type="button"
              variant="destructive"
              data-testid="remove-section-confirm"
              onClick={() => {
                setRemoveOpen(false);
                remove();
              }}
            >
              Remove
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      {newSectionOpen && (
        <NewSectionDialog
          open
          onOpenChange={(open) => {
            if (!open) setNewSectionOpen(false);
          }}
          layout={layout}
          saveLayout={saveLayout}
        />
      )}
    </>
  );
}

/**
 * The "Move to section" submenu of a project folder's menu. Owns the id
 * promotion (a label-only folder gets a first-class id) and writes the move
 * against the freshly read layout so an edit made while the project was being
 * created isn't lost.
 */
export function MoveProjectToSectionMenu({
  components: C,
  projectId,
  projectName,
}: {
  components: MenuComponents;
  projectId: string | null;
  projectName: string;
}) {
  const context = useSidebarLayoutContext();
  const queryClient = useQueryClient();
  const projectSections =
    context === null
      ? []
      : context.layout.sections.filter((section) => section.kind === "projects");
  const currentSectionId =
    context === null || projectId === null ? null : sectionOfProject(context.layout, projectId);

  const moveToSection = async (sectionId: string | null) => {
    if (context === null) return;
    let id = projectId;
    try {
      if (id === null) {
        id = await resolveOrCreateProjectId(projectName);
        // The folder can only render once the created project appears in the
        // list the layout resolves ids against.
        await queryClient.invalidateQueries({ queryKey: ["projects"] });
      }
    } catch {
      showToast("Couldn't move the project");
      return;
    }
    if (id === null) return;
    context.saveLayout((current) => moveProjectToSection(current, id, sectionId));
  };

  if (projectSections.length === 0) return null;

  return (
    <C.Sub>
      <C.SubTrigger data-testid="move-project-to-section">
        <FolderInputIcon className="size-3.5" />
        Move to section
      </C.SubTrigger>
      <C.SubContent>
        {projectSections.map((section) => (
          <C.Item
            key={section.id}
            data-testid={`move-to-section-${section.id}`}
            disabled={currentSectionId === section.id}
            onSelect={() => void moveToSection(section.id)}
          >
            <span className="flex-1 truncate">{section.name}</span>
            {currentSectionId === section.id && (
              <CheckMarkIcon className="size-3.5 shrink-0 text-primary" />
            )}
          </C.Item>
        ))}
        <C.Item
          data-testid="move-to-section-other"
          disabled={currentSectionId === null}
          onSelect={() => void moveToSection(null)}
        >
          <span className="flex-1 truncate">Projects (no section)</span>
          {currentSectionId === null && (
            <CheckMarkIcon className="size-3.5 shrink-0 text-primary" />
          )}
        </C.Item>
      </C.SubContent>
    </C.Sub>
  );
}

/**
 * Shown after the section loop when nothing rendered a header — the only other
 * "New section…" entry lives in a section's own options menu, so an empty
 * layout (or one holding only sections that render nothing yet) would strand
 * the user with no way to add one back.
 */
export function NewSectionFallback() {
  const context = useSidebarLayoutContext();
  const [open, setOpen] = useState(false);
  if (context === null) return null;
  return (
    <>
      <button
        type="button"
        data-testid="sidebar-new-section-fallback"
        onClick={() => setOpen(true)}
        className="flex h-7 w-full items-center gap-1 rounded-[var(--radius-otto-sm)] border-0 px-2 text-left text-sm font-normal text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
      >
        <PlusIcon className="size-3.5" />
        New section…
      </button>
      {open && (
        <NewSectionDialog
          open
          onOpenChange={(next) => {
            if (!next) setOpen(false);
          }}
          layout={context.layout}
          saveLayout={context.saveLayout}
        />
      )}
    </>
  );
}
