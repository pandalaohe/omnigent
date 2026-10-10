import { AgentBadge } from "@/components/AgentBadge";
import { copyText } from "@/lib/clipboard";
import { AGENT_TEMPLATE_LABEL } from "@/lib/customAgentsApi";
import { filterSessionScope } from "@/lib/sessionVisibility";
import { getCurrentUserId } from "@/lib/identity";
import { PinCapacityContext, SidebarConfigContext } from "@/lib/sidebarConfig";
import { useSidebarDisplayPagination } from "@/hooks/useSidebarDisplayPagination";
import { useSidebarData, useSidebarView, type SidebarListQuery } from "@/hooks/useSidebarData";
import { useArchivedSessions } from "@/hooks/useScopeCache";
import { InfiniteScrollSentinel, type AutoLoadBudget } from "@/components/InfiniteScrollSentinel";
import {
  type ComponentType,
  type CSSProperties,
  type KeyboardEvent,
  type MouseEvent,
  type ReactNode,
  type RefObject,
  createContext,
  memo,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import {
  ActivityIcon,
  ArchiveIcon,
  ArrowUpDownIcon,
  ArchiveRestoreIcon,
  BellIcon,
  BellOffIcon,
  CheckIcon,
  CheckIcon as CheckMarkIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  ClockIcon,
  CircleAlertIcon,
  CircleStopIcon,
  CopyIcon,
  FolderGit2Icon,
  FolderIcon,
  FolderInputIcon,
  FolderMinusIcon,
  FolderOpenIcon,
  GitBranchIcon,
  GitForkIcon,
  InboxIcon,
  ListChecksIcon,
  ListFilterIcon,
  LaptopIcon,
  LayoutDashboardIcon,
  Loader2Icon,
  MailIcon,
  MailOpenIcon,
  MessageCircleCheckIcon,
  MessageCircleDashedIcon,
  MessageCirclePlusIcon,
  Maximize2Icon,
  Minimize2Icon,
  MoreHorizontalIcon,
  PencilIcon,
  PinIcon,
  PinOffIcon,
  PlayIcon,
  PlusIcon,
  SearchIcon,
  Settings2Icon,
  ShareIcon,
  SmilePlusIcon,
  SquareIcon,
  SquareCheckIcon,
  Trash2Icon,
  UsersIcon,
  WalletIcon,
  XIcon,
} from "lucide-react";
import {
  DndContext,
  KeyboardSensor,
  DragOverlay,
  type DragEndEvent,
  type DragStartEvent,
  MeasuringStrategy,
  MouseSensor,
  closestCenter,
  pointerWithin,
  TouchSensor,
  useDraggable,
  useDroppable,
  useSensor,
  useSensors,
} from "@dnd-kit/core";
import {
  arrayMove,
  SortableContext,
  sortableKeyboardCoordinates,
  verticalListSortingStrategy,
  useSortable,
} from "@dnd-kit/sortable";
import { useProjectOrder, useSaveProjectOrder } from "@/hooks/useProjectOrder";
import { useIsMutating, useMutation, useQueryClient } from "@tanstack/react-query";
import { PIN_WRITE_MUTATION_KEY } from "@/lib/sessionListCache";
import { Link, useLocation, useNavigate, useParams, useRebasePath } from "@/lib/routing";
import { SidebarHeaderActions, SidebarSettingsButton } from "./SidebarHeaderActions";
import omnigentWordmark from "@/assets/omnigent-wordmark.svg";
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
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuSeparator,
  ContextMenuSub,
  ContextMenuSubContent,
  ContextMenuSubTrigger,
  ContextMenuTrigger,
} from "@/components/ui/context-menu";
import { HoverCard, HoverCardContent, HoverCardTrigger } from "@/components/ui/hover-card";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import {
  type Conversation,
  type PinnedConversationsResult,
  useArchiveConversation,
  useBulkArchiveConversations,
  useBulkDeleteConversations,
  useBulkMoveToProject,
  useProjects,
  useProjectSessions,
  useLeaveSession,
  useMoveToProject,
  useDeleteProject,
  useRenameProject,
  useProjectConfig,
  useUpdateProjectConfig,
  PROJECT_LABEL_KEY,
  PINNED_CONVERSATIONS_KEY,
  useTogglePinnedConversation,
  useReorderPinnedConversations,
  setConversationPinned,
  useRenameConversation,
  useStopAndDeleteConversation,
  useStopSession,
} from "@/hooks/useConversations";
import { useHosts, type Host } from "@/hooks/useHosts";
import { useComments } from "@/hooks/useComments";
import { bulkCommentsDeleteLine, unhandledCommentsDeleteLine } from "@/lib/comments";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isFeatureEnabled, isSingleUserMode, sandboxOptionLabel } from "@/lib/capabilities";
import { useBranding } from "@/lib/branding";
import { relativeTime } from "@/lib/relativeTime";
import { USER_SESSION_TITLE_MAX_CHARS } from "@/lib/sessionTitles";
import { showToast } from "@/components/ui/toast";
import { showArchiveUndoToast } from "./archiveUndoToast";
import { unpinWithUndo } from "./unpinUndoToast";
import { useArchiveWorktreePrompt } from "./ArchiveWorktreeDialog";
import { PermissionsModal } from "@/components/PermissionsModal";
import { ProjectSettingsDialog } from "./ProjectSettingsDialog";
import { ProjectRowIcon } from "./ProjectPicker";
import { EmojiPicker } from "@/components/ProjectIconPicker";
import {
  BackgroundActivityBadge,
  ColdIdleDot,
  GoalActivityBadge,
  SessionStateBadge,
} from "@/components/SessionStateBadge";
import { useSessionRunnerOnline } from "@/hooks/RunnerHealthProvider";
import { useActiveRootSessionId } from "@/hooks/useSession";
import { useSessionNavigationPreferences } from "@/hooks/useSessionNavigationPreferences";
import { useNewSessionTarget } from "@/hooks/useNewSessionTarget";
import {
  newSessionRoute,
  newSessionTargetLabel,
  type NewSessionTarget,
} from "@/lib/newSessionTarget";
import { isSessionStoppable } from "@/lib/sessionStop";
import { retrySession } from "@/lib/sessionsApi";
import { effectiveWorktree } from "@/lib/types";
import { isImeCompositionKeyEvent } from "@/lib/ime";
import { useHasSessionDraft } from "@/lib/sessionDrafts";
import { useOptimisticTitle } from "@/lib/optimisticTitles";
import {
  getConversationForegroundStatus,
  getSessionState,
  type SessionState,
} from "@/hooks/useSessionState";
import { useSessionErrorStates } from "@/hooks/useSessionErrors";
import type { LatestSessionError } from "@/lib/sessionError";
import { rowMark } from "@/lib/rowMark";
import { useSoundAlertPreferences } from "@/hooks/useSoundAlertPreferences";
import { readSoundAlertPreferences, writeSoundAlertPreferences } from "@/lib/soundAlertPreferences";
import { SoundAlertsLockedHint } from "@/components/SoundAlertSettings";
import { useChatStore } from "@/store/chatStore";
import {
  isConversationUnseen,
  isExplicitlyUnread,
  markConversationsSeen,
  markConversationRead,
  markConversationUnread,
  useConversationReadState,
  useUnseenTick,
} from "@/hooks/useUnseenConversations";
import { cn } from "@/lib/utils";
import { useOmnigentAnalytics } from "@/lib/analytics";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { useIOSNativeKeyboardInset } from "@/hooks/useIOSNativeKeyboardInset";
import { useResizableSidebar } from "@/hooks/useResizableSidebar";
import { useSessionSwitchHotkey } from "@/hooks/useSessionSwitchHotkey";
import { usePinnedSessionHotkeys } from "@/hooks/usePinnedSessionHotkeys";
import { useSessionPollingHotkeys } from "@/hooks/useSessionPollingHotkeys";
import { isCurrentServerLocal } from "@/lib/serverOrigin";
import {
  type SessionFilter,
  readSessionFilter,
  writeSessionFilter,
} from "@/lib/sessionFilterPreferences";
import { ExtensionPrimaryNavigation } from "@/extensions/ExtensionPrimaryNavigation";
import { PrimaryNavLink } from "@/shell/PrimaryNavLink";
import { useSystemStatusSummary, type SystemStatusLevel } from "@/hooks/useSystemStatus";
import { useViewerId } from "@/hooks/useViewerId";
import { useSidebarLayout } from "@/hooks/useSidebarLayout";
import { useRecentSessions, RecentSessionsUnavailableError } from "@/hooks/useRecentSessions";
import {
  favoritesRows,
  insertSection,
  moveSection,
  newSectionId,
  removeFavorite,
  sectionOfProject,
  splitFavoritesReorder,
  type FavoriteRef,
  type SidebarLayout,
  type SidebarSectionDef,
} from "@/lib/sidebarLayout";
import {
  addProjectToFavoritesWithPromotion,
  moveProjectToSectionWithPromotion,
  MoveProjectToSectionMenu,
  NewSectionFallback,
  sectionOrderId,
  SidebarLayoutContext,
  SidebarSection,
  SectionBody,
  useSidebarLayoutContext,
} from "./SidebarSection";
import { useExtensions } from "@/extensions/ExtensionProvider";
import { extensionPathParts, resolveExtensionPageFromPath } from "@/extensions/catalog";
import { NewProjectButton } from "./NewProjectButton";
import { SettingsSidebarBody, useSettingsRoute, useTrackSettingsReturn } from "./settingsNav";
import {
  type ActiveChatOverride,
  clearLegacyPinnedConversationIds,
  computeNextActiveOverride,
  conversationDisplayLabel,
  dedupeConversationsById,
  EXPANDED_PROJECT_SECTIONS_STORAGE_KEY,
  orderByPinnedTimestamp,
  pinOrderWrites,
  type PinOrderWrite,
  readCollapsedSidebarSectionIds,
  readPinnedConversationIds,
  resolveSidebarDrop,
  type SidebarDropTarget,
  sortByUpdatedAtDesc,
  writeCollapsedSidebarSectionIds,
  writeLegacyPinnedConversationIds,
  isOwnedByViewer,
  sessionBelongsToProject,
} from "./sidebarNav";
import { SidebarServerPicker } from "./SidebarServerPicker";
import { ForkSessionDialog } from "./ForkSessionDialog";
import { SIDEBAR_ROW } from "./sidebarStyles";
import { TooltipArrow } from "radix-ui/tooltip";
import { getEmbedRoot } from "../lib/host";
import { CompactShortcutKeys, useShortcutHint } from "@/components/KeyboardShortcut";

// Positioning for a row's trailing session-state badge. Anchored at the row's
// trailing icon edge in every viewport: on desktop it fades on hover so the pin
// + kebab take its place; on mobile those controls are gone, so the badge holds
// that edge.
const SESSION_STATE_SLOT_CLASS =
  "-translate-y-1/2 pointer-events-none absolute top-1/2 flex h-5 items-center transition-opacity md:group-hover:opacity-0 md:group-has-[:focus-visible]:opacity-0 md:group-has-[[aria-expanded=true]]:opacity-0";

function isPlainNavigationClick(event: MouseEvent<HTMLAnchorElement>): boolean {
  return (
    !event.defaultPrevented &&
    event.button === 0 &&
    !event.metaKey &&
    !event.ctrlKey &&
    !event.shiftKey &&
    !event.altKey
  );
}

// Small markers (running/starting/unseen dot, or the draft pencil when there's
// no session state) get a fixed size-6 centered box so their glyph lands 16px
// from the right edge — lining up vertically with the desktop kebab and the
// size-6 section-header icons above (the marker column) in every viewport. The
// "awaiting" pill keeps its natural width — a fixed box would clip its "Needs
// response" label.
function isDotMarker(state: SessionState | null): boolean {
  return state === null || state.kind !== "awaiting";
}
const SESSION_STATE_DOT_SLOT_CLASS = "w-6 justify-center";

// Match the Settings sidebar's ghost-button hover treatment across every home
// sidebar row.
const SIDEBAR_HOVER_HIGHLIGHT = "hover:bg-muted hover:text-foreground dark:hover:bg-muted/50";
// Keep a project-folder row highlighted while its actions menu is open (kebab
// or header context menu, both flagged `data-state=open` inside `group/header`).
// A right-click opens the context menu in a portal, dropping `:hover` from the
// header, so the hover highlight alone would leave the row un-highlighted.
const SIDEBAR_OPEN_MENU_HIGHLIGHT =
  "group-has-[[data-state=open]]/header:bg-muted group-has-[[data-state=open]]/header:text-foreground dark:group-has-[[data-state=open]]/header:bg-muted/50";
// Active highlight also wins on hover so active items don't lose their
// background and flash when the mouse enters them.
const SIDEBAR_ACTIVE_HIGHLIGHT =
  "bg-[var(--sidebar-active)] text-[var(--sidebar-active-foreground)] hover:bg-[var(--sidebar-active)] hover:text-[var(--sidebar-active-foreground)] dark:hover:bg-[var(--sidebar-active)] dark:hover:text-[var(--sidebar-active-foreground)]";
const DROP_TARGET_HIGHLIGHT = SIDEBAR_ACTIVE_HIGHLIGHT;

const SCROLLBAR_HIDE_DELAY_MS = 700;

// Maps a first-class project id → its name, provided once at the list level so
// each row resolves its ``project_id`` to a folder name without its own
// ``useProjects()`` subscription. Keeps row renders O(1) and avoids spinning up
// a query observer per row (which would also re-run on every project mutation).
const ProjectNamesContext = createContext<Map<string, string>>(new Map());
// Maps a first-class project id → its chosen emoji icon (only projects that
// have one), sharing the same list-level lookup as the names map so a row can
// surface the real project glyph in the pinned flyout without its own query.
const ProjectIconsContext = createContext<Map<string, string>>(new Map());
const HostsByIdContext = createContext<ReadonlyMap<string, Host>>(new Map());
// Row-invariant values resolved once at the list owner and shared, so a row
// doesn't run `useIsMobileViewport` (a matchMedia-on-every-render store) or
// `useViewerId` (an identity-resolve effect) per instance.
const IsMobileContext = createContext<boolean>(false);
const ViewerIdContext = createContext<string | null>(null);
const ServerInfoContext = createContext<ReturnType<typeof useServerInfo>>("loading");
const RowActivationContext = createContext<
  (id: string, event: MouseEvent<HTMLAnchorElement>) => void
>(() => {});
// True while a pin, unpin, or reorder is saving; pin controls are disabled then.
const PinSavingContext = createContext(false);
// Set only around the Pinned section, so its rows become drag-to-reorder targets.
// `draggingId` is the session being dragged; `overId` the row it's over.
const PinOrderContext = createContext<{
  ids: string[];
  draggingId: string | null;
  overId: string | null;
} | null>(null);
// Rows report an in-progress inline-rename edit here so ConversationList can
// hold the sort order for the edit's whole duration — the pointer often
// leaves the list while typing, and a reorder then would shuffle rows around
// the open input (and can even blur it mid-edit, committing a half-typed
// title). See the order-freeze block in ConversationList.
const RowEditHoldContext = createContext<(id: string, editing: boolean) => void>(() => {});

// Stable callback identity that always runs the latest `fn` — keeps the row
// handlers from changing on every Sidebar render and defeating the row memo.
function useStableCallback<A extends unknown[], R>(fn: (...args: A) => R): (...args: A) => R {
  const ref = useRef(fn);
  ref.current = fn;
  return useCallback((...args: A) => ref.current(...args), []);
}

function SidebarRowDataProvider({
  projectNamesById,
  projectIconsById,
  hostsById,
  isMobile,
  viewerId,
  serverInfo,
  onActivate,
  children,
}: {
  projectNamesById: Map<string, string>;
  projectIconsById: Map<string, string>;
  hostsById: ReadonlyMap<string, Host>;
  isMobile: boolean;
  viewerId: string | null;
  serverInfo: ReturnType<typeof useServerInfo>;
  onActivate: (id: string, event: MouseEvent<HTMLAnchorElement>) => void;
  children: ReactNode;
}) {
  return (
    <ProjectNamesContext.Provider value={projectNamesById}>
      <ProjectIconsContext.Provider value={projectIconsById}>
        <HostsByIdContext.Provider value={hostsById}>
          <IsMobileContext.Provider value={isMobile}>
            <ViewerIdContext.Provider value={viewerId}>
              <ServerInfoContext.Provider value={serverInfo}>
                <RowActivationContext.Provider value={onActivate}>
                  {children}
                </RowActivationContext.Provider>
              </ServerInfoContext.Provider>
            </ViewerIdContext.Provider>
          </IsMobileContext.Provider>
        </HostsByIdContext.Provider>
      </ProjectIconsContext.Provider>
    </ProjectNamesContext.Provider>
  );
}

/**
 * Which slice of sessions the sidebar shows. ``"mine"``/``"shared"`` split by
 * ownership (see :func:`isOwnedByViewer`); ``"archived"`` is the only slice
 * that includes archived sessions. The vocabulary lives with the persistence
 * helpers, which validate a stored value against it.
 */
type SidebarTab = SessionFilter;

const SIDEBAR_FILTERS: { value: SidebarTab; label: string }[] = [
  { value: "all", label: "All sessions" },
  { value: "mine", label: "My sessions" },
  { value: "shared", label: "Shared sessions" },
  { value: "archived", label: "Archived sessions" },
];

// Shown in place of the list when a filter matches nothing.
const SIDEBAR_FILTER_EMPTY: Record<SidebarTab, string> = {
  all: "No sessions",
  mine: "No sessions",
  shared: "No sessions",
  archived: "No sessions",
};

// Bulk-selection targets either the flat "Sessions" list or the sessions
// nested inside project folders; the active scope decides which rows show
// checkboxes and where the bulk-action bar renders.
type SelectionScope = "sessions" | "projects";

interface SidebarProps {
  open: boolean;
  onClose: () => void;
  /**
   * Pin a peeking sidebar fully open (the in-sidebar toggle shown while
   * peeking). Optional (defaults to a no-op) so the sidebar renders standalone
   * in tests.
   */
  onOpen?: () => void;
  /**
   * Live open fraction (0 = closed, 1 = open) while the iOS shell's left-edge
   * swipe is dragging the sidebar; `null` when not dragging. When set, the
   * mobile overlay tracks it directly (transition suppressed) so the drawer
   * follows the finger; on release the parent clears it and toggles `open`,
   * letting the CSS transition animate to the resting state.
   */
  dragProgress?: number | null;
  /**
   * Open the global command palette (⌘K). The sidebar's "Search" button routes
   * here rather than filtering inline: session search (title + chat content)
   * lives in the palette, which the box now doubles as an entry point for.
   * Optional (defaults to a no-op) so the sidebar renders standalone in tests.
   */
  onOpenSearch?: () => void;
  /**
   * Whether the sidebar is peeking.
   */
  peek?: boolean;
}

/**
 * Which top-level nav button (New session / Inbox) is active for the current
 * route.
 *
 * The inbox route has no param to key off, and the sidebar is basename-agnostic
 * (in embedded mode the routing seam rebases `to="/inbox"` → `${basename}/inbox`
 * behind its back), so `useMatch` / `NavLink` can't be used without knowing the
 * mount path. Instead compare the active route's last non-empty path segment,
 * which is `inbox` in both standalone and embedded modes. Conversation ids are
 * `conv_…`-prefixed, so a chat route's leaf can never collide with `inbox`.
 */
function useActiveNavItem(): {
  isNewChatPage: boolean;
  isInboxPage: boolean;
  isCanvasPage: boolean;
  isTasksPage: boolean;
  isUsagePage: boolean;
  isSystemStatusPage: boolean;
  activeExtensionPageId: string | null;
  newSessionProjectName: string | null;
} {
  const location = useLocation();
  const rebasePath = useRebasePath();
  const extensions = useExtensions();
  const leaf = location.pathname.split("/").filter(Boolean).at(-1);
  const isExtensionRoute = extensionPathParts(location.pathname) !== null;
  const isInboxPage = !isExtensionRoute && leaf === "inbox";
  const isCanvasPage = !isExtensionRoute && leaf === "canvas";
  const isTasksPage = !isExtensionRoute && leaf === "tasks";
  const isUsagePage = !isExtensionRoute && leaf === "usage";
  const isSystemStatusPage = !isExtensionRoute && leaf === "system";
  const activeExtensionPageId =
    resolveExtensionPageFromPath(extensions, location.pathname)?.page.id ?? null;
  const isNewSessionRoute =
    location.pathname.replace(/\/+$/, "") === rebasePath("/").replace(/\/+$/, "");
  const requestedProject = isNewSessionRoute
    ? new URLSearchParams(location.search).get("project")
    : null;
  const newSessionProjectName = requestedProject || null;
  // Exclude non-composer routes: they also have no `:conversationId`, so they
  // would otherwise light up the "New session" button. A project-prefilled
  // new session belongs to that project row instead of the global nav item.
  const isNewChatPage = isNewSessionRoute && newSessionProjectName == null;
  return {
    isNewChatPage,
    isInboxPage,
    isCanvasPage,
    isTasksPage,
    isUsagePage,
    isSystemStatusPage,
    activeExtensionPageId,
    newSessionProjectName,
  };
}

// Amber / red, mirroring the finding levels; an ok summary renders no
// trailing indicator at all. The dot's accessible name carries the level;
// the count is plain text next to it.
const SYSTEM_STATUS_DOT_CLASS: Record<Exclude<SystemStatusLevel, "ok">, string> = {
  amber: "bg-amber-500",
  red: "bg-red-500",
};

/**
 * The "System status" primary-nav row with its live indicator.
 *
 * Reads the payload-free summary query (no interval — refreshed only by the
 * `system_status_changed` nudge or a socket reconnect), so the sidebar adds
 * no resident poll of its own.
 */
function SystemStatusPrimaryNavLink({
  active,
  onClick,
}: {
  active: boolean;
  onClick: (event: MouseEvent<HTMLAnchorElement>) => void;
}) {
  const { data } = useSystemStatusSummary();
  const level = data?.level ?? "ok";
  const count = data?.findings.length ?? 0;
  return (
    <PrimaryNavLink
      to="/system"
      label="System status"
      icon={ActivityIcon}
      active={active}
      onClick={onClick}
      componentId="sidebar.system_status"
      testId="system-status-nav"
      trailing={
        level === "ok" ? undefined : (
          <span className="ml-auto flex items-center gap-1.5">
            <span
              data-testid="system-status-dot"
              role="img"
              aria-label={`System status: ${level}`}
              className={cn("size-2 shrink-0 rounded-full", SYSTEM_STATUS_DOT_CLASS[level])}
            />
            {count > 0 && (
              <span
                data-testid="system-status-count"
                className={cn(
                  "text-10 font-medium tabular-nums",
                  active ? "text-[var(--sidebar-active-foreground)]" : "text-muted-foreground",
                )}
              >
                {count}
              </span>
            )}
          </span>
        )
      }
    />
  );
}

/**
 * Sidebar — brand mark, "New chat" button, conversations list.
 *
 * Responsive layout (mobile overlay vs desktop push) — see AppShell for
 * the layout side of the contract. Auto-close behavior is also
 * viewport-conditional:
 *
 *   - **Mobile**: navigation actions (New chat, conversation rows)
 *     close the sidebar. The sidebar covers the chat as a full-screen
 *     overlay, so dismissing on action is what reveals the new
 *     destination.
 *   - **Desktop**: navigation actions do NOT close. Only the X button
 *     in the brand row dismisses. Pushing chat content aside to read
 *     scrollback is fine; users typically want the conversations list
 *     to stay visible while they switch around.
 */
/**
 * Compute the set of IDs to add for a shift-click range selection.
 * Returns null when the range can't be computed (missing anchor or id).
 */
export function computeShiftSelectRange(
  visibleIds: readonly string[],
  anchorId: string,
  targetId: string,
): string[] | null {
  const anchorIdx = visibleIds.indexOf(anchorId);
  const targetIdx = visibleIds.indexOf(targetId);
  if (anchorIdx === -1 || targetIdx === -1) return null;
  const [start, end] = anchorIdx < targetIdx ? [anchorIdx, targetIdx] : [targetIdx, anchorIdx];
  return visibleIds.slice(start, end + 1);
}

/** Stable empty array for the pinned-conversations fallback (referential
    equality keeps dependent memos from re-firing while the query loads). */
const EMPTY_CONVERSATIONS: Conversation[] = [];

/**
 * One-time migration of localStorage pins to server-side labels.
 *
 * Pins used to live only in `localStorage` under
 * `PINNED_CONVERSATION_IDS_STORAGE_KEY`. Now they're an `omnigent.pinned`
 * session label so they follow the user across devices. On the first mount
 * after this ships, push any still-local pins the server doesn't already know
 * about (as the label) so no one loses their existing pins.
 *
 * Runs only when `filterHonored` is true — i.e. the server actually applied
 * `?pinned=true`, so it can store server-side pins. A pre-upgrade server that
 * predates this feature ignores the param and returns an unfiltered page;
 * migrating against it would PATCH pins under a key that server can't
 * per-user-scope AND clear the legacy key, so after the eventual server upgrade
 * every pin would read as unpinned. Gating on `filterHonored` keeps the
 * migration inert (localStorage untouched, pins still render via the union in
 * the caller) until the server can honor it — so a UI-before-server upgrade is
 * safe.
 *
 * A legacy id is only dropped from localStorage once its server write is
 * confirmed; anything unwritten (failed, offline, or not-yet-run because the
 * server can't store pins) stays so the next load retries. Runs the writes
 * directly rather than through the mutation hook: this fires once before any
 * user interaction, and it patches the pinned-list cache itself with the
 * confirmed rows — the same cache-patch (not invalidate) strategy
 * `useTogglePinnedConversation` uses, since the `?pinned=true` index lags these
 * writes.
 *
 * @param serverPinnedIds - Ids the server already reports as pinned.
 * @param pinnedLoaded - Whether the server pinned query has settled.
 * @param filterHonored - Whether the server applied the `?pinned=true` filter.
 */
export function useMigrateLocalPinsToServer(
  serverPinnedIds: Set<string>,
  pinnedLoaded: boolean,
  filterHonored: boolean,
  ownedIds?: ReadonlySet<string>,
): void {
  const queryClient = useQueryClient();
  const attempted = useRef(new Set<string>());
  useEffect(() => {
    // Don't migrate until the query settled AND the server proved it honors the
    // pinned filter — an old server ignores it, and migrating there wipes local
    // pins. A later load can retry after the server upgrade.
    if (!pinnedLoaded || !filterHonored) return;
    const legacyIds = readPinnedConversationIds();
    const remaining = legacyIds.filter((id) => !serverPinnedIds.has(id));
    const toMigrate = remaining.filter(
      (id) => !attempted.current.has(id) && (!ownedIds || ownedIds.has(id)),
    );
    // Ids the server already owns can be dropped from the legacy key right away;
    // ids still to migrate stay until their write succeeds (below), so a failed
    // or offline write retries next load instead of losing the pin.
    if (remaining.length === 0) clearLegacyPinnedConversationIds();
    else writeLegacyPinnedConversationIds(remaining);
    if (toMigrate.length === 0) return;
    toMigrate.forEach((id) => attempted.current.add(id));
    void (async () => {
      // Legacy localStorage kept pins most-recently-pinned-first, so preserve
      // that order by synthesizing descending pin timestamps: the oldest pin
      // (last in the list) gets the smallest value and stays at the top of the
      // Pinned section, matching the pre-migration ordering.
      const now = Date.now();
      const results = await Promise.all(
        toMigrate.map((id, i) =>
          setConversationPinned(id, true, now - i)
            .then((conv) => ({ id, conv }))
            .catch(() => ({ id, conv: null as Conversation | null })),
        ),
      );
      // Keep only the ids whose write failed in the legacy key, so the next
      // load retries them; drop the succeeded ones (now server-owned).
      const succeeded = new Set(results.filter((r) => r.conv !== null).map((r) => r.id));
      writeLegacyPinnedConversationIds(
        readPinnedConversationIds().filter((id) => !succeeded.has(id)),
      );
      // Patch the pinned-list cache with the confirmed rows rather than
      // invalidating — the `?pinned=true` index lags these writes, so a refetch
      // here would momentarily drop the just-migrated pins.
      const rows = results.map((r) => r.conv).filter((c): c is Conversation => c != null);
      if (rows.length > 0) {
        queryClient.setQueryData<PinnedConversationsResult>(PINNED_CONVERSATIONS_KEY, (old) => {
          const ids = new Set(rows.map((c) => c.id));
          const prev = old ?? { conversations: [], filterHonored: true };
          return {
            ...prev,
            conversations: [...prev.conversations.filter((c) => !ids.has(c.id)), ...rows],
          };
        });
      }
    })();
    // Retry newly loaded owned IDs; failed writes wait until the next mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pinnedLoaded, filterHonored, ownedIds]);
}

function SidebarImpl({
  open,
  onClose,
  onOpen,
  dragProgress = null,
  onOpenSearch,
  peek,
}: SidebarProps) {
  const navigate = useNavigate();
  const newSessionShortcut = useShortcutHint("newSession");
  const sidebarData = useSidebarData();
  const branding = useBranding();
  const serverInfo = useServerInfo();
  const { showGoalSessionMarkers } = useSessionNavigationPreferences();
  const { data: newSessionProjects } = useProjects();
  const {
    target: newSessionTarget,
    route: newSessionTargetRoute,
    selectNoProject: selectNoProjectNewSessionTarget,
    selectProject: selectProjectNewSessionTarget,
  } = useNewSessionTarget(newSessionProjects);
  const usagePageEnabled = isFeatureEnabled(serverInfo, "usage_page");
  const canvasEnabled = isFeatureEnabled(serverInfo, "canvas");
  const [selectionMode, setSelectionMode] = useState(false);
  // Which rows the current selection targets: the flat "Sessions" list, or the
  // sessions nested inside project folders. Set when selection mode is entered
  // (from the Sessions header or the Projects header kebab, respectively).
  const [selectionScope, setSelectionScope] = useState<SelectionScope>("sessions");
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  // A loopback-only server has one user, so "Shared" is meaningless there —
  // the filter menu drops that option. Mirrors AppShell's `shareDisabled`.
  // Read before the filter state, which validates a stored "shared" against it.
  const multiUser = !isCurrentServerLocal() && sidebarData.sharedAvailable;
  // Active filter from the Sessions heading's menu, seeded from the persisted
  // preference so a reload keeps the slice the viewer was last on.
  const [activeTab, setActiveTab] = useState<SidebarTab>(() => readSessionFilter(multiUser));

  const lastSelectedIdRef = useRef<string | null>(null);
  const getVisibleIdsRef = useRef<() => string[]>(() => []);

  const toggleSelected = useCallback((id: string, shiftKey?: boolean) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (shiftKey && lastSelectedIdRef.current != null) {
        const range = computeShiftSelectRange(
          getVisibleIdsRef.current(),
          lastSelectedIdRef.current,
          id,
        );
        if (range) {
          for (const rid of range) next.add(rid);
          return next;
        }
      }
      if (next.has(id)) next.delete(id);
      else next.add(id);
      lastSelectedIdRef.current = id;
      return next;
    });
  }, []);

  const deselectAll = useCallback(() => {
    setSelectedIds(new Set());
  }, []);

  const exitSelectionMode = useCallback(() => {
    setSelectionMode(false);
    setSelectionScope("sessions");
    setSelectedIds(new Set());
    lastSelectedIdRef.current = null;
  }, []);

  // Enter selection mode targeting a given scope; clear any prior selection so
  // rows from the previous scope don't linger.
  const enterSelectionMode = useCallback((scope: SelectionScope) => {
    setSelectionScope(scope);
    setSelectedIds(new Set());
    lastSelectedIdRef.current = null;
    setSelectionMode(true);
  }, []);

  // Switch the visible scope tab. Selection is a single global set while the
  // tabs show disjoint, ownership-scoped slices, so leaving selection mode on
  // switch keeps the bulk-action count honest with the visible tab (the viewer
  // re-enters per tab) instead of carrying stale rows across. Every path that
  // changes the tab must go through here — not a bare setActiveTab — or the
  // selection cleanup and the persisted preference are skipped (e.g. the
  // "New session" snap-back below).
  const switchTab = useCallback(
    (tab: SidebarTab) => {
      if (selectionMode) exitSelectionMode();
      setActiveTab(tab);
      writeSessionFilter(tab);
    },
    [selectionMode, exitSelectionMode],
  );

  const availableTab = activeTab === "shared" && !sidebarData.sharedAvailable ? "mine" : activeTab;
  useLayoutEffect(() => {
    if (availableTab !== activeTab) switchTab(availableTab);
  }, [activeTab, availableTab, switchTab]);

  useSidebarView(availableTab);
  const archivedQuery = useArchivedSessions(availableTab === "archived");
  const displayQuery: SidebarListQuery =
    availableTab === "archived"
      ? archivedQuery
      : availableTab === "mine"
        ? sidebarData.mine
        : availableTab === "shared"
          ? sidebarData.shared
          : sidebarData.all;
  const inboxCount = sidebarData.inboxCount;
  // Fork feature — the unread set drives the "mark all seen" affordance. A
  // session already framed by an active goal marker is excluded: its row
  // advertises the goal instead, so counting it as unread double-signals.
  useUnseenTick();
  const unreadConversations = sidebarData.loadedRows.filter(
    (conversation) =>
      !(showGoalSessionMarkers && conversation.goal_state === "active") &&
      (isExplicitlyUnread(conversation.id) ||
        isConversationUnseen(
          conversation.id,
          conversation.updated_at,
          getConversationForegroundStatus(conversation),
        )),
  );

  // The scrollable list container — used as the IntersectionObserver root for
  // infinite scroll (auto-loading the next page as the sentinel nears view).
  const scrollContainerRef = useRef<HTMLElement>(null);
  const [hasScrolled, setHasScrolled] = useState(false);
  // Show the scrollbar only while actively scrolling; hide it after a pause.
  const [isScrolling, setIsScrolling] = useState(false);
  const scrollIdleTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const markScrolling = useCallback(() => {
    setIsScrolling(true);
    clearTimeout(scrollIdleTimer.current ?? undefined);
    scrollIdleTimer.current = setTimeout(() => setIsScrolling(false), SCROLLBAR_HIDE_DELAY_MS);
  }, []);
  useEffect(() => () => clearTimeout(scrollIdleTimer.current ?? undefined), []);
  const setScrollContainer = useCallback((node: HTMLElement | null) => {
    scrollContainerRef.current = node;
    setHasScrolled((node?.scrollTop ?? 0) > 0);
  }, []);

  // Row-Link click handler. The Link navigates natively (so modifier/middle
  // clicks open tabs); we only close the drawer on a plain primary click on
  // mobile. Stable identity (useStableCallback) so it doesn't defeat the row memo.
  const onNavClick = useStableCallback((e: MouseEvent<HTMLAnchorElement>) => {
    if (e.defaultPrevented) return;
    if (e.button !== 0) return;
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    if (isMobileViewport()) onClose();
  });

  // Which top-level nav button to highlight for the current route.
  const {
    isNewChatPage,
    isInboxPage,
    isCanvasPage,
    isTasksPage,
    isUsagePage,
    isSystemStatusPage,
    activeExtensionPageId,
    newSessionProjectName,
  } = useActiveNavItem();
  const onNewSessionComposer = isNewChatPage || newSessionProjectName !== null;
  const syncedComposerTargetRef = useRef<string | undefined>(undefined);

  useEffect(() => {
    if (!onNewSessionComposer) {
      syncedComposerTargetRef.current = undefined;
      return;
    }
    if (newSessionProjects === undefined) return;
    const project = newSessionProjects.find(
      (candidate) => candidate.name === newSessionProjectName,
    );
    if (newSessionProjectName !== null && !project) return;
    // Sync each resolved URL scope once. Another tab may select a different
    // target; repeatedly reasserting this tab's URL would ping-pong storage.
    const scope = JSON.stringify([newSessionProjectName, project?.id ?? null]);
    if (syncedComposerTargetRef.current === scope) return;
    syncedComposerTargetRef.current = scope;
    if (newSessionProjectName === null) {
      if (newSessionTarget.kind !== "none") selectNoProjectNewSessionTarget();
      return;
    }
    if (!project) return;
    if (
      newSessionTarget.kind === "project" &&
      newSessionTarget.projectId === project.id &&
      newSessionTarget.projectName === project.name
    ) {
      return;
    }
    selectProjectNewSessionTarget(project);
  }, [
    newSessionProjectName,
    newSessionProjects,
    newSessionTarget,
    onNewSessionComposer,
    selectNoProjectNewSessionTarget,
    selectProjectNewSessionTarget,
  ]);

  const selectProjectTarget = useCallback(
    (project: { id: string | null; name: string }) => {
      selectProjectNewSessionTarget(project);
      if (!onNewSessionComposer) return;
      navigate(
        newSessionRoute({
          kind: "project",
          projectId: project.id,
          projectName: project.name,
        }),
      );
    },
    [navigate, onNewSessionComposer, selectProjectNewSessionTarget],
  );
  const selectNoProjectTarget = useCallback(() => {
    selectNoProjectNewSessionTarget();
    if (!onNewSessionComposer) return;
    navigate("/");
  }, [navigate, onNewSessionComposer, selectNoProjectNewSessionTarget]);

  const routeTarget: NewSessionTarget =
    newSessionProjectName !== null
      ? {
          kind: "project",
          projectId:
            newSessionProjects?.find((project) => project.name === newSessionProjectName)?.id ??
            null,
          projectName: newSessionProjectName,
        }
      : { kind: "none" };
  const displayedNewSessionTarget = onNewSessionComposer ? routeTarget : newSessionTarget;
  const selectedNewSessionProjectName =
    displayedNewSessionTarget.kind === "project" ? displayedNewSessionTarget.projectName : null;
  const noProjectNewSessionTargetSelected = displayedNewSessionTarget.kind === "none";

  // On /settings the card keeps its chrome but swaps the conversation list
  // for the settings section nav (see settingsNav.tsx) — entering settings
  // shouldn't replace the whole sidebar.
  const { inSettings } = useSettingsRoute();
  // Remember the pre-settings location so the Back row returns to the
  // conversation the user was viewing, not the home page. Tracked here since
  // the sidebar stays mounted across the transition into settings.
  useTrackSettingsReturn();

  // Pins are stored on the server as an `omnigent.pinned` session label, so
  // they follow the user across devices. `usePinnedConversations` is the
  // authoritative pinned set (independent of the paginated window); the toggle
  // mutation flips the label and refreshes that query.
  const { data: pinnedData, isSuccess: pinnedLoaded } = sidebarData.pinned;
  // Stable empty fallback so downstream memos don't re-fire on every render
  // while the query is still loading (`pinnedData` undefined).
  const pinnedConversations = useMemo(
    () => pinnedData?.conversations ?? EMPTY_CONVERSATIONS,
    [pinnedData],
  );
  const pinnedFilterHonored = pinnedData?.filterHonored ?? false;
  // Membership is the union of the server's pinned rows and any pins still in
  // the legacy localStorage key — so a not-yet-migrated pin (server too old, or
  // a migration write that hasn't landed) keeps showing in the Pinned section
  // instead of vanishing. The union collapses to just the server set once the
  // migration clears the legacy key. Ordering/rows still come from
  // `pinnedConversations` where available; a legacy-only id renders from the
  // loaded list rows the grouping already has.
  //
  // Caveat: a legacy-only id whose session is OUTSIDE the currently-loaded
  // paginated window has no backing row, so the id is in the pinned set but may
  // not render a row until it's loaded. This is window-scoped and transient —
  // against a new server the migration promotes the id to a real server pinned
  // row (which carries its own row) on the same or next load.
  const ownedPinIds = useMemo(
    () =>
      sidebarData.pinsIncludeShared
        ? undefined
        : new Set(
            filterSessionScope(sidebarData.loadedRows, "mine", getCurrentUserId()).map(
              (row) => row.id,
            ),
          ),
    [sidebarData.pinsIncludeShared, sidebarData.loadedRows],
  );
  const pinnedConversationIds = useMemo(() => {
    const ids = pinnedConversations.map((c) => c.id);
    const seen = new Set(ids);
    for (const id of readPinnedConversationIds())
      if (!seen.has(id) && (!ownedPinIds || ownedPinIds.has(id))) ids.push(id);
    return ids;
    // `pinnedLoaded` isn't read but is a dep on purpose: it re-reads the legacy
    // key after the migration (gated on the query settling) mutates it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pinnedConversations, pinnedLoaded, ownedPinIds]);
  // The migration compares the legacy key against what the SERVER already owns
  // (not the union — a legacy-only id must still count as "to migrate").
  const serverPinnedIdSet = useMemo(
    () => new Set(pinnedConversations.map((c) => c.id)),
    [pinnedConversations],
  );

  // One-time migration: pins used to live only in localStorage. Push any
  // still-local pins up to the server (as the `omnigent.pinned` label) the
  // first time this build runs, so no one loses their existing pins, then
  // clear the legacy key so this runs at most once.
  useMigrateLocalPinsToServer(serverPinnedIdSet, pinnedLoaded, pinnedFilterHonored, ownedPinIds);

  // Desktop-only drag-to-resize, mirroring the right rail. The width is
  // exposed as a CSS variable consumed by the ``md:w-[var(--sidebar-width)]``
  // class so it only applies on desktop — on mobile the sidebar is a
  // full-screen overlay (``fixed inset-0``) and the variable is ignored.
  const { width: sidebarWidth, handleProps: resizeHandleProps } = useResizableSidebar();

  // While the iOS edge-swipe is dragging, the overlay is on-screen and
  // interactive even though `open` hasn't flipped yet — treat a live drag as
  // visually open so it isn't `inert`/`aria-hidden` mid-gesture.
  const dragging = dragProgress != null;
  const effectiveOpen = open || dragging || peek;
  useLayoutEffect(() => {
    if (!effectiveOpen || !isMobileViewport() || !scrollContainerRef.current) return;
    scrollContainerRef.current.scrollTop = 0;
    setHasScrolled(false);
  }, [effectiveOpen]);

  // The mobile drawer is a `fixed inset-0` overlay, so the iOS shell-lock
  // (useIOSViewportLock) — which only resizes flow content inside .app-shell —
  // doesn't lift it above the soft keyboard. Pad the drawer's bottom by the
  // keyboard inset so every session row can still scroll into view while an
  // inline rename holds the keyboard up. No-op off iOS / keyboard closed.
  const keyboardInset = useIOSNativeKeyboardInset(effectiveOpen);

  // While the peek card's entry animation is still fading it in, the card is
  // (nearly) invisible yet already covers the toggle whose hover armed it —
  // taking pointer events then would swallow a click aimed at that toggle,
  // landing it on whatever sidebar content sits under the pointer instead.
  // Stay click-through until the composed entry animation completes.
  // Children's animations bubble too, so only the card's own end unlocks it.
  const [peekInteractive, setPeekInteractive] = useState(false);
  useEffect(() => {
    if (!peek) {
      setPeekInteractive(false);
      return;
    }
    // Do not leave the card click-through if animationend is suppressed or missed.
    const fallback = setTimeout(() => setPeekInteractive(true), 200);
    return () => clearTimeout(fallback);
  }, [peek]);
  const prefersReducedMotion =
    typeof window !== "undefined" &&
    window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

  // While peeking, leaving the card closes it after a short grace period;
  // re-entering before that fires cancels the close so a wobble doesn't
  // dismiss it.
  const peekCloseTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const cancelPeekClose = useCallback(() => {
    if (peekCloseTimer.current) {
      clearTimeout(peekCloseTimer.current);
      peekCloseTimer.current = null;
    }
  }, []);
  useEffect(() => cancelPeekClose, [cancelPeekClose]);

  return (
    <>
      {/* Mobile: the drawer stops short of the right edge, leaving a strip of
      the chat visible; this scrim covers that strip so tapping it dismisses the
      drawer — the gesture that replaced the collapse icon. Full-bleed and
      behind the drawer (z-45 vs z-50), so only the strip is actually reachable.
      Tracks the finger during an edge-swipe drag.

      A labeled <button>, not a bare div: with the collapse toggle gone on
      mobile this is the drawer's only non-navigational way out, so it has to be
      reachable by keyboard and announced by a screen reader, not just findable
      by sighted users with a pointer. Parked, it leaves the tab order and the
      a11y tree via tabIndex/aria-hidden rather than the `inert` the drawer
      uses: React 18 doesn't know `inert` as a boolean attribute and drops it,
      so a focusable control relying on it would stay tabbable while closed.

      z-45, not z-40: chat chrome also sits at z-40 (ChatPage's jump-to-top
      pill) and renders after the sidebar, so a same-z peer that ever takes
      pointer events would win the tie inside the exposed strip and swallow the
      dismiss. Strictly between chat chrome and the drawer's z-50. */}
      {!inSettings && (
        <button
          type="button"
          data-testid="sidebar-scrim"
          aria-label="Close sidebar"
          onClick={onClose}
          tabIndex={effectiveOpen ? 0 : -1}
          aria-hidden={!effectiveOpen}
          className={cn(
            "fixed inset-0 z-[45] bg-black/25 transition-opacity duration-200 ease-out md:hidden",
            effectiveOpen ? "opacity-100" : "pointer-events-none opacity-0",
          )}
          style={dragging ? { opacity: dragProgress, transition: "none" } : undefined}
        />
      )}
      <aside
        aria-label="Conversations"
        onAnimationEnd={(event) => {
          if (event.target === event.currentTarget) setPeekInteractive(true);
        }}
        onPointerEnter={cancelPeekClose}
        onPointerLeave={() => {
          if (!peek) return;
          cancelPeekClose();
          // Defer closing if any context menu is open
          const tryClose = () => {
            if (document.querySelector('[role="menu"][data-state="open"]')) {
              peekCloseTimer.current = setTimeout(tryClose, 200);
              return;
            }
            onClose();
          };
          peekCloseTimer.current = setTimeout(tryClose, 200);
        }}
        className={cn(
          // Base: bg + flex column. No transition — expand/collapse snaps
          // instantly (animating the width also lagged drag-to-resize).
          // conversations-sidebar only matters under the macOS Electron
          // shell, where it pushes the card below the traffic lights
          // (see the [data-electron-mac] rules in index.css).
          "conversations-sidebar flex flex-col bg-card md:select-none",
          // Mobile (default): a fixed drawer that slides in via translate-x
          // and stops short of the right edge, leaving a tappable strip of the
          // chat behind it. The floating-card treatment below is desktop-only.
          // bg-card-solid (opaque): the overlay sits on top of the chat, and
          // WebKit drops the glass rule's backdrop-filter once a Radix popper
          // opens (and never repaints it), letting the chat bleed through the
          // 60%-alpha glass --card. Desktop keeps the translucent bg-card —
          // there the sidebar pushes content aside, so nothing sits behind it.
          "max-md:bg-card-solid",
          // `max-md:right-14` is the ChatGPT-style peek strip: the drawer covers
          // most of the phone but stops short of the right edge so the chat stays
          // visible (and tappable through the scrim) behind it.
          "fixed inset-0 z-50",
          // The settings nav takes the sidebar over and its "Back" row is the
          // only way out, so there the drawer stays full-bleed (and gets no
          // dismiss scrim) rather than offering a tap that strands the user.
          !inSettings && "max-md:right-14",
          // Mobile only: animate the slide so the iOS edge-swipe settles
          // smoothly on release. Suppressed inline while a drag is live (the
          // overlay must track the finger 1:1). Scoped to transform so it can't
          // re-introduce the width-animation lag the base comment warns about,
          // and gated to mobile so the desktop floating card is unaffected.
          "max-md:transition-transform max-md:duration-200 max-md:ease-out",
          effectiveOpen ? "translate-x-0" : "-translate-x-full",
          // Desktop: a full-height panel flush to the window edge, carrying
          // the brand gradient canvas (see html:not(.dark) .conversations-sidebar
          // in index.css) and separated from the white content by a right
          // divider — no outer margin or rounding. Width (the user-resizable
          // variable) animates →0 to push main; when closed the border
          // collapses too so nothing lingers.
          "md:translate-x-0 md:overflow-hidden",
          // Normal desktop flow: relative panel that pushes main. Suppressed while
          // peeking so its `md:inset-auto`/`md:relative` don't override the
          // floating-card positioning below (same `md:` layer, source order wins).
          !peek && "md:relative md:inset-auto",
          open || peek ? "md:m-0 md:w-[var(--sidebar-width)] " : "md:m-0 md:w-0 md:border-0",
          // Peek: float as a card 4px off the viewport edge (capped at 300px wide),
          // ringed and shadowed, sliding+fading in from the left so it reads as an
          // overlay rather than a push.
          peek &&
            "is-peek md:absolute md:inset-2 p-0 md:max-w-[400px] ring-1 ring-border rounded-xl md:shadow-xl animate-in fade-in slide-in-from-left-4 duration-200 ease-out",
          // Click-through while fading in (see peekInteractive above): the
          // click falls through to the header toggle underneath, which pins
          // the sidebar open — what the user aimed for.
          peek && !prefersReducedMotion && !peekInteractive && "pointer-events-none",
        )}
        style={
          {
            "--sidebar-width": `${sidebarWidth}px`,
            ...(keyboardInset > 0 ? { paddingBottom: keyboardInset } : null),
            // Track the finger: map the 0→1 open fraction to translateX
            // -100%→0% and kill the transition so it follows the drag exactly.
            ...(dragging
              ? { transform: `translateX(${(dragProgress - 1) * 100}%)`, transition: "none" }
              : null),
          } as CSSProperties
        }
        // Hide from the accessibility tree when closed so screen readers
        // don't see the empty-state contents while focus is elsewhere.
        aria-hidden={!effectiveOpen}
        data-collapsed={!effectiveOpen || undefined}
        // Match the keyboard-focus story: when closed, the sidebar's
        // children shouldn't receive tabs.
        inert={!effectiveOpen}
      >
        {/* Right-edge resize handle (desktop only), mirroring the right rail's
          left-edge handle. Hidden on mobile, where the sidebar is a
          full-screen overlay with no resize affordance; the parent's ``inert``
          when closed also keeps it from being draggable while collapsed.
          Hidden while peeking too — the peek card is a fixed-width flyout, not
          a resizable panel. */}
        {!peek && (
          <div
            {...resizeHandleProps}
            className="absolute inset-y-0 right-0 z-10 hidden w-1 cursor-col-resize transition-colors hover:bg-primary/30 active:bg-primary/50 md:block"
          />
        )}
        {inSettings ? (
          <SettingsSidebarBody onNavClick={onNavClick} />
        ) : (
          <>
            {/* sidebar-header-row is the hook for the macOS Electron shell, where
          this row shares the window's top strip with the traffic lights: the
          brand mark is dropped and the actions slide left to sit beside the
          window controls (see the [data-electron-mac] rules in index.css).
          Inert in a browser and on other platforms, which keep the row below. */}
            {/* h-14 below md matches the mobile chat header height so the
            Search bubble shares a centerline with the overflow bubble in the
            chat strip beside the open drawer. */}
            <div className="sidebar-header-row flex h-14 shrink-0 items-center justify-between pr-3 pl-4 md:h-12">
              {unreadConversations.length > 0 && !selectionMode && (
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  aria-label="Mark all sessions as read"
                  data-testid="mark-all-sessions-read-mobile"
                  className="size-9 shrink-0 rounded-full p-0 md:hidden max-md:size-11 max-md:text-foreground"
                  onClick={() => markConversationsSeen(unreadConversations)}
                >
                  <MessageCircleCheckIcon className="size-4" />
                </Button>
              )}
              {/* Brand mark doubles as the "home" affordance: clicking it
            returns to `/`, the new-session composer. Without this there
            is no way back to the landing composer once you're inside a
            session. Reuses onNavClick so a plain primary click closes
            the sidebar on mobile (where it's a full-screen overlay) but
            modifier/middle clicks still open `/` in a new tab. */}
              <Link
                to="/"
                onClick={onNavClick}
                data-testid="sidebar-brand"
                componentId="sidebar.home"
                className={cn(
                  "sidebar-brand rounded-none transition-opacity duration-200 ease-[var(--ease-otto)] hover:opacity-70",
                  unreadConversations.length > 0 && !selectionMode && "max-md:hidden",
                )}
              >
                {branding.app_name ? (
                  <span className="text-[15px] font-semibold tracking-tight">
                    {branding.app_name}
                  </span>
                ) : (
                  <img
                    src={omnigentWordmark}
                    alt="Omnigent"
                    data-testid="sidebar-wordmark"
                    className="h-[15px] w-auto shrink-0 translate-y-px dark:invert"
                  />
                )}
              </Link>
              {/* On the macOS shell this copy is hidden and an identical cluster
            renders in the title-bar strip instead (see AppShell), so the icons
            keep their place when the sidebar collapses or peeks. Everywhere
            else this is the only copy. */}
              <SidebarHeaderActions
                expanded={!peek}
                // onOpen is optional (the sidebar renders standalone in tests), so
                // fall back to a no-op rather than widening the child's contract.
                onToggle={peek ? () => onOpen?.() : onClose}
                onOpenSearch={onOpenSearch}
              />
            </div>

            <div className="flex flex-col gap-px px-2 pt-2 pb-0" data-testid="sidebar-primary-nav">
              {/* "New session" routes to the home composer ("/"), which now owns
            session creation end-to-end (host/workspace/worktree chips +
            send). Rendered as a Link so cmd/middle-click opens it in a new
            tab; onNavClick still closes the sidebar on a plain mobile tap. */}
              <Button
                asChild
                className={cn(
                  // px-2 + gap-2 puts the icon on the sidebar's left (red) column
                  // and the label on the label (blue) column — matching section
                  // headers and project folders. border-0 drops the Button base's
                  // transparent 1px border so the icon lands exactly on that
                  // column, flush with the Inbox row and folder rows.
                  SIDEBAR_ROW,
                  "group/new-session w-full justify-start border-0 font-normal",
                  SIDEBAR_HOVER_HIGHLIGHT,
                  isNewChatPage && SIDEBAR_ACTIVE_HIGHLIGHT,
                )}
                variant="ghost"
                data-testid="new-chat-button"
              >
                {/* New session always creates a session the viewer owns, which
              lands under "My sessions" — so snap the tab back there on click
              (the button stays visible on both tabs). */}
                <Link
                  to={newSessionTargetRoute}
                  componentId="sidebar.new_chat"
                  aria-label={`New session in ${newSessionTargetLabel(newSessionTarget)}`}
                  aria-keyshortcuts={newSessionShortcut.aria}
                  onClick={(e) => {
                    switchTab("mine");
                    onNavClick(e);
                  }}
                >
                  <MessageCirclePlusIcon
                    className={cn(
                      "ui-icon",
                      isNewChatPage
                        ? "text-[var(--sidebar-active-foreground)]"
                        : "text-muted-foreground",
                    )}
                  />
                  <span className="min-w-0 flex-1 truncate">New session</span>
                  <span
                    data-testid="new-session-target-label"
                    className="ml-auto max-w-28 truncate text-sm text-muted-foreground transition-opacity group-focus-visible/new-session:opacity-0 [@media((hover:hover)_and_(pointer:fine))]:group-hover/new-session:opacity-0"
                  >
                    {newSessionTargetLabel(newSessionTarget)}
                  </span>
                  <CompactShortcutKeys
                    keys={newSessionShortcut.keys}
                    className="pointer-events-none absolute top-1/2 right-2 -translate-y-1/2 opacity-0 transition-opacity group-focus-visible/new-session:opacity-100 [@media((hover:hover)_and_(pointer:fine))]:group-hover/new-session:opacity-100"
                  />
                </Link>
              </Button>
              {/* Keep Scheduled in the primary nav group with the same row treatment as New session. */}
              <Button
                asChild
                className={cn(
                  // Same shared nav-row construct as "New session" / "Inbox" so
                  // the active-pill, hover, insets, icon column, and text weight
                  // all match post-refactor.
                  SIDEBAR_ROW,
                  "w-full justify-start border-0 font-normal",
                  SIDEBAR_HOVER_HIGHLIGHT,
                  isTasksPage && SIDEBAR_ACTIVE_HIGHLIGHT,
                )}
                variant="ghost"
                data-testid="scheduled-tasks-nav"
              >
                <Link to="/tasks" onClick={onNavClick} componentId="sidebar.tasks">
                  <ClockIcon
                    className={cn(
                      "ui-icon",
                      isTasksPage
                        ? "text-[var(--sidebar-active-foreground)]"
                        : "text-muted-foreground",
                    )}
                  />
                  Automations
                </Link>
              </Button>
              <Button
                asChild
                variant="ghost"
                className={cn(
                  SIDEBAR_ROW,
                  "w-full justify-start border-0 font-normal",
                  SIDEBAR_HOVER_HIGHLIGHT,
                  isInboxPage && SIDEBAR_ACTIVE_HIGHLIGHT,
                )}
                data-testid="inbox-button"
              >
                <Link to="/inbox" onClick={onNavClick} componentId="sidebar.inbox">
                  <InboxIcon
                    className={cn(
                      "ui-icon",
                      isInboxPage
                        ? "text-[var(--sidebar-active-foreground)]"
                        : "text-muted-foreground",
                    )}
                  />
                  Inbox
                  {inboxCount > 0 && (
                    <span
                      aria-label={
                        inboxCount === 1
                          ? "1 inbox item waiting"
                          : `${inboxCount} inbox items waiting`
                      }
                      className={cn(
                        "ml-auto inline-flex h-4 min-w-4 items-center justify-center rounded-full px-1 text-10 font-medium text-[var(--sidebar-active-foreground)] tabular-nums",
                        // The active Inbox row already paints the translucent
                        // --sidebar-active wash; repainting it on the nested
                        // badge would double-composite to a darker fill.
                        isInboxPage ? "bg-transparent" : "bg-[var(--sidebar-active)]",
                      )}
                    >
                      {inboxCount}
                    </span>
                  )}
                </Link>
              </Button>
              {canvasEnabled && (
                <PrimaryNavLink
                  to="/canvas"
                  label="Canvas"
                  icon={LayoutDashboardIcon}
                  active={isCanvasPage}
                  onClick={onNavClick}
                  componentId="sidebar.canvas"
                  testId="canvas-nav"
                />
              )}
              <ExtensionPrimaryNavigation
                activePageId={activeExtensionPageId}
                onNavigate={onNavClick}
              />
              {usagePageEnabled && (
                <Button
                  asChild
                  variant="ghost"
                  className={cn(
                    SIDEBAR_ROW,
                    "w-full justify-start border-0 font-normal",
                    SIDEBAR_HOVER_HIGHLIGHT,
                    isUsagePage && SIDEBAR_ACTIVE_HIGHLIGHT,
                  )}
                  data-testid="usage-nav"
                >
                  <Link to="/usage" onClick={onNavClick} componentId="sidebar.usage">
                    <WalletIcon
                      className={cn(
                        "ui-icon",
                        isUsagePage
                          ? "text-[var(--sidebar-active-foreground)]"
                          : "text-muted-foreground",
                      )}
                    />
                    Usage
                  </Link>
                </Button>
              )}
              <SystemStatusPrimaryNavLink active={isSystemStatusPage} onClick={onNavClick} />
            </div>

            {/* Wrapper (not the `aside`) anchors the floating Settings button:
          absolute-positioning inside the aside would place it in the native
          safe-area padding, under the home indicator. */}
            <div className="relative flex min-h-0 flex-1 flex-col">
              <div
                aria-hidden="true"
                data-testid="sidebar-scroll-divider"
                className={cn(
                  "pointer-events-none absolute inset-x-0 top-0 z-10 h-px bg-border",
                  hasScrolled ? "opacity-100" : "opacity-0",
                )}
              />
              <nav
                ref={setScrollContainer}
                onScroll={(event) => {
                  setHasScrolled(event.currentTarget.scrollTop > 0);
                  markScrolling();
                }}
                // max-md:pb-16 is the floating Settings chip's clearance: the
                // chip is a non-scrolling sibling pinned bottom-right, so
                // without a gutter the last row's always-visible kebab parks
                // underneath it and can't be tapped.
                className={cn(
                  "relative flex-1 overflow-y-auto px-2 pt-4 pb-3 max-md:pb-16 md:mr-1",
                  // Reserve the gutter so toggling the thumb never reflows the list.
                  "[scrollbar-width:thin] [&::-webkit-scrollbar]:w-2 [&::-webkit-scrollbar-thumb]:rounded-full [&::-webkit-scrollbar-track]:bg-transparent",
                  isScrolling
                    ? "[scrollbar-color:var(--muted-foreground)_transparent] [&::-webkit-scrollbar-thumb]:bg-muted-foreground"
                    : "[scrollbar-color:transparent_transparent] [&::-webkit-scrollbar-thumb]:bg-transparent",
                )}
              >
                {sidebarData.identityReady ? (
                  <ConversationList
                    conversationsQuery={displayQuery}
                    unreadConversations={unreadConversations}
                    scrollContainerRef={scrollContainerRef}
                    onRowClick={onNavClick}
                    searchQuery=""
                    selectedNewSessionProjectName={selectedNewSessionProjectName}
                    noProjectNewSessionTargetSelected={noProjectNewSessionTargetSelected}
                    onSelectProjectNewSessionTarget={selectProjectTarget}
                    onSelectNoProjectNewSessionTarget={selectNoProjectTarget}
                    activeTab={availableTab}
                    onActiveTabChange={switchTab}
                    multiUser={multiUser}
                    pinnedConversationIds={pinnedConversationIds}
                    pinnedConversations={pinnedConversations}
                    pinReorderEnabled={pinnedFilterHonored}
                    onEnterSelectionMode={enterSelectionMode}
                    selectionMode={selectionMode}
                    selectionScope={selectionScope}
                    selectedIds={selectedIds}
                    onToggleSelected={toggleSelected}
                    onDeselectAll={deselectAll}
                    onExitSelectionMode={exitSelectionMode}
                    getVisibleIdsRef={getVisibleIdsRef}
                  />
                ) : (
                  <p role="status" className="px-2 py-1 text-muted-foreground text-sm">
                    Loading sessions…
                  </p>
                )}
              </nav>
              {/* Mobile: Settings floats over the bottom of the session list, with
          Search floating at the top of the header row — the two icons the
          drawer keeps once the collapse toggle is gone. */}
              <SidebarSettingsButton
                testId="sidebar-settings-float"
                className="absolute right-3 bottom-3 md:hidden"
              />
            </div>

            {/* Native-shell server picker, pinned below the scrolling session
          list. Self-hiding: renders nothing outside a shell with the picker
          bridge (see SidebarServerPicker), so browsers keep an unchanged
          sidebar that ends with the list. */}
            <SidebarServerPicker />
          </>
        )}
      </aside>
    </>
  );
}

// Memoized so AppShell's frequent re-renders (chatStore status churn during a
// bind) don't re-render the whole sidebar — its props are stable per switch
// (AppShell stabilizes the callbacks with useCallback).
export const Sidebar = memo(SidebarImpl);

/**
 * Auto-loading pagination control. An IntersectionObserver fetches the next
 * page when this nears view (rooted on the scroll container, pre-fetching 200px
 * early for smoothness); the button stays clickable as an a11y /
 * no-IntersectionObserver fallback. Renders nothing once there's no more to
 * load. Shared by the global list and each project folder.
 */
const projectDragId = (name: string) => `project-order:${name}`;
/** The sortable/droppable id of one row inside a favorites section. */
const favItemId = (sectionId: string, type: "session" | "project", id: string) =>
  `fav-item:${sectionId}:${type}:${id}`;
type ProjectHeaderDrag = ReturnType<typeof useSortable>;

/**
 * One project folder. Fetches its own sessions server-side (`?project=`) so it
 * shows ALL its members regardless of how far the global sidebar list has been
 * scrolled, paginated with its own infinite-scroll sentinel. Lazy: the fetch is
 * gated on `expanded`, so a collapsed folder costs nothing. The collapsed
 * `marker` is supplied by the parent (best-effort, from the globally-loaded
 * window) since a collapsed folder hasn't fetched yet.
 */
interface ProjectFolderOrdering {
  /** Disables the reorder menu items (a section's own order in alphabetical mode). */
  disabled: boolean;
  /** Disables the header sortable; defaults to `disabled`. Dragging stays on
      even when the menu is off, so a folder can still be moved to a section. */
  dragDisabled?: boolean;
  insertion?: "before" | "after";
  move: (destination: "up" | "down" | "top" | "bottom") => void;
  first: boolean;
  last: boolean;
}

function ProjectFolder({
  name,
  projectId,
  icon,
  windowConversations,
  activeConversationId,
  expanded,
  active,
  onSelectNewSessionTarget,
  onToggleCollapsed,
  pinnedConversationIds,
  activeOverride,
  frozenSortKeys,
  scrollRoot,
  onRowClick,
  onTogglePinned,
  selectionMode,
  selectedIds,
  onToggleSelected,
  onProjectAssigned,
  onConversationsLoaded,
  ordering,
  favoriteItem,
  headerDrag: headerDragOverride,
  projectOrderId,
  folderInstanceKey,
  rowMeta,
}: {
  /** Absent in a user `projects` section, whose membership order is the
      section's own `projectIds`. */
  ordering?: ProjectFolderOrdering;
  name: string;
  /** First-class project id, or null for a label-only folder. */
  projectId: string | null;
  /** Chosen emoji icon (unicode grapheme), or null/absent for the default
      folder glyph. */
  icon?: string | null;
  /** This folder's members from the globally-loaded window (may lag or lead
      the folder's own pages — e.g. a just-moved row carries its optimistic
      membership here before the folder query returns it). */
  windowConversations: Conversation[];
  /** The active conversation's resolved top-level root id (see
      ConversationSection) — forwarded to the folder's section for row highlight. */
  activeConversationId: string | null;
  expanded: boolean;
  /** Whether the new-session composer is currently scoped to this project. */
  active: boolean;
  /** Select this project as the destination for global new-session actions. */
  onSelectNewSessionTarget: () => void;
  onToggleCollapsed: () => void;
  pinnedConversationIds: string[];
  activeOverride: ActiveChatOverride | null;
  /** Pointer-inside sort-key freeze shared with the flat list (see
      ConversationList); null while the pointer is outside the list. */
  frozenSortKeys: Map<string, number> | null;
  scrollRoot: RefObject<HTMLElement | null>;
  onRowClick: (e: MouseEvent<HTMLAnchorElement>) => void;
  onTogglePinned: (conversationId: string) => void;
  selectionMode: boolean;
  selectedIds: Set<string>;
  onToggleSelected: (conversationId: string, shiftKey?: boolean) => void;
  onProjectAssigned?: (projectName: string) => void;
  /** Report this folder's own loaded (rendered) sessions to the parent. The
      folder paginates independently of the global window, so bulk-selection in
      the projects scope must resolve selected rows against these — not the
      global list — or an out-of-window member would silently drop from the
      action. The copy's instance key keeps a favorite copy's rows separate from
      the owning section copy's. */
  onConversationsLoaded?: (
    name: string,
    conversations: Conversation[],
    instanceKey?: string,
  ) => void;
  /** Set on a favorites copy: the section it sits in and its row index. The
      copy is a reorder target (not a project drop target) and disables its
      header's project-order drag. */
  favoriteItem?: { sectionId: string; index: number };
  /** Overrides the header's own project-order sortable (a favorites copy drags
      as a favorites item instead). */
  headerDrag?: ProjectHeaderDrag;
  /** Overrides the header sortable id so a copy doesn't collide with the
      section copy's `project-order:<name>` registration. */
  projectOrderId?: string;
  /** The folder copy's identity for registration / child row keys. */
  folderInstanceKey?: string;
  /** Per-row identity for a copy whose rows aren't canonical (favorites). */
  rowMeta?: (conversation: Conversation) => ConversationRowMeta;
}) {
  const query = useProjectSessions(name, expanded);
  const { registerFolder } = useSidebarData();
  const hidePinnedHomeCopies =
    useSidebarLayoutContext()?.layout.sections.some((section) => section.kind === "favorites") ===
    true;
  const watchedRows = useMemo(
    () => query.data?.pages.flatMap((page) => page.data) ?? [],
    [query.data],
  );
  useEffect(() => {
    if (expanded) registerFolder(name, watchedRows, folderInstanceKey);
  }, [expanded, name, watchedRows, registerFolder, folderInstanceKey]);
  useEffect(
    () => () => registerFolder(name, null, folderInstanceKey),
    [expanded, name, registerFolder, folderInstanceKey],
  );
  const conversations = useMemo(() => {
    // Union the folder's own pages with its members from the globally-loaded
    // window, window rows winning: those carry the move overlay
    // (useMoveToProject), so a just-filed session shows here immediately
    // instead of waiting out the PATCH + folder refetch round-trips.
    const byId = new Map<string, Conversation>();
    for (const c of query.data?.pages.flatMap((page) => page.data) ?? []) byId.set(c.id, c);
    for (const c of windowConversations) byId.set(c.id, c);
    // Pinning changes where the row is shown, not its project membership.
    return sortByUpdatedAtDesc(
      [...byId.values()].filter(
        (c) => !hidePinnedHomeCopies || !pinnedConversationIds.includes(c.id),
      ),
      activeOverride,
      frozenSortKeys,
    );
  }, [
    query.data,
    windowConversations,
    hidePinnedHomeCopies,
    pinnedConversationIds,
    activeOverride,
    frozenSortKeys,
  ]);
  const errors = useSessionErrorStates(conversations);
  const startingConversationId = useChatStore((s) =>
    s.status === "streaming" || s.terminalPending ? s.conversationId : null,
  );
  const { showGoalSessionMarkers } = useSessionNavigationPreferences();
  const marker = projectMarkerState(
    conversations,
    errors,
    startingConversationId,
    showGoalSessionMarkers,
  );

  // Publish the folder's rendered rows upward so projects-scope bulk selection
  // resolves them (the parent sources its action set from these, not the global
  // paginated window).
  useEffect(() => {
    onConversationsLoaded?.(name, conversations, folderInstanceKey);
  }, [name, conversations, onConversationsLoaded, folderInstanceKey]);

  // While the first page loads, show a "Loading…" footer instead of the "No
  // chats" empty state (which would otherwise flash before rows arrive).
  const loadingFirstPage = expanded && query.isLoading;

  // The whole folder (collapsed header included) is a drop target: releasing a
  // dragged session anywhere on it files the session into this project. The
  // `project:` prefix keeps the droppable id clear of conversation ids (the
  // draggable ids) and the ungroup sentinel. A favorite project's header has
  // its own sortable target; the folder body still accepts session drops.
  const { setNodeRef, isOver } = useDroppable({
    id: `project:${name}`,
    data: { type: "project", name },
  });

  const { actions: menuActions, dialogs: menuDialogs } = useProjectFolderMenu(
    name,
    projectId,
    icon,
  );

  const ownHeaderDrag = useSortable({
    id: projectOrderId ?? projectDragId(name),
    data: { type: "project-order", name },
    disabled:
      headerDragOverride !== undefined || (ordering?.dragDisabled ?? ordering?.disabled ?? true),
  });
  const headerDrag = headerDragOverride ?? ownHeaderDrag;
  const orderedMenuActions = ordering === undefined ? menuActions : { ...menuActions, ordering };

  return (
    <div
      ref={setNodeRef}
      className={cn(
        "relative rounded-[var(--radius-otto-sm)] transition-colors duration-200 ease-[var(--ease-otto)]",
        // Subtle background tint on drag-over — no border, no shadow. A
        // favorites copy only reorders, so it doesn't take the file highlight.
        favoriteItem === undefined && isOver && DROP_TARGET_HIGHLIGHT,
      )}
    >
      {ordering?.insertion && (
        <span
          data-testid="project-order-insertion"
          className="pointer-events-none absolute inset-x-0 z-10 h-0.5 bg-primary"
          style={ordering.insertion === "before" ? { top: 0 } : { bottom: 0 }}
        />
      )}
      <ConversationSection
        headerDrag={headerDrag}
        title={name}
        icon={
          icon ? (
            <span className="text-[14px] leading-none">{icon}</span>
          ) : expanded ? (
            <FolderOpenIcon
              className={cn(
                "ui-icon",
                active ? "text-[var(--sidebar-active-foreground)]" : "text-muted-foreground",
              )}
            />
          ) : (
            <FolderIcon
              className={cn(
                "ui-icon",
                active ? "text-[var(--sidebar-active-foreground)]" : "text-muted-foreground",
              )}
            />
          )
        }
        active={active}
        onSelect={onSelectNewSessionTarget}
        selectionLabel={`Use ${name} for new sessions`}
        marker={marker.state}
        backgroundActivityCount={marker.backgroundActivityCount}
        conversations={conversations}
        activeConversationId={activeConversationId}
        pinnedConversationIds={pinnedConversationIds}
        // Projects default collapsed: shown only when explicitly expanded.
        collapsed={!expanded}
        onToggleCollapsed={onToggleCollapsed}
        onRowClick={onRowClick}
        onTogglePinned={onTogglePinned}
        selectionMode={selectionMode}
        selectedIds={selectedIds}
        onToggleSelected={onToggleSelected}
        onProjectAssigned={onProjectAssigned}
        rowMeta={rowMeta}
        emptyMessage={
          loadingFirstPage ? undefined : (
            <span className="block text-ui">
              No sessions. Start a{" "}
              <Link
                to={`/?project=${encodeURIComponent(name)}`}
                className="font-medium text-primary underline-offset-4 hover:underline"
                onClick={(e) => {
                  e.stopPropagation();
                  if (isPlainNavigationClick(e)) onSelectNewSessionTarget();
                  onRowClick(e);
                }}
              >
                new session
              </Link>
              .
            </span>
          )
        }
        indentRows
        headerAction={
          <ProjectFolderActions
            projectName={name}
            projectId={projectId}
            onNavigate={onRowClick}
            onSelectTarget={onSelectNewSessionTarget}
            actions={orderedMenuActions}
          />
        }
        headerContextMenu={
          <ContextMenuContent className="min-w-40">
            <ProjectFolderMenuItems
              components={contextBundle}
              projectName={name}
              projectId={projectId}
              onNavigate={onRowClick}
              onSelectTarget={onSelectNewSessionTarget}
              actions={orderedMenuActions}
            />
          </ContextMenuContent>
        }
        footer={
          loadingFirstPage ? (
            <p className="px-2 py-1 pl-5 text-muted-foreground text-sm">Loading…</p>
          ) : (
            <InfiniteScrollSentinel
              hasMore={query.hasNextPage}
              isFetching={query.isFetchingNextPage}
              fetchMore={query.fetchNextPage}
              scrollRoot={scrollRoot}
              scopeKey={name}
              indent
            />
          )
        }
      />
      {menuDialogs}
    </div>
  );
}

/**
 * A project ref rendered inside a favorites section: a copy of the project
 * folder that reorders as a `fav-item` and expands under its own
 * `fav:<sectionId>:<name>` key, independent of the folder's section copy. Its
 * rows are copies too, so only the section copy owns project drops / ordering.
 */
function FavoriteProjectCopy({
  entry,
  sectionId,
  index,
  canonicalInstanceKeys,
  expanded,
  onToggleCollapsed,
  windowConversations,
  activeConversationId,
  selectedNewSessionProjectName,
  onSelectProjectNewSessionTarget,
  pinnedConversationIds,
  activeOverride,
  frozenSortKeys,
  scrollRoot,
  onRowClick,
  onTogglePinned,
  selectionMode,
  selectedIds,
  onToggleSelected,
  onProjectAssigned,
  onConversationsLoaded,
}: {
  entry: ResolvedProjectGroup;
  sectionId: string;
  index: number;
  canonicalInstanceKeys: Map<string, string>;
  expanded: boolean;
  onToggleCollapsed: () => void;
  windowConversations: Conversation[];
  activeConversationId: string | null;
  selectedNewSessionProjectName: string | null;
  onSelectProjectNewSessionTarget: (project: { id: string | null; name: string }) => void;
  pinnedConversationIds: string[];
  activeOverride: ActiveChatOverride | null;
  frozenSortKeys: Map<string, number> | null;
  scrollRoot: RefObject<HTMLElement | null>;
  onRowClick: (e: MouseEvent<HTMLAnchorElement>) => void;
  onTogglePinned: (conversationId: string) => void;
  selectionMode: boolean;
  selectedIds: Set<string>;
  onToggleSelected: (conversationId: string, shiftKey?: boolean) => void;
  onProjectAssigned?: (projectName: string) => void;
  onConversationsLoaded?: (
    name: string,
    conversations: Conversation[],
    instanceKey?: string,
  ) => void;
}) {
  const group = entry.group;
  const headerDrag = useSortable({
    id: favItemId(sectionId, "project", group.id ?? group.name),
    data: {
      type: "fav-item",
      sectionId,
      refType: "project",
      refId: group.id ?? group.name,
      index,
      label: group.name,
    },
  });
  const rowMeta = (conversation: Conversation): ConversationRowMeta => {
    const key = `fav:${sectionId}:${conversation.id}`;
    const isCanonical = canonicalInstanceKeys.get(conversation.id) === key;
    return { instanceKey: isCanonical ? conversation.id : key, canonical: isCanonical };
  };
  return (
    <ProjectFolder
      name={group.name}
      projectId={group.id}
      icon={group.icon}
      windowConversations={windowConversations}
      activeConversationId={activeConversationId}
      expanded={expanded}
      active={selectedNewSessionProjectName === group.name}
      onSelectNewSessionTarget={() =>
        onSelectProjectNewSessionTarget({ id: group.id, name: group.name })
      }
      onToggleCollapsed={onToggleCollapsed}
      pinnedConversationIds={pinnedConversationIds}
      activeOverride={activeOverride}
      frozenSortKeys={frozenSortKeys}
      scrollRoot={scrollRoot}
      onRowClick={onRowClick}
      onTogglePinned={onTogglePinned}
      selectionMode={selectionMode}
      selectedIds={selectedIds}
      onToggleSelected={onToggleSelected}
      onProjectAssigned={onProjectAssigned}
      onConversationsLoaded={onConversationsLoaded}
      favoriteItem={{ sectionId, index }}
      headerDrag={headerDrag}
      projectOrderId={`fav-project-own:${sectionId}:${group.id ?? group.name}`}
      folderInstanceKey={`fav:${sectionId}`}
      rowMeta={rowMeta}
    />
  );
}

interface ConversationListProps {
  conversationsQuery: SidebarListQuery;
  unreadConversations: Conversation[];
  // The scrollable ancestor, used as the infinite-scroll observer root.
  scrollContainerRef: RefObject<HTMLElement | null>;
  onRowClick: (e: MouseEvent<HTMLAnchorElement>) => void;
  searchQuery: string;
  /** Persisted target, overridden while an explicit new-session route is open. */
  selectedNewSessionProjectName: string | null;
  noProjectNewSessionTargetSelected: boolean;
  onSelectProjectNewSessionTarget: (project: { id: string | null; name: string }) => void;
  onSelectNoProjectNewSessionTarget: () => void;
  activeTab: SidebarTab;
  onActiveTabChange: (tab: SidebarTab) => void;
  /** Multi-user server; gates the "Shared" filter option. */
  multiUser: boolean;
  pinnedConversationIds: string[];
  // The server-authoritative pinned sessions, so a pinned session that sits
  // outside the loaded pagination window still renders in the Pinned section.
  pinnedConversations: Conversation[];
  // False against a server that can't store pins (they live in localStorage,
  // which has no order), so drag-to-reorder is off there.
  pinReorderEnabled: boolean;
  onEnterSelectionMode: (scope: SelectionScope) => void;
  selectionMode: boolean;
  selectionScope: SelectionScope;
  selectedIds: Set<string>;
  onToggleSelected: (conversationId: string, shiftKey?: boolean) => void;
  onDeselectAll: () => void;
  onExitSelectionMode: () => void;
  getVisibleIdsRef: RefObject<() => string[]>;
}

// A project folder and the rows it holds. The sidebar resolves each layout
// section once into this shape so the render loop, keyboard order, polling and
// shift-select all walk the same projection.
interface SidebarProjectGroup {
  id: string | null;
  name: string;
  icon?: string | null;
  conversations: Conversation[];
}

interface ResolvedProjectGroup {
  group: SidebarProjectGroup;
  /** The folder's own rendered rows, falling back to the global window subset. */
  rows: Conversation[];
  /** Set on a favorites project copy: its rows' instance-key prefix. */
  instanceKeyPrefix?: string;
}

interface ResolvedSection {
  section: SidebarSectionDef;
  collapsed: boolean;
  groups: ResolvedProjectGroup[];
  flatSessions: Conversation[];
  /** Favorites: the ordered refs (sessions + projects) the section renders. */
  favoriteOrder?: FavoriteRef[];
}

/** Per-row identity for a section that may render a session more than once. */
interface ConversationRowMeta {
  /** Unique dnd id for this copy; equals the session id on the canonical copy. */
  instanceKey: string;
  /** Whether this copy owns the active-row scroll / drag registration. */
  canonical: boolean;
  /** Trailing muted project name, set on recent copies. */
  projectLabel?: string;
  /** Favorites copy: registers the pin-reorder target and drags to reorder pins. */
  pinReorderCopy?: boolean;
  /** Sessions copy: still drags (to file / unfile) even when non-canonical. */
  moveCopy?: boolean;
  dragDisabled?: boolean;
}

/** A freshly created favorites section; empty, so every pin renders in pin order. */
function newFavoritesSection(): SidebarSectionDef {
  return { id: newSectionId(), kind: "favorites", name: "Favorites", maxRows: 10, items: [] };
}

/** Ensure exactly one favorites section, created at the top when none exists. */
function ensureFavoritesSection(current: SidebarLayout): SidebarLayout {
  return insertSection(current, newFavoritesSection());
}

/** Re-insert a session ref at its old slot, but leave the implicit default favorites untouched. */
function insertFavoriteRefAt(
  current: SidebarLayout,
  ref: FavoriteRef,
  index: number,
): SidebarLayout {
  const favorites = current.sections.find((section) => section.kind === "favorites");
  if (favorites === undefined || favorites.implicit === true) return current;
  const items = favorites.items ?? [];
  if (items.some((item) => item.type === ref.type && item.id === ref.id)) return current;
  const at = Math.max(0, Math.min(index, items.length));
  const next = [...items.slice(0, at), ref, ...items.slice(at)];
  return {
    ...current,
    sections: current.sections.map((section) =>
      section.id === favorites.id ? { ...section, items: next } : section,
    ),
  };
}

/**
 * Reorder a favorites section's stored `items` to the moved resolved order
 * `moved`, replacing each still-resolved ref in place. A ref that no longer
 * resolves (a deleted project, a session unpinned elsewhere) keeps its position;
 * a `moved` entry with no stored counterpart (a pin that had no ref) appends.
 */
function reorderFavoriteItems(
  stored: readonly FavoriteRef[],
  moved: readonly FavoriteRef[],
  isResolved: (ref: FavoriteRef) => boolean,
): FavoriteRef[] {
  const items: FavoriteRef[] = [];
  let cursor = 0;
  for (const ref of stored) {
    if (!isResolved(ref)) {
      items.push(ref);
      continue;
    }
    items.push(moved[cursor] ?? ref);
    cursor += 1;
  }
  for (; cursor < moved.length; cursor += 1) {
    const ref = moved[cursor];
    if (ref !== undefined) items.push(ref);
  }
  return items;
}

/**
 * The session a favorites drag anchors on for the pin-timestamp rewrite: the
 * session the moved one lands beside. A downward move lands the moved session
 * just after its anchor, so the anchor is the nearest session before it; an
 * upward move lands it just before, so the anchor is the nearest session after.
 * A project-ref target holds no pin slot, so the anchor is the nearest session
 * on that side.
 */
function favoriteReorderAnchor(
  order: readonly FavoriteRef[],
  id: string,
  downward: boolean,
): string | null {
  const index = order.findIndex((ref) => ref.type === "session" && ref.id === id);
  if (index < 0) return null;
  if (downward) {
    for (let i = index - 1; i >= 0; i -= 1) {
      const ref = order[i];
      if (ref !== undefined && ref.type === "session") return ref.id;
    }
  } else {
    for (let i = index + 1; i < order.length; i += 1) {
      const ref = order[i];
      if (ref !== undefined && ref.type === "session") return ref.id;
    }
  }
  return null;
}

function ConversationList({
  conversationsQuery,
  unreadConversations,
  scrollContainerRef,
  onRowClick,
  searchQuery,
  selectedNewSessionProjectName,
  noProjectNewSessionTargetSelected,
  onSelectProjectNewSessionTarget,
  onSelectNoProjectNewSessionTarget,
  activeTab,
  onActiveTabChange,
  multiUser,
  pinnedConversationIds,
  pinnedConversations,
  pinReorderEnabled,
  onEnterSelectionMode,
  selectionMode,
  selectionScope,
  selectedIds,
  onToggleSelected,
  onDeselectAll,
  onExitSelectionMode,
  getVisibleIdsRef,
}: ConversationListProps) {
  // Row-invariant values resolved once here and shared with rows via context
  // (see IsMobileContext etc.), so each row doesn't run its own copy.
  const viewerId = useViewerId();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const isMobile = useIsMobileViewport();
  const serverInfo = useServerInfo();
  const { layout, saveLayout } = useSidebarLayout();
  // Host metadata is shared by every row tooltip. Resolve it once at the list
  // owner so ordinary rows do not each create their own polling observer.
  const { data: hosts = [] } = useHosts({ includeSandbox: true });
  const hostsById = useMemo(
    () => new Map(hosts.map((host) => [host.host_id, host] as const)),
    [hosts],
  );
  // All loaded conversations from the single paginated list (for the flat
  // session list; pinned rows are merged in from the server pinned query).
  const allConversations = useMemo(
    () => conversationsQuery.data?.pages.flatMap((page) => page.data) ?? [],
    [conversationsQuery.data],
  );
  // Project folders ({ id, name }) for grouping sessions — first-class id
  // and/or the legacy omni_project label, unioned server-side.
  const { data: projects = [] } = useProjects();
  const projectOrder = useProjectOrder();
  const saveOrder = useSaveProjectOrder();
  const [draggedProject, setDraggedProject] = useState<string | null>(null);
  const [draggedSection, setDraggedSection] = useState<string | null>(null);
  // A favorite project copy being dragged to reorder inside the favorites
  // section (distinct from `draggedProject`, which moves between sections).
  const [draggedFavorite, setDraggedFavorite] = useState<{ refId: string; label: string } | null>(
    null,
  );
  const dragOrigin = useRef<{ left: number; top: number; width: number } | undefined>(undefined);
  const [overProject, setOverProject] = useState<string | null>(null);
  const [overSection, setOverSection] = useState<string | null>(null);
  const [overPin, setOverPin] = useState<string | null>(null);
  const { mutateAsync: pinAt } = useTogglePinnedConversation();
  // Pin writes don't overlap (the hooks refuse one while another saves), so the
  // pinned rows stop taking drops until the current write settles.
  const pinWriting = useIsMutating({ mutationKey: PIN_WRITE_MUTATION_KEY }) > 0;
  const { mutate: reorderPins } = useReorderPinnedConversations();

  // Pin (server) plus the favorites layout in one action. The pin path owns the
  // cap / ownership checks and the optimistic cache move; the layout is touched
  // only once the pin is accepted — a refused or failed pin leaves it untouched.
  // A session add needs no ref (an unreferenced pin already appends in pin
  // order), so it only creates a favorites section when none exists; a remove
  // drops the ref only when one exists and the section is explicit.
  const pinFavorite = useCallback(
    (id: string) => {
      pinAt({ id, pinned: true })
        .then(() => saveLayout(ensureFavoritesSection))
        .catch(() => {});
    },
    [pinAt, saveLayout],
  );
  const unpinFavorite = useCallback(
    (id: string) => {
      // Remember where the session's ref sat when the drop removes it, so an
      // Undo can restore it among the section's project refs, not append it.
      let removed: { ref: FavoriteRef; index: number } | null = null;
      unpinWithUndo(
        queryClient,
        pinAt,
        id,
        pinnedConversations.find((c) => c.id === id),
        () => {
          saveLayout((current) => {
            const favorites = current.sections.find((section) => section.kind === "favorites");
            if (favorites === undefined || favorites.implicit === true) return current;
            const items = favorites.items ?? [];
            const index = items.findIndex((ref) => ref.type === "session" && ref.id === id);
            if (index < 0) return current;
            removed = { ref: items[index]!, index };
            return removeFavorite(current, { type: "session", id });
          });
        },
        () => {
          if (removed === null) return;
          const { ref, index } = removed;
          saveLayout((current) => insertFavoriteRefAt(current, ref, index));
        },
      );
    },
    [pinAt, queryClient, pinnedConversations, saveLayout],
  );
  const favoriteActions = useMemo(
    () => ({ add: pinFavorite, remove: unpinFavorite }),
    [pinFavorite, unpinFavorite],
  );
  const sidebarLayoutContextValue = useMemo(
    () => ({
      layout,
      saveLayout,
      addFavorite: favoriteActions.add,
      removeFavorite: favoriteActions.remove,
    }),
    [layout, saveLayout, favoriteActions],
  );

  // Any pin path (quick button, mobile Pin item, hotkeys, favorites menu)
  // routes through here.
  const handleTogglePinned = useStableCallback((conversationId: string) => {
    if (pinnedConversationIds.includes(conversationId)) unpinFavorite(conversationId);
    else pinFavorite(conversationId);
  });
  const moveProject = (name: string, destination: "up" | "down" | "top" | "bottom") => {
    if (saveOrder.isPending) return;
    const from = projects.findIndex((p) => p.name === name);
    const to =
      destination === "top"
        ? 0
        : destination === "bottom"
          ? projects.length - 1
          : destination === "up"
            ? from - 1
            : from + 1;
    if (from < 0 || to < 0 || to >= projects.length || from === to) return;
    saveOrder.mutate(arrayMove(projects, from, to));
  };

  // id → name for the rows' project_id lookup, built once here and shared via
  // context so a row doesn't subscribe to useProjects() itself.
  const projectNamesById = useMemo(() => {
    const map = new Map<string, string>();
    for (const p of projects) {
      if (p.id !== null) map.set(p.id, p.name);
    }
    return map;
  }, [projects]);

  // id → emoji icon for rows that want to show the real project glyph (e.g. the
  // pinned flyout); built alongside the names map and shared the same way.
  const projectIconsById = useMemo(() => {
    const map = new Map<string, string>();
    for (const p of projects) {
      if (p.id !== null && p.icon) map.set(p.id, p.icon);
    }
    return map;
  }, [projects]);

  // Freeze the active chat's sort key while you're inside it so an
  // updated_at bump from sending a message doesn't reorder the row
  // out from under you. Snapshot is dropped on navigate-away so the
  // chat snaps back to its real position once you've left.
  const { conversationId: activeId } = useParams<{ conversationId: string }>();
  // Resolve the active conversation's top-level root once here (rows get a plain
  // `isActive` prop, not their own query). Falls back to the raw id while the
  // parent walk loads — a top-level session resolves to itself.
  const activeRootSessionId = useActiveRootSessionId(activeId ?? null);
  const resolvedActiveId = activeRootSessionId ?? activeId ?? null;
  const [optimisticActiveId, setOptimisticActiveId] = useState<string | null>(null);
  useEffect(() => setOptimisticActiveId(null), [activeId]);
  const activateRow = useCallback((id: string, event: MouseEvent<HTMLAnchorElement>) => {
    if (event.defaultPrevented || event.button !== 0) return;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    setOptimisticActiveId(id);
  }, []);
  const displayedActiveId = optimisticActiveId ?? resolvedActiveId;
  const [activeOverride, setActiveOverride] = useState<ActiveChatOverride | null>(null);
  useEffect(() => {
    setActiveOverride((prev) => computeNextActiveOverride(activeId, allConversations, prev));
  }, [activeId, allConversations]);

  // While the pointer is inside the list OR a rename edit is open, pin every
  // row's sort key so background updated_at bumps can't reorder rows under
  // the cursor / around the edit input — a row sliding into place
  // mid-interaction receives the click / right-click and the rename it
  // triggers, hitting a session the user never aimed at; a reorder during an
  // edit can move (and blur) the input, committing a half-typed title. The
  // map accumulates keys lazily inside sortByUpdatedAtDesc (a render-time ref
  // write) and is cleared once neither hold is active, when the order snaps
  // back to reality.
  const frozenKeysRef = useRef<Map<string, number>>(new Map());
  const [pointerInside, setPointerInside] = useState(false);
  const [editingIds, setEditingIds] = useState<ReadonlySet<string>>(() => new Set());
  const reportRowEditing = useCallback((id: string, editing: boolean) => {
    setEditingIds((prev) => {
      if (prev.has(id) === editing) return prev;
      const next = new Set(prev);
      if (editing) next.add(id);
      else next.delete(id);
      return next;
    });
  }, []);
  const orderFrozen = pointerInside || editingIds.size > 0;
  const frozenKeys = orderFrozen ? frozenKeysRef.current : null;
  useEffect(() => {
    if (!orderFrozen) frozenKeysRef.current.clear();
  }, [orderFrozen]);

  // Build sections: Pinned and Archived are peeled off; the rest splits into
  // the viewer's own sessions (Chats) and ones shared with them. Archived
  // sessions render in their own group at the bottom (below "Shared with
  // me"); a pinned-then-archived session shows under Archived, not Pinned.
  const pinnedSet = useMemo(() => new Set(pinnedConversationIds), [pinnedConversationIds]);
  // Removing Favorites must reveal existing server pins in their home lists.
  const hidePinnedHomeCopies = layout.sections.some((section) => section.kind === "favorites");
  const loadedSections = useMemo(() => {
    // Merge the server pinned set in, so a pinned session outside the loaded
    // paginated window still renders. Dedupe by id: a pinned session is usually
    // also present in the paginated list, and merging both would render it twice.
    const allWithPinned = dedupeConversationsById([...allConversations, ...pinnedConversations]);
    const notArchived = allWithPinned.filter((c) => c.archived !== true);
    // The filter picks the slice; the Pinned / Projects / Sessions structure is
    // then built from it, so every filter reuses the same layout.
    const tabScoped =
      activeTab === "archived"
        ? allWithPinned.filter((c) => c.archived === true)
        : activeTab === "shared"
          ? notArchived.filter((c) => !isOwnedByViewer(c, viewerId))
          : activeTab === "mine"
            ? notArchived.filter((c) => isOwnedByViewer(c, viewerId))
            : notArchived;

    // With Favorites present, pinned sessions render there in order of the `omnigent.pinned` label
    // value (pin time, or a dragged position; newest pin at the bottom), NOT by
    // `updated_at`, so a pinned session holds its slot when a new message bumps
    // its `updated_at`. Pins are ownership-agnostic, so the favorites section
    // always shows every non-archived pin regardless of the
    // My/Shared/All/Archived filter — it's scoped to notArchived (not
    // tabScoped), so an owned pin stays visible on the Shared tab, a shared pin
    // stays visible on My sessions, and the pins don't vanish on the Archived
    // tab either. (An archived session is never pinned into the live sections —
    // hence notArchived, not allWithPinned.)
    const pinned = orderByPinnedTimestamp(notArchived.filter((c) => pinnedSet.has(c.id)));

    // The Projects section renders the same folders on every filter (scope to
    // notArchived, not tabScoped, so folders don't empty out on Shared or
    // Archived). Filing is owner-only, though — UNLIKE pins — so membership is
    // gated on ownership: a folder only ever holds the viewer's OWNED sessions.
    // Without the guard the legacy label arm would match a shared session by
    // project name alone, pulling a foreign session into the viewer's folder
    // (and out of the flat Shared list via filedIds). The folder's view filters
    // pinned rows while the membership remains on the server.
    const filedIds = new Set<string>();
    const projectGroups: {
      id: string | null;
      name: string;
      icon?: string | null;
      conversations: Conversation[];
    }[] = projects.map(({ id, name, icon }) => {
      // Dual-read membership: a session belongs to this folder if it has
      // the first-class id OR the legacy omni_project label of this name,
      // and (filing being owner-only) the viewer owns it.
      const inProject = notArchived.filter((c) =>
        sessionBelongsToProject(c, { id, name }, viewerId),
      );
      inProject.forEach((c) => filedIds.add(c.id));
      return {
        id,
        name,
        icon,
        conversations: sortByUpdatedAtDesc(inProject, activeOverride, frozenKeys),
      };
    });
    // NOTE: empty projects are intentionally NOT filtered out. A project comes
    // from the server project list (useProjects), so it can have zero *loaded*
    // conversations — either genuinely empty or because its chats live on an
    // unloaded page. We render it as a folder with a "No sessions" placeholder
    // rather than hiding it (matches the target sidebar layout).

    // Sessions: unfiled rows without a pinned copy.
    const sessions = sortByUpdatedAtDesc(
      tabScoped.filter(
        (c) => !filedIds.has(c.id) && (!hidePinnedHomeCopies || !pinnedSet.has(c.id)),
      ),
      activeOverride,
      frozenKeys,
    );
    return { pinned, sessions, projectGroups };
  }, [
    allConversations,
    pinnedConversations,
    pinnedSet,
    hidePinnedHomeCopies,
    activeOverride,
    frozenKeys,
    projects,
    activeTab,
    viewerId,
  ]);

  const config = useContext(SidebarConfigContext);
  const displayPagination = useSidebarDisplayPagination(
    loadedSections.sessions,
    JSON.stringify([activeTab, searchQuery]),
    activeTab === "shared"
      ? (config.sharedDisplayPageSize ?? config.displayPageSize)
      : config.displayPageSize,
    conversationsQuery.hasNextPage,
    conversationsQuery.fetchNextPage,
  );
  const sections = useMemo(
    () => ({ ...loadedSections, sessions: displayPagination.rows }),
    [loadedSections, displayPagination.rows],
  );

  // Project groups addressed by their first-class id, for the layout's
  // `projects` sections (which list project ids).
  const projectGroupsById = useMemo(() => {
    const map = new Map<string, (typeof sections.projectGroups)[number]>();
    for (const group of sections.projectGroups) {
      if (group.id !== null) map.set(group.id, group);
    }
    return map;
  }, [sections.projectGroups]);

  // Projects claimed by any `projects` section: the rest render under
  // "Projects". Label-only folders (id null) can never be claimed.
  const claimedProjectIds = useMemo(() => {
    const claimed = new Set<string>();
    for (const section of layout.sections) {
      if (section.kind !== "projects") continue;
      for (const projectId of section.projectIds ?? []) claimed.add(projectId);
    }
    return claimed;
  }, [layout.sections]);

  const favoriteProjectIds = useMemo(() => {
    const favorites = layout.sections.find((section) => section.kind === "favorites");
    return new Set(
      (favorites?.items ?? []).filter((ref) => ref.type === "project").map((ref) => ref.id),
    );
  }, [layout.sections]);

  const unclaimedProjectGroups = useMemo(
    () =>
      sections.projectGroups.filter(
        (group) =>
          group.id === null ||
          (!claimedProjectIds.has(group.id) && !favoriteProjectIds.has(group.id)),
      ),
    [sections.projectGroups, claimedProjectIds, favoriteProjectIds],
  );

  // Scope-active flags: which section owns the current selection UI (checkboxes
  // + bulk-action bar). Only one is ever true at a time.
  const sessionsSelecting = selectionMode && selectionScope === "sessions";
  const projectsSelecting = selectionMode && selectionScope === "projects";

  // Collapsed section ids — device-local (never synced), persisted so the
  // preference survives reloads.
  const [collapsedSectionIds, setCollapsedSectionIds] = useState<string[]>(
    readCollapsedSidebarSectionIds,
  );
  const toggleSectionCollapsed = useCallback((sectionId: string) => {
    setCollapsedSectionIds((prev) => {
      const next = prev.includes(sectionId)
        ? prev.filter((id) => id !== sectionId)
        : [...prev, sectionId];
      writeCollapsedSidebarSectionIds(next);
      return next;
    });
  }, []);

  // Auto-expand the favorites section when a session is newly pinned, so a
  // freshly-pinned chat can't hide inside a collapsed group. Only reacts to
  // pins being *added* — unpinning or reordering leaves the collapsed
  // preference alone.
  const favoritesSectionId = useMemo(
    () => layout.sections.find((section) => section.kind === "favorites")?.id ?? null,
    [layout.sections],
  );
  const prevPinnedIds = useRef(pinnedConversationIds);
  useEffect(() => {
    const prev = new Set(prevPinnedIds.current);
    const wasPinned = pinnedConversationIds.some((id) => !prev.has(id));
    prevPinnedIds.current = pinnedConversationIds;
    if (!wasPinned || favoritesSectionId === null) return;
    setCollapsedSectionIds((prevCollapsed) => {
      if (!prevCollapsed.includes(favoritesSectionId)) return prevCollapsed;
      const next = prevCollapsed.filter((id) => id !== favoritesSectionId);
      writeCollapsedSidebarSectionIds(next);
      return next;
    });
  }, [pinnedConversationIds, favoritesSectionId]);

  // When a search query appears, auto-expand all sections so results
  // in collapsed groups are visible. The user can still manually collapse
  // sections while searching. When the search is cleared, restore the
  // persisted collapsed state.
  const prevSearchQuery = useRef(searchQuery);
  const [searchCollapsedSections, setSearchCollapsedSections] = useState<string[]>([]);
  useEffect(() => {
    const wasEmpty = !prevSearchQuery.current;
    const isNonEmpty = !!searchQuery;
    prevSearchQuery.current = searchQuery;
    if (wasEmpty && isNonEmpty) {
      setSearchCollapsedSections([]);
    }
  }, [searchQuery]);
  const effectiveCollapsedSections = searchQuery ? searchCollapsedSections : collapsedSectionIds;
  const effectiveToggleSectionCollapsed = searchQuery
    ? (sectionId: string) => {
        setSearchCollapsedSections((prev) =>
          prev.includes(sectionId) ? prev.filter((id) => id !== sectionId) : [...prev, sectionId],
        );
      }
    : toggleSectionCollapsed;

  // Project folders default to COLLAPSED, so we track the inverse — names the
  // user has expanded — persisted across reloads. A project shows its rows only
  // while its name is in this set.
  const [expandedProjects, setExpandedProjects] = useState<string[]>(readExpandedProjectSections);
  const toggleProjectExpanded = useCallback((projectName: string) => {
    setExpandedProjects((prev) => {
      const next = prev.includes(projectName)
        ? prev.filter((n) => n !== projectName)
        : [...prev, projectName];
      writeExpandedProjectSections(next);
      return next;
    });
  }, []);
  // Expand a project (idempotent). Called right after a session is filed into
  // one, so the freshly populated folder — especially a brand-new project —
  // opens to reveal the session instead of appearing collapsed.
  const expandProject = useCallback((projectName: string) => {
    setExpandedProjects((prev) => {
      if (prev.includes(projectName)) return prev;
      const next = [...prev, projectName];
      writeExpandedProjectSections(next);
      return next;
    });
  }, []);

  // ── Drag-and-drop: file sessions into / out of projects ────────────────────
  // A session row can be dragged onto a project folder (file it there), onto the
  // "Chats" list / a fallback strip (unfile it), or onto "Pinned" (pin it, which
  // floats it out of its project). "Shared with me" is deliberately not a drop
  // target — you can't file sessions there. The kebab "Move session" menu + the
  // pin button remain the keyboard-accessible session actions.
  const moveToProject = useMoveToProject();
  // The session currently being dragged (id + source project + pinned state), or
  // null. Set on drag start, cleared on end/cancel; drives the DragOverlay
  // preview and which drop zones light up (ungroup only for a filed session, pin
  // only for an unpinned one).
  const [activeDrag, setActiveDrag] = useState<{
    id: string;
    label: string;
    project: string | null;
    isPinned: boolean;
    /** Favorites copy drag: only pin reorder is a valid outcome. */
    reorderOnly: boolean;
  } | null>(null);
  // Mouse: a small drag threshold so a plain click still navigates / opens the
  // kebab. Touch: a press-and-hold delay so scrolling the list isn't hijacked
  // into a drag. Project headers also support keyboard sorting.
  const sensors = useSensors(
    useSensor(KeyboardSensor, {
      coordinateGetter: (event, args) => {
        const type = args.context.active?.data.current?.type;
        if (type !== "project-order" && type !== "section-order") {
          return sortableKeyboardCoordinates(event, args);
        }
        const containers = args.context.droppableContainers;
        const sortableContainers = {
          getEnabled: () =>
            containers.getEnabled().filter((container) => container.data.current?.type === type),
          get: containers.get.bind(containers),
        } as typeof containers;
        const collisionRect = args.context.collisionRect;
        const activeRect = args.context.active
          ? args.context.droppableRects.get(args.context.active.id)
          : undefined;
        const keyboardCollisionRect =
          collisionRect?.width === 0 && collisionRect.height === 0
            ? (activeRect ?? collisionRect)
            : collisionRect;
        return sortableKeyboardCoordinates(event, {
          ...args,
          context: {
            ...args.context,
            collisionRect: keyboardCollisionRect,
            droppableContainers: sortableContainers,
          },
        });
      },
      keyboardCodes: { start: ["Space"], cancel: ["Escape"], end: ["Space"] },
    }),
    useSensor(MouseSensor, { activationConstraint: { distance: 5 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 250, tolerance: 8 } }),
  );
  const handleDragStart = useCallback((event: DragStartEvent) => {
    const type = event.active.data.current?.type;
    // New drop zones can shift the source row; preserve its position before rendering them.
    const target = event.activatorEvent.target;
    const sourceSelector =
      type === "project-order" || type === "fav-item"
        ? "[data-project-order-name]"
        : type === "section-order"
          ? "[data-section-id]"
          : "[data-sidebar-session-id]";
    const rect =
      ("clientX" in event.activatorEvent || "touches" in event.activatorEvent) &&
      target instanceof Element
        ? target.closest(sourceSelector)?.getBoundingClientRect()
        : undefined;
    dragOrigin.current = rect ? { left: rect.left, top: rect.top, width: rect.width } : undefined;
    if (type === "project-order") {
      setDraggedProject(event.active.data.current?.name as string);
      return;
    }
    if (type === "section-order") {
      setDraggedSection(event.active.data.current?.id as string);
      return;
    }
    if (type === "fav-item") {
      setDraggedFavorite({
        refId: event.active.data.current?.refId as string,
        label: (event.active.data.current?.label as string) ?? "",
      });
      return;
    }
    const data = event.active.data.current as
      | {
          id?: string;
          label?: string;
          project?: string | null;
          isPinned?: boolean;
          reorderOnly?: boolean;
        }
      | undefined;
    // A non-canonical copy's dnd id is its instanceKey, so read the session id
    // off the draggable data instead.
    const sessionId = data?.id ?? String(event.active.id);
    setActiveDrag({
      id: sessionId,
      label: data?.label ?? sessionId,
      project: data?.project ?? null,
      isPinned: data?.isPinned ?? false,
      reorderOnly: data?.reorderOnly ?? false,
    });
  }, []);
  // Move a project within one `projects` section's own `projectIds` order. The
  // destination is resolved against the freshly read layout so a concurrent
  // edit isn't clobbered.
  const moveProjectInSection = useCallback(
    (
      sectionId: string,
      projectId: string,
      resolveTarget: (ids: string[], from: number) => number,
    ) => {
      saveLayout((current) => {
        const section = current.sections.find((candidate) => candidate.id === sectionId);
        if (section === undefined || section.kind !== "projects") return current;
        const ids = section.projectIds ?? [];
        const from = ids.indexOf(projectId);
        if (from < 0) return current;
        const knownProjectIds = new Set(
          projects.flatMap((project) => (project.id === null ? [] : [project.id])),
        );
        const resolvedPositions = ids
          .map((id, index) => (knownProjectIds.has(id) ? index : -1))
          .filter((index) => index >= 0);
        const fromPosition = resolvedPositions.indexOf(from);
        const targetIndex = Math.max(0, Math.min(resolveTarget(ids, from), ids.length - 1));
        const targetPosition = resolvedPositions.findIndex((index) => index >= targetIndex);
        const toPosition = targetPosition < 0 ? resolvedPositions.length - 1 : targetPosition;
        if (fromPosition < 0 || fromPosition === toPosition) return current;
        const reorderedIds = arrayMove(
          resolvedPositions.map((index) => ids[index]!),
          fromPosition,
          toPosition,
        );
        const projectIds = [...ids];
        resolvedPositions.forEach((index, position) => {
          const id = reorderedIds[position];
          if (id !== undefined) projectIds[index] = id;
        });
        return {
          ...current,
          sections: current.sections.map((candidate) =>
            candidate.id === sectionId ? { ...candidate, projectIds } : candidate,
          ),
        };
      });
    },
    [projects, saveLayout],
  );

  // Reorder the favorites section's items when a row (project copy or session)
  // is dropped onto another favorites row: the new slot pattern goes to the
  // layout, and — when the session subsequence changed — the pin timestamps are
  // rewritten. Refs that no longer resolve keep their place.
  const applyFavoriteReorder = useCallback(
    (activeRef: FavoriteRef, overRef: FavoriteRef) => {
      const pinnedIds = sections.pinned.map((c) => c.id);
      const pinnedIdSet = new Set(pinnedIds);
      const knownProjectIds = new Set(projectGroupsById.keys());
      const isResolved = (ref: FavoriteRef) =>
        ref.type === "project" ? knownProjectIds.has(ref.id) : pinnedIdSet.has(ref.id);
      const pendingWrites: PinOrderWrite[] = [];
      saveLayout((current) => {
        const section = current.sections.find((candidate) => candidate.kind === "favorites");
        if (section === undefined) return current;
        const order = favoritesRows(section, pinnedIds, knownProjectIds);
        const key = (ref: FavoriteRef) => `${ref.type}:${ref.id}`;
        const from = order.findIndex((ref) => key(ref) === key(activeRef));
        const to = order.findIndex((ref) => key(ref) === key(overRef));
        if (from < 0 || to < 0 || from === to) return current;
        const moved = arrayMove(order, from, to);
        const { items: movedItems, sessionOrder } = splitFavoritesReorder(moved);
        if (activeRef.type === "session") {
          const before = splitFavoritesReorder(order).sessionOrder;
          if (before.join("\u0000") !== sessionOrder.join("\u0000")) {
            const target = favoriteReorderAnchor(movedItems, activeRef.id, to > from);
            if (target !== null) {
              pendingWrites.push(...pinOrderWrites(sections.pinned, activeRef.id, target));
            }
          }
        }
        // The implicit default favorites section is never persisted by a
        // pin-only reorder: its sessions render in pin order, so rewriting the
        // timestamps is the whole edit and no layout write is needed.
        if (section.implicit === true) return current;
        const items = reorderFavoriteItems(section.items ?? [], movedItems, isResolved);
        return {
          ...current,
          sections: current.sections.map((candidate) =>
            candidate.id === section.id ? { ...candidate, items } : candidate,
          ),
        };
      });
      // The updater runs synchronously against the stored layout; the mutation
      // stays outside it so the updater stays a pure read-modify-write.
      if (pendingWrites.length > 0) reorderPins(pendingWrites);
    },
    [sections.pinned, projectGroupsById, saveLayout, reorderPins],
  );

  const handleDragEnd = useCallback(
    (event: DragEndEvent) => {
      const activeType = event.active.data.current?.type;
      if (activeType === "fav-item") {
        setDraggedFavorite(null);
        const over = event.over?.data.current;
        if (over?.type === "fav-item") {
          applyFavoriteReorder(
            { type: "project", id: event.active.data.current?.refId as string },
            { type: over.refType as "session" | "project", id: over.refId as string },
          );
        }
        return;
      }
      if (activeType === "section-order") {
        setDraggedSection(null);
        setOverSection(null);
        if (event.over?.data.current?.type !== "section-order") return;
        const id = event.active.data.current?.id as string;
        const overId = event.over.data.current?.id as string;
        const targetIndex = layout.sections.findIndex((section) => section.id === overId);
        if (targetIndex < 0) return;
        saveLayout((current) => moveSection(current, id, targetIndex));
        return;
      }
      if (activeType === "project-order") {
        setDraggedProject(null);
        setOverProject(null);
        const over = event.over?.data.current;
        const sourceName = event.active.data.current?.name as string;
        const sourceProject = projects.find((project) => project.name === sourceName);
        if (sourceProject === undefined) return;
        // Dropped on a section wrapper (its header or body): file it there.
        if (over?.type === "section") {
          const sectionId = over.kind === "other_projects" ? null : (over.id as string);
          const currentSectionId =
            sourceProject.id === null ? null : sectionOfProject(layout, sourceProject.id);
          if (currentSectionId === sectionId) return;
          void moveProjectToSectionWithPromotion({
            projectId: sourceProject.id,
            projectName: sourceName,
            sectionId,
            saveLayout,
            queryClient,
          });
          return;
        }
        // Dropped on a favorites section: add the project as a favorite ref
        // (promoting a label-only folder first).
        if (over?.type === "favorites") {
          void addProjectToFavoritesWithPromotion({
            projectId: sourceProject.id,
            projectName: sourceName,
            saveLayout,
            queryClient,
          });
          return;
        }
        if (over?.type !== "project-order" || saveOrder.isPending) return;
        const targetName = over.name as string;
        if (targetName === sourceName) return;
        const targetProject = projects.find((project) => project.name === targetName);
        if (targetProject === undefined) return;
        const sourceSectionId =
          sourceProject.id === null ? null : sectionOfProject(layout, sourceProject.id);
        const targetSectionId =
          targetProject.id === null ? null : sectionOfProject(layout, targetProject.id);
        // Dropped on a folder of a different section: move into that section.
        if (sourceSectionId !== targetSectionId) {
          void moveProjectToSectionWithPromotion({
            projectId: sourceProject.id,
            projectName: sourceName,
            sectionId: targetSectionId,
            saveLayout,
            queryClient,
          });
          return;
        }
        if (sourceSectionId === null) {
          // Today's behaviour: reorder unclaimed projects in the global order.
          if (!projectOrder.data) return;
          const from = projects.findIndex((project) => project.name === sourceName);
          const to = projects.findIndex((project) => project.name === targetName);
          if (from >= 0 && to >= 0 && from !== to) {
            saveOrder.mutate(arrayMove(projects, from, to));
          }
          return;
        }
        if (sourceProject.id === null || targetProject.id === null) return;
        // Alphabetical mode renders A–Z, so a same-section reorder is a no-op.
        if (projectOrder.data?.sort_mode !== "manual") return;
        const ids =
          layout.sections.find((section) => section.id === sourceSectionId)?.projectIds ?? [];
        const to = ids.indexOf(targetProject.id);
        if (to < 0) return;
        moveProjectInSection(sourceSectionId, sourceProject.id, () => to);
        return;
      }
      const dragged = activeDrag;
      setActiveDrag(null);
      setOverPin(null);
      if (!dragged) return;
      const target = (event.over?.data.current as SidebarDropTarget | undefined) ?? null;
      const action = resolveSidebarDrop(
        {
          id: dragged.id,
          project: dragged.project,
          isPinned: dragged.isPinned,
          favoritesCopy: dragged.reorderOnly,
        },
        target,
      );
      // A favorites copy drags only to reorder pins or to leave favorites; its
      // folder / Sessions twin owns move / ungroup.
      if (dragged.reorderOnly && action.kind !== "reorder-pin" && action.kind !== "unpin") return;
      if (action.kind === "move") {
        moveToProject.mutate({ id: dragged.id, project: action.project });
        // Open the (possibly brand-new) folder so the session is visible in it.
        expandProject(action.project);
        return;
      }
      if (action.kind === "reorder-pin") {
        if (target?.type === "fav-item") {
          // A mixed reorder across any favorites row (session or project):
          // the helper writes the slot pattern and any needed pin timestamps.
          applyFavoriteReorder(
            { type: "session", id: dragged.id },
            { type: target.refType, id: target.refId },
          );
        } else {
          const writes = pinOrderWrites(sections.pinned, dragged.id, action.targetId);
          if (writes.length > 0) reorderPins(writes);
        }
        return;
      }
      if (action.kind === "pin" && action.targetId) {
        // Pin into the dropped-on slot; the new pin goes through the pin toggle
        // (cap / ownership checks), and only once it's accepted are any
        // renumbered neighbours rewritten through the batch.
        const writes = pinOrderWrites(sections.pinned, dragged.id, action.targetId);
        const pin = writes.find((w) => w.id === dragged.id);
        const rest = writes.filter((w) => w.id !== dragged.id);
        // Chain on this call's own promise: per-call mutate callbacks are dropped
        // when a later drop calls the mutation again.
        if (pin) {
          pinAt({ id: pin.id, pinned: true, pinnedAt: pin.pinnedAt })
            .then(() => {
              saveLayout(ensureFavoritesSection);
              if (rest.length > 0) reorderPins(rest);
            })
            .catch(() => {});
        }
        return;
      }
      if (action.kind === "pin") {
        // Dropped on the favorites section: pin through the toggle; a favorites
        // section is created only when the layout has none.
        pinAt({ id: dragged.id, pinned: true })
          .then(() => saveLayout(ensureFavoritesSection))
          .catch(() => {});
        return;
      }
      if (action.kind === "unpin") {
        // A favorites copy dropped on the ungroup / Sessions zone leaves
        // favorites: unpin and drop its ref.
        unpinFavorite(dragged.id);
        return;
      }
      if (action.kind === "ungroup") {
        // Unfile silently — a first-class project persists when emptied, so
        // dragging out its last session deletes nothing. Mirrors the kebab flow.
        moveToProject.mutate({ id: dragged.id, project: "" });
      }
    },
    [
      activeDrag,
      moveToProject,
      expandProject,
      projects,
      saveOrder,
      sections.pinned,
      pinAt,
      reorderPins,
      layout,
      saveLayout,
      queryClient,
      moveProjectInSection,
      applyFavoriteReorder,
      unpinFavorite,
      projectOrder.data,
    ],
  );

  const expandAllProjects = useCallback((allNames: string[]) => {
    setExpandedProjects(() => {
      writeExpandedProjectSections(allNames);
      return allNames;
    });
  }, []);
  const collapseAllProjects = useCallback(() => {
    setExpandedProjects(() => {
      writeExpandedProjectSections([]);
      return [];
    });
  }, []);

  // Sessions each expanded ProjectFolder has actually rendered, keyed by project
  // name and the copy's instance key. A folder paginates independently of the
  // global window, so its rows can include members the global list hasn't
  // loaded; projects-scope selection and the projection must resolve against
  // these. A favorite copy reports under its own key, so neither copy's rows
  // overwrite the other's. Folders report via `onConversationsLoaded`; collapsed
  // folders report `[]`.
  const [folderConversations, setFolderConversations] = useState<Map<string, Conversation[]>>(
    () => new Map(),
  );
  const handleFolderConversationsLoaded = useCallback(
    (name: string, conversations: Conversation[], instanceKey?: string) => {
      const key = instanceKey === undefined ? name : `${name}#${instanceKey}`;
      setFolderConversations((prev) => {
        const existing = prev.get(key);
        // Row objects, not ids: Poll reads pending cards and updated_at from
        // this map, and a changed row arrives as a fresh object while
        // react-query keeps unchanged rows referentially stable.
        if (
          existing &&
          existing.length === conversations.length &&
          existing.every((c, i) => c === conversations[i])
        ) {
          return prev;
        }
        const next = new Map(prev);
        next.set(key, conversations);
        return next;
      });
    },
    [],
  );

  // The recent section's count drives its query; enabled whenever the section
  // exists — a collapsed section still needs its members for the header marker.
  const recentSection = useMemo(
    () => layout.sections.find((section) => section.kind === "recent") ?? null,
    [layout.sections],
  );
  const recentQuery = useRecentSessions(recentSection?.count ?? 5, recentSection !== null);
  const recentUnavailable = recentQuery.error instanceof RecentSessionsUnavailableError;
  // Replace each recent row with the freshest copy the sidebar already holds
  // (pinned / folder / flat), so live status updates apply; a session the
  // sidebar doesn't hold renders from the query row.
  const recentRows = useMemo(() => {
    const rows = recentQuery.data ?? [];
    if (rows.length === 0) return rows;
    const byId = new Map<string, Conversation>();
    for (const c of sections.pinned) byId.set(c.id, c);
    for (const c of loadedSections.sessions) byId.set(c.id, c);
    for (const group of sections.projectGroups) {
      for (const c of group.conversations) byId.set(c.id, c);
    }
    for (const list of folderConversations.values()) {
      for (const c of list) byId.set(c.id, c);
    }
    return rows
      .filter((row) => !hidePinnedHomeCopies || !pinnedSet.has(row.id))
      .map((row) => byId.get(row.id) ?? row);
  }, [
    recentQuery.data,
    sections.pinned,
    sections.projectGroups,
    loadedSections.sessions,
    folderConversations,
    pinnedSet,
    hidePinnedHomeCopies,
  ]);
  const projectLabelFor = useCallback(
    (conversation: Conversation): string | undefined => {
      const firstClass =
        conversation.project_id != null ? projectNamesById.get(conversation.project_id) : undefined;
      return firstClass ?? conversation.labels?.[PROJECT_LABEL_KEY] ?? undefined;
    },
    [projectNamesById],
  );

  // The one ordered projection of the layout: each section resolved once, in
  // layout order, into the groups it renders (a `projects` section's ids in
  // alphabetical-or-manual order, `other_projects`' unclaimed folders), its
  // flat sessions, and each folder's rendered rows. Every consumer below walks
  // this so they cannot disagree about section order.
  const projection = useMemo<ResolvedSection[]>(() => {
    const alphabetical = projectOrder.data?.sort_mode !== "manual";
    // A collapsed section unmounts its folders, so their last-reported rows go
    // stale in `folderConversations` and status changes never reach the
    // section's marker. Refresh each snapshot row from the globally loaded
    // window and drop rows the window has since refiled elsewhere; rows the
    // window never loaded keep their snapshot copy, and the snapshot's order
    // keeps a mounted folder identical to what it reported.
    const windowIds = new Set<string>();
    for (const c of allConversations) windowIds.add(c.id);
    for (const c of pinnedConversations) windowIds.add(c.id);
    const withRows = (group: SidebarProjectGroup, instanceKey?: string): ResolvedProjectGroup => {
      const key = instanceKey === undefined ? group.name : `${group.name}#${instanceKey}`;
      const snapshot = folderConversations.get(key);
      if (snapshot === undefined)
        return {
          group,
          rows: group.conversations.filter((c) => !hidePinnedHomeCopies || !pinnedSet.has(c.id)),
        };
      const current = new Map(group.conversations.map((c) => [c.id, c] as const));
      const rows: Conversation[] = [];
      const seen = new Set<string>();
      for (const row of snapshot) {
        seen.add(row.id);
        const fresh = current.get(row.id);
        if (fresh !== undefined) rows.push(fresh);
        else if (!windowIds.has(row.id)) rows.push(row);
      }
      for (const row of group.conversations) {
        if (!seen.has(row.id)) rows.push(row);
      }
      return { group, rows: rows.filter((c) => !hidePinnedHomeCopies || !pinnedSet.has(c.id)) };
    };
    return layout.sections.map((section) => {
      const collapsed = effectiveCollapsedSections.includes(section.id);
      let groups: ResolvedProjectGroup[] = [];
      let flatSessions: Conversation[] = [];
      let favoriteOrder: FavoriteRef[] | undefined;
      switch (section.kind) {
        case "favorites": {
          // Walk the slot rule: project refs render in place as folder copies,
          // session refs as rows filled by pin order.
          const byId = new Map(sections.pinned.map((c) => [c.id, c] as const));
          const knownProjectIds = new Set(projectGroupsById.keys());
          favoriteOrder = favoritesRows(
            section,
            sections.pinned.map((c) => c.id),
            knownProjectIds,
          );
          const prefix = `fav:${section.id}`;
          for (const ref of favoriteOrder) {
            if (ref.type === "session") {
              const conversation = byId.get(ref.id);
              if (conversation !== undefined) flatSessions.push(conversation);
            } else {
              const group = projectGroupsById.get(ref.id);
              if (group !== undefined)
                groups.push({ ...withRows(group, prefix), instanceKeyPrefix: prefix });
            }
          }
          break;
        }
        case "projects": {
          const resolved = (section.projectIds ?? [])
            .filter((projectId) => !favoriteProjectIds.has(projectId))
            .map((projectId) => projectGroupsById.get(projectId))
            .filter((group): group is SidebarProjectGroup => group !== undefined);
          if (alphabetical) resolved.sort((a, b) => a.name.localeCompare(b.name));
          groups = resolved.map((group) => withRows(group));
          break;
        }
        case "other_projects":
          groups = unclaimedProjectGroups.map((group) => withRows(group));
          break;
        case "other_sessions":
          flatSessions = sections.sessions;
          break;
        case "recent":
          flatSessions = recentRows;
          break;
      }
      return { section, collapsed, groups, flatSessions, favoriteOrder };
    });
  }, [
    layout.sections,
    effectiveCollapsedSections,
    sections.pinned,
    sections.sessions,
    projectGroupsById,
    unclaimedProjectGroups,
    folderConversations,
    projectOrder.data?.sort_mode,
    recentRows,
    allConversations,
    pinnedConversations,
    pinnedSet,
    hidePinnedHomeCopies,
    favoriteProjectIds,
  ]);

  // The visible folder copy wins; otherwise the first visible copy in layout
  // order is canonical.
  const canonicalInstanceKeys = useMemo(() => {
    const firstRendered = new Map<string, string>();
    const folderRendered = new Map<string, string>();
    const record = (id: string, key: string, folder: boolean) => {
      if (!firstRendered.has(id)) firstRendered.set(id, key);
      if (folder && !folderRendered.has(id)) folderRendered.set(id, key);
    };
    for (const { section, collapsed, groups, flatSessions } of projection) {
      if (collapsed) continue;
      const sectionFolder = section.kind === "projects" || section.kind === "other_projects";
      for (const { group, rows, instanceKeyPrefix } of groups) {
        // A favorites project copy expands under its own `fav:<section>:<name>`
        // key, independent of the section copy.
        const expandedKey =
          instanceKeyPrefix !== undefined ? `${instanceKeyPrefix}:${group.name}` : group.name;
        if (!expandedProjects.includes(expandedKey)) continue;
        // Only the owning section folder outranks other copies; a favorite
        // folder copy is just the first copy in layout order.
        const folder = sectionFolder;
        for (const conversation of rows) {
          const key =
            instanceKeyPrefix !== undefined
              ? `${instanceKeyPrefix}:${conversation.id}`
              : sectionFolder
                ? conversation.id
                : `${section.id}:${conversation.id}`;
          record(conversation.id, key, folder);
        }
      }
      if (!sectionFolder) {
        for (const conversation of flatSessions) {
          record(conversation.id, `${section.id}:${conversation.id}`, false);
        }
      }
    }
    const canonical = new Map<string, string>();
    for (const [id, key] of firstRendered) canonical.set(id, folderRendered.get(id) ?? key);
    return canonical;
  }, [projection, expandedProjects]);

  // The deduped union of every session any section renders. Hooks can't run
  // per section in a loop, so the section markers below read errors from one
  // shared fetch over this set.
  const sectionMembers = useMemo(() => {
    const members: Conversation[] = [];
    for (const { groups, flatSessions } of projection) {
      for (const { rows } of groups) members.push(...rows);
      members.push(...flatSessions);
    }
    return dedupeConversationsById(members);
  }, [projection]);

  const memberErrors = useSessionErrorStates(sectionMembers);
  const errorsById = useMemo(() => {
    const map = new Map<string, LatestSessionError | null>();
    sectionMembers.forEach((conversation, index) => {
      map.set(conversation.id, memberErrors[index] ?? null);
    });
    return map;
  }, [sectionMembers, memberErrors]);

  const startingConversationId = useChatStore((s) =>
    s.status === "streaming" || s.terminalPending ? s.conversationId : null,
  );
  const { showGoalSessionMarkers } = useSessionNavigationPreferences();

  // A collapsed section's header rolls up the marker a project folder shows,
  // over every session the section would render (all its folders' rows).
  const sectionMarkers = useMemo(() => {
    const markers = new Map<string, ProjectMarkerState>();
    for (const { section, groups, flatSessions } of projection) {
      const members: Conversation[] = [];
      for (const { rows } of groups) members.push(...rows);
      members.push(...flatSessions);
      markers.set(
        section.id,
        projectMarkerState(
          members,
          members.map((conversation) => errorsById.get(conversation.id) ?? null),
          startingConversationId,
          showGoalSessionMarkers,
        ),
      );
    }
    return markers;
  }, [projection, errorsById, startingConversationId, showGoalSessionMarkers]);

  // Whether any section will actually paint a header. An implicit favorites
  // with no pins paints nothing; if every section is one of those (or the
  // layout is empty), the user needs a fallback entry to create a section.
  const rendersSectionHeader = useMemo(
    () =>
      projection.some(
        ({ section, flatSessions }) =>
          !(section.kind === "favorites" && section.implicit === true && flatSessions.length === 0),
      ),
    [projection],
  );

  // The projects-scope selection pool: the folders' own rendered rows (the
  // authoritative, possibly-out-of-window set) unioned with the global-derived
  // membership as a fallback for folders that haven't reported yet. Deduped by
  // id. This backs the bulk-action bar, the shift-select range, and the
  // stranding guard so all three agree on what's selectable.
  const projectSessionPool = useMemo(() => {
    const byId = new Map<string, Conversation>();
    for (const group of sections.projectGroups) {
      for (const c of group.conversations) byId.set(c.id, c);
    }
    for (const rows of folderConversations.values()) {
      for (const c of rows) byId.set(c.id, c);
    }
    return [...byId.values()].filter((c) => !hidePinnedHomeCopies || !pinnedSet.has(c.id));
  }, [sections.projectGroups, folderConversations, pinnedSet, hidePinnedHomeCopies]);

  // The bulk-action bar lives under the header of the section it targets, so it
  // unmounts when that section empties (e.g. every selected session
  // archived/deleted). Exit selection mode in that case so the user isn't
  // stranded without its controls. Suppressed while the list is refetching: a
  // background refetch can briefly yield an empty page, and exiting on that
  // transient would kick the user out of selection mode mid-task. The projects
  // pool unions global-derived membership with the folder queries, so a single
  // folder's transient-empty refetch can't zero it while any member is still in
  // the global window (only a genuinely empty pool exits).
  useEffect(() => {
    if (!selectionMode || conversationsQuery.isFetching) return;
    const pool =
      selectionScope === "projects" ? projectSessionPool.length : sections.sessions.length;
    if (pool === 0) onExitSelectionMode();
  }, [
    selectionMode,
    selectionScope,
    sections.sessions.length,
    projectSessionPool.length,
    conversationsQuery.isFetching,
    onExitSelectionMode,
  ]);

  // The project the currently-selected session is filed under, if any. Derived
  // as a primitive so the auto-expand effect below only fires when the
  // selection (or its project) changes — not on every background list refetch,
  // which would re-open a folder the user just collapsed.
  const activeProjectName = useMemo(() => {
    if (!activeId) return null;
    const active = allConversations.find((c) => c.id === activeId);
    return active?.labels?.[PROJECT_LABEL_KEY] ?? null;
  }, [activeId, allConversations]);
  // Auto-expand the project folder holding the selected session, so navigating
  // to a filed session reveals it instead of leaving it hidden in a collapsed
  // folder. Skipped for pinned sessions: they're already reachable from the
  // Pinned section, so forcing their project open would undo a manual collapse
  // every time the user clicks the pinned row.
  useEffect(() => {
    if (!activeId || !activeProjectName) return;
    if (pinnedSet.has(activeId)) return;
    expandProject(activeProjectName);
  }, [activeId, activeProjectName, pinnedSet, expandProject]);

  // Visible rows in render order (collapsed sections excluded) for the Cmd+↑/↓
  // session hotkey. Sections render in layout order.
  const orderedConversationIds = useMemo(() => {
    const ids: string[] = [];
    const seen = new Set<string>();
    const push = (list: readonly Conversation[]) => {
      for (const conversation of list) {
        if (seen.has(conversation.id)) continue;
        seen.add(conversation.id);
        ids.push(conversation.id);
      }
    };
    // A project's chats are navigable only when the section that contains the
    // folder is expanded AND that individual folder is expanded (folders are
    // collapsed unless explicitly opened — inverse of the fixed sections).
    for (const { section, collapsed, groups, flatSessions, favoriteOrder } of projection) {
      if (collapsed) continue;
      if (section.kind === "projects" || section.kind === "other_projects") {
        for (const { group, rows } of groups) {
          if (expandedProjects.includes(group.name)) push(rows);
        }
      } else if (section.kind === "favorites" && favoriteOrder !== undefined) {
        const groupById = new Map(groups.map((entry) => [entry.group.id, entry] as const));
        const sessionById = new Map(flatSessions.map((c) => [c.id, c] as const));
        for (const ref of favoriteOrder) {
          if (ref.type === "session") {
            const conversation = sessionById.get(ref.id);
            if (conversation !== undefined) push([conversation]);
          } else {
            const entry = groupById.get(ref.id);
            if (
              entry !== undefined &&
              expandedProjects.includes(`fav:${section.id}:${entry.group.name}`)
            ) {
              push(entry.rows);
            }
          }
        }
      } else {
        push(flatSessions);
      }
    }
    return ids;
  }, [projection, expandedProjects]);

  // Poll's candidate pool in sidebar order: what the rendered sections hold,
  // with each project folder's own paginated rows in place of the global
  // window's subset. A row the sidebar doesn't hold (another page, another
  // filter) is not a target, and polling never fetches a page to find one.
  const pollingPopulation = useMemo(() => {
    const rows = projection.flatMap(({ section, groups, flatSessions }) => [
      ...groups.flatMap((group) => group.rows),
      // The flat list keeps every loaded row (not just the display page) as a
      // candidate; hiddenPollingIds below hides the ones past the page.
      ...(section.kind === "other_sessions" ? loadedSections.sessions : flatSessions),
    ]);
    return dedupeConversationsById(rows).filter((conversation) => conversation.archived !== true);
  }, [projection, loadedSections.sessions]);

  // Rows the rendered sidebar hides: a collapsed section, an unexpanded
  // project folder (or one whose section is collapsed), and flat rows past the
  // display page. Poll's plain cycle skips these; needs-response and unread
  // jumps still reach them.
  const hiddenPollingIds = useMemo(() => {
    const visibleIds = new Set<string>();
    for (const { collapsed, groups, flatSessions } of projection) {
      if (collapsed) continue;
      for (const { group, rows, instanceKeyPrefix } of groups) {
        // A favorite copy expands under its own `fav:<section>:<name>` key.
        const expandedKey =
          instanceKeyPrefix !== undefined ? `${instanceKeyPrefix}:${group.name}` : group.name;
        if (!expandedProjects.includes(expandedKey)) continue;
        for (const conversation of rows) visibleIds.add(conversation.id);
      }
      for (const conversation of flatSessions) visibleIds.add(conversation.id);
    }
    const hidden = new Set<string>();
    for (const conversation of pollingPopulation) {
      if (!visibleIds.has(conversation.id)) hidden.add(conversation.id);
    }
    return hidden;
  }, [pollingPopulation, projection, expandedProjects]);

  const pollingArchive = useArchiveConversation();
  useSessionPollingHotkeys({
    activeId,
    getConversations: async () => pollingPopulation,
    isCollapsed: (conversation) => hiddenPollingIds.has(conversation.id),
    onArchive: async (conversation) => {
      await pollingArchive.mutateAsync({ id: conversation.id, archived: true });
      showArchiveUndoToast(queryClient, [conversation], navigate);
    },
    canArchive: (conversation) => isOwnedByViewer(conversation, viewerId),
  });
  // Getter for the shift-select range, built on demand (at click time). Scopes
  // to whichever section is selectable: the flat Sessions list, or the sessions
  // across expanded project folders (in render order). For projects scope the
  // range uses each folder's own reported rows — the same source the folder
  // renders — so a shift target the global window hasn't loaded still resolves.
  // Rows outside the active scope have no checkboxes, so they never enter a range.
  getVisibleIdsRef.current = () => {
    if (selectionScope === "projects") {
      const ids: string[] = [];
      const seen = new Set<string>();
      for (const { collapsed, groups } of projection) {
        if (collapsed) continue;
        for (const { group, rows } of groups) {
          if (!expandedProjects.includes(group.name)) continue;
          for (const conversation of rows) {
            if (seen.has(conversation.id)) continue;
            seen.add(conversation.id);
            ids.push(conversation.id);
          }
        }
      }
      return ids;
    }
    const sessionsSection = projection.find(({ section }) => section.kind === "other_sessions");
    if (sessionsSection === undefined || sessionsSection.collapsed) {
      return [];
    }
    return [...new Set(sessionsSection.flatSessions.map((c) => c.id))];
  };
  useSessionSwitchHotkey(orderedConversationIds, activeId);

  // Cmd/Ctrl+1..9/0 jumps to the first ten pinned sessions (desktop only;
  // see the hook). Empty when the favorites section is collapsed.
  const pinnedSessionIds = useMemo(
    () =>
      favoritesSectionId !== null && collapsedSectionIds.includes(favoritesSectionId)
        ? []
        : sections.pinned.map((c) => c.id),
    [sections.pinned, collapsedSectionIds, favoritesSectionId],
  );
  usePinnedSessionHotkeys(pinnedSessionIds, activeId);
  const pinOrder = useMemo(
    () =>
      pinReorderEnabled && !pinWriting
        ? {
            ids: sections.pinned.map((c) => c.id),
            draggingId: activeDrag?.id ?? null,
            overId: overPin,
          }
        : null,
    [pinReorderEnabled, pinWriting, sections.pinned, activeDrag, overPin],
  );

  // Pinned membership is server-authoritative (the `omnigent.pinned` label),
  // so there's no client-side list to normalize against the loaded window —
  // the pinned query returns exactly the pinned sessions, unpinning removes the
  // label, and a deleted session drops out of the query on the server.
  const autoLoadBudget = useRef<AutoLoadBudget>({ scope: activeTab, count: 0 });
  const hasMorePages = displayPagination.hasMore;
  const fetchNextPage = displayPagination.loadMore;
  const { isFetchingNextPage } = conversationsQuery;

  const sessionStatus = conversationsQuery.isError ? (
    <p role="status" className="px-2 py-1 text-destructive text-ui">
      {allConversations.length === 0
        ? `Failed to load: ${conversationsQuery.error instanceof Error ? conversationsQuery.error.message : String(conversationsQuery.error)}`
        : "Some sessions could not be loaded."}{" "}
      <button type="button" onClick={() => void conversationsQuery.refetch?.()}>
        Retry
      </button>
    </p>
  ) : conversationsQuery.isLoading ? (
    <p role="status" className="px-2 py-1 text-muted-foreground text-sm">
      Loading…
    </p>
  ) : undefined;
  const showShared = activeTab === "shared";
  const emptyMessage = searchQuery ? "No matching conversations" : "No sessions";

  // Archived sessions are surfaced on the Settings page, not here, so they
  // don't count toward the sidebar's empty-state threshold. Each project
  // counts itself (not just its loaded chats) so an empty project still
  // renders its "Projects" header + "No sessions" folder rather than the global
  // empty-state message. `sections` is tab-scoped, so this counts the active
  // tab only (Projects is empty on the Shared tab).
  const totalVisible =
    sections.pinned.length +
    sections.sessions.length +
    sections.projectGroups.length +
    sections.projectGroups.reduce((sum, g) => sum + g.conversations.length, 0);

  // The shared ProjectFolder props; only the ordering source and scroll root
  // differ between the Projects group and a user `projects` section.
  const renderProjectFolder = (
    group: (typeof sections.projectGroups)[number],
    ordering: ProjectFolderOrdering | undefined,
    scrollRoot: RefObject<HTMLElement | null>,
  ) => (
    <ProjectFolder
      key={group.name}
      ordering={ordering}
      name={group.name}
      projectId={group.id}
      icon={group.icon}
      windowConversations={group.conversations}
      activeConversationId={displayedActiveId}
      expanded={expandedProjects.includes(group.name)}
      active={selectedNewSessionProjectName === group.name}
      onSelectNewSessionTarget={() =>
        onSelectProjectNewSessionTarget({ id: group.id, name: group.name })
      }
      onToggleCollapsed={() => toggleProjectExpanded(group.name)}
      pinnedConversationIds={pinnedConversationIds}
      activeOverride={activeOverride}
      frozenSortKeys={frozenKeys}
      scrollRoot={scrollRoot}
      onRowClick={onRowClick}
      onTogglePinned={handleTogglePinned}
      selectionMode={projectsSelecting}
      selectedIds={selectedIds}
      onToggleSelected={onToggleSelected}
      onProjectAssigned={expandProject}
      onConversationsLoaded={handleFolderConversationsLoaded}
    />
  );

  // Where to draw the folder reorder line, when this folder is the target of a
  // same-section drag. Cross-section drags are moves, not reorders, so they
  // draw no line.
  const projectInsertion = (
    groups: ResolvedProjectGroup[],
    targetName: string,
  ): "before" | "after" | undefined => {
    if (draggedProject === null || overProject !== targetName || draggedProject === targetName) {
      return undefined;
    }
    const from = groups.findIndex(({ group }) => group.name === draggedProject);
    const to = groups.findIndex(({ group }) => group.name === targetName);
    if (from < 0 || to < 0 || from === to) return undefined;
    return from < to ? "after" : "before";
  };

  // Where to draw the section reorder line, when this section is the target.
  const sectionInsertion = (sectionId: string): "before" | "after" | undefined => {
    if (draggedSection === null || overSection !== sectionId || draggedSection === sectionId) {
      return undefined;
    }
    const from = layout.sections.findIndex((section) => section.id === draggedSection);
    const to = layout.sections.findIndex((section) => section.id === sectionId);
    if (from < 0 || to < 0 || from === to) return undefined;
    return from < to ? "after" : "before";
  };

  const sectionDragDisabled = selectionMode || editingIds.size > 0;
  const projectDragActive = draggedProject !== null;
  const projectSortAlphabetical = projectOrder.data?.sort_mode !== "manual";

  // Section structure comes from the muted micro-headers + whitespace
  // alone (Linear-style) — no icons or counts in the headers, no divider
  // rules between groups.
  return (
    <SidebarRowDataProvider
      projectNamesById={projectNamesById}
      projectIconsById={projectIconsById}
      hostsById={hostsById}
      isMobile={isMobile}
      viewerId={viewerId}
      serverInfo={serverInfo}
      onActivate={activateRow}
    >
      <SidebarLayoutContext.Provider value={sidebarLayoutContextValue}>
        <DndContext
          sensors={sensors}
          collisionDetection={(args) => {
            const activeType = args.active.data.current?.type;
            const isProjectDrag = activeType === "project-order";
            const isSectionDrag = activeType === "section-order";
            const isReorderOnly = args.active.data.current?.reorderOnly === true;
            let collisionRect = args.collisionRect;
            if (collisionRect.width === 0 && collisionRect.height === 0) {
              if (args.pointerCoordinates) {
                const { x: left, y: top } = args.pointerCoordinates;
                collisionRect = { ...collisionRect, left, right: left, top, bottom: top };
              } else {
                const activeRect = args.droppableRects.get(args.active.id);
                if (activeRect) {
                  const atOrigin = collisionRect.left === 0 && collisionRect.top === 0;
                  const left = atOrigin ? activeRect.left : collisionRect.left;
                  const top = atOrigin ? activeRect.top : collisionRect.top;
                  collisionRect = {
                    ...collisionRect,
                    left,
                    right: left + activeRect.width,
                    top,
                    bottom: top + activeRect.height,
                    width: activeRect.width,
                    height: activeRect.height,
                  };
                }
              }
            }
            const droppableContainers = args.droppableContainers.filter((container) => {
              const type = container.data.current?.type;
              // A favorites copy drags only to reorder pins or to leave
              // favorites (the ungroup / Sessions zone).
              if (isReorderOnly)
                return type === "pin-order" || type === "fav-item" || type === "ungroup";
              // A session dropped on a favorite project files into that folder;
              // dragging the project header itself still reorders favorites.
              if (
                activeType === "session" &&
                type === "fav-item" &&
                container.data.current?.refType === "project"
              )
                return false;
              // A project drag may reorder a folder header or land on a
              // section; a session drag must never land on either.
              if (isProjectDrag)
                return type === "project-order" || type === "section" || type === "favorites";
              if (isSectionDrag) return type === "section-order";
              // A session drop must not land on the project-only favorites
              // wrapper; the section's own pin target handles it.
              return (
                type !== "project-order" &&
                type !== "section" &&
                type !== "section-order" &&
                type !== "favorites"
              );
            });
            const folderContainers = droppableContainers.filter(
              (container) => container.data.current?.type === "project-order",
            );
            const hasProjectsSection = layout.sections.some(
              (section) => section.kind === "projects",
            );
            if (!args.pointerCoordinates) {
              return closestCenter({
                ...args,
                collisionRect,
                droppableContainers:
                  isProjectDrag && !hasProjectsSection ? folderContainers : droppableContainers,
              });
            }
            if (!isProjectDrag) return pointerWithin({ ...args, droppableContainers });
            // Dropping a project onto a favorites section adds it there,
            // whether or not a `projects` section exists.
            const favoriteContainers = droppableContainers.filter(
              (container) => container.data.current?.type === "favorites",
            );
            const favoriteCollisions = pointerWithin({
              ...args,
              droppableContainers: favoriteContainers,
            });
            if (favoriteCollisions.length > 0) return favoriteCollisions;
            if (!hasProjectsSection) {
              return closestCenter({
                ...args,
                collisionRect,
                droppableContainers: folderContainers,
              });
            }
            const sectionContainers = droppableContainers.filter(
              (container) => container.data.current?.type === "section",
            );
            // Restrict project drops to the project list inside the sidebar.
            const rects = droppableContainers
              .map((c) => args.droppableRects.get(c.id))
              .filter((r) => r != null);
            const y = args.pointerCoordinates.y;
            const x = args.pointerCoordinates.x;
            const sidebar = scrollContainerRef.current?.getBoundingClientRect();
            if (sidebar && (x < sidebar.left || x > sidebar.right)) return [];
            if (
              !rects.length ||
              y < Math.min(...rects.map((r) => r.top)) - 10 ||
              y > Math.max(...rects.map((r) => r.bottom)) + 10
            )
              return [];
            // A folder header wins over the section wrapper it sits inside, so
            // a drop on a folder reorders / moves to its section, while a drop
            // on a section's header or empty body lands on the section.
            const folderCollisions = pointerWithin({
              ...args,
              droppableContainers: folderContainers,
            });
            if (folderCollisions.length > 0) return folderCollisions;
            const sectionCollisions = pointerWithin({
              ...args,
              droppableContainers: sectionContainers,
            });
            if (sectionCollisions.length === 0) return [];
            const target = sectionContainers.find(
              (container) => container.id === sectionCollisions[0]?.id,
            )?.data.current;
            const sourceName = args.active.data.current?.name as string;
            const source = projects.find((project) => project.name === sourceName);
            const currentSectionId =
              source === undefined || source.id === null
                ? null
                : sectionOfProject(layout, source.id);
            const targetSectionId =
              target?.kind === "other_projects" ? null : (target?.id as string | undefined);
            if (currentSectionId === targetSectionId) {
              // Reordering inside the hit section: only its own folders are
              // candidates, or a folder of a neighbouring section that lies
              // nearer the pointer would win and the drop would read as a
              // cross-section move.
              const sectionProjectIds =
                targetSectionId === null
                  ? null
                  : new Set(
                      layout.sections.find((section) => section.id === targetSectionId)
                        ?.projectIds ?? [],
                    );
              const inHitSection = (name: string) => {
                if (targetSectionId === null) {
                  return unclaimedProjectGroups.some((group) => group.name === name);
                }
                const project = projects.find((candidate) => candidate.name === name);
                return (
                  project !== undefined && project.id !== null && sectionProjectIds!.has(project.id)
                );
              };
              return closestCenter({
                ...args,
                collisionRect,
                droppableContainers: folderContainers.filter((container) =>
                  inHitSection(container.data.current?.name as string),
                ),
              });
            }
            return sectionCollisions;
          }}
          // Always-measure so the transient "remove from project" zone (mounted at
          // drag start) is registered as a drop target without a stale layout cache.
          measuring={{ droppable: { strategy: MeasuringStrategy.Always } }}
          onDragStart={handleDragStart}
          onDragEnd={handleDragEnd}
          onDragOver={(event) => {
            const over = event.over?.data.current;
            setOverProject(over?.type === "project-order" ? (over.name as string) : null);
            setOverSection(over?.type === "section-order" ? (over.id as string) : null);
            setOverPin(
              over?.type === "pin-order"
                ? (over.id as string)
                : over?.type === "fav-item" && over.refType === "session"
                  ? (over.refId as string)
                  : null,
            );
          }}
          onDragCancel={() => {
            setActiveDrag(null);
            setOverPin(null);
            setDraggedProject(null);
            setOverProject(null);
            setDraggedSection(null);
            setOverSection(null);
            setDraggedFavorite(null);
          }}
        >
          <RowEditHoldContext.Provider value={reportRowEditing}>
            <PinSavingContext.Provider value={pinWriting}>
              <div
                className="flex flex-col gap-6"
                data-testid="sidebar-conversation-list"
                // Freeze the sort order while the pointer is over the list so rows
                // never move under the cursor. The frozen-keys map is cleared by the
                // effect above once no hold (pointer or open rename edit) remains.
                onMouseEnter={() => setPointerInside(true)}
                onMouseLeave={() => setPointerInside(false)}
              >
                {/* Removing a filed session from its project means dropping it back
            onto the flat "Chats" list — so the Chats section itself is the
            ungroup target (wrapped below). This top strip is only a FALLBACK
            for when there are no ungrouped chats yet, so the Chats section
            isn't rendered and there'd otherwise be nowhere to drop. */}
                {!showShared && activeDrag?.project != null && sections.sessions.length === 0 && (
                  <UngroupDropZone />
                )}
                {totalVisible === 0 && searchQuery && !sessionStatus ? (
                  <>
                    <p className="px-2 py-1 text-ui text-muted-foreground">{emptyMessage}</p>
                    {/* The list is one paginated stream ordered by updated_at across
              owned + shared sessions, so the current filter can be empty on the
              loaded window while its sessions live on a later page. Keep the
              sentinel mounted so pagination continues instead of stranding the
              user on a false "empty" state. */}
                    {hasMorePages && (
                      <InfiniteScrollSentinel
                        scopeKey={activeTab}
                        budgetRef={autoLoadBudget}
                        maxAutoLoads={displayPagination.maxAutoLoads}
                        hasMore={hasMorePages}
                        isFetching={isFetchingNextPage}
                        fetchMore={fetchNextPage}
                        scrollRoot={scrollContainerRef}
                      />
                    )}
                  </>
                ) : (
                  <SortableContext
                    items={projection
                      .filter(
                        ({ section, flatSessions }) =>
                          section.kind !== "favorites" ||
                          section.implicit !== true ||
                          flatSessions.length > 0,
                      )
                      .map(({ section }) => sectionOrderId(section.id))}
                    strategy={verticalListSortingStrategy}
                  >
                    {projection.map(
                      ({
                        section,
                        collapsed: sectionCollapsed,
                        groups,
                        flatSessions,
                        favoriteOrder,
                      }) => {
                        const toggleCollapsed = () => effectiveToggleSectionCollapsed(section.id);
                        const marker = sectionMarkers.get(section.id);
                        const sectionShellProps = {
                          dragDisabled: sectionDragDisabled,
                          insertion: sectionInsertion(section.id),
                          projectDragActive,
                          dropHighlightClass: DROP_TARGET_HIGHLIGHT,
                        };
                        switch (section.kind) {
                          case "favorites": {
                            // The default favorites section stays hidden while it has
                            // no rows; an explicit one keeps its header and hint.
                            const favoriteSessions = new Map(
                              flatSessions.map((conversation) => [conversation.id, conversation]),
                            );
                            const favoriteGroups = new Map(
                              groups.map((entry) => [entry.group.id, entry] as const),
                            );
                            const hasFavoriteRows = (favoriteOrder ?? []).length > 0;
                            if (section.implicit === true && !hasFavoriteRows) return null;
                            // Drop a session here to pin it — pin-precedence then floats
                            // it out of any project into this section. Active only while
                            // dragging an unpinned session; outline-only highlight.
                            return (
                              <SidebarSection
                                key={section.id}
                                section={section}
                                fallbackScrollRoot={scrollContainerRef}
                                {...sectionShellProps}
                              >
                                {(body, optionsAction, headerDrag) => (
                                  <PinDropZone active={activeDrag != null && !activeDrag.isPinned}>
                                    <PinOrderContext.Provider value={pinOrder}>
                                      <ConversationSection
                                        headerDrag={headerDrag}
                                        title={section.name}
                                        conversations={flatSessions}
                                        hasRows={hasFavoriteRows}
                                        activeConversationId={displayedActiveId}
                                        pinnedConversationIds={pinnedConversationIds}
                                        marker={marker?.state ?? null}
                                        backgroundActivityCount={
                                          marker?.backgroundActivityCount ?? 0
                                        }
                                        collapsed={sectionCollapsed}
                                        onToggleCollapsed={toggleCollapsed}
                                        onRowClick={onRowClick}
                                        onTogglePinned={handleTogglePinned}
                                        selectionMode={false}
                                        selectedIds={selectedIds}
                                        onToggleSelected={onToggleSelected}
                                        onProjectAssigned={expandProject}
                                        headerAction={optionsAction}
                                        bodyRef={body.bodyRef}
                                        bodyMaxHeight={body.maxHeight}
                                        emptyMessage={
                                          section.implicit === true
                                            ? undefined
                                            : "Drag sessions here or use Add to favorites"
                                        }
                                        renderRows={() => (
                                          <SortableContext
                                            items={(favoriteOrder ?? []).map((ref) =>
                                              favItemId(section.id, ref.type, ref.id),
                                            )}
                                            strategy={verticalListSortingStrategy}
                                          >
                                            <ul className="flex flex-col gap-px">
                                              {(favoriteOrder ?? []).map((ref, index) => {
                                                if (ref.type === "session") {
                                                  const conversation = favoriteSessions.get(ref.id);
                                                  if (conversation === undefined) return null;
                                                  const key = `${section.id}:${conversation.id}`;
                                                  const isCanonical =
                                                    canonicalInstanceKeys.get(conversation.id) ===
                                                    key;
                                                  return (
                                                    <ConversationRow
                                                      key={favItemId(
                                                        section.id,
                                                        "session",
                                                        conversation.id,
                                                      )}
                                                      conversation={conversation}
                                                      instanceKey={
                                                        isCanonical ? conversation.id : key
                                                      }
                                                      canonical={isCanonical}
                                                      pinReorderCopy
                                                      favoriteItem={{
                                                        sectionId: section.id,
                                                        index,
                                                      }}
                                                      isActive={
                                                        conversation.id === displayedActiveId
                                                      }
                                                      isPinned={pinnedConversationIds.includes(
                                                        conversation.id,
                                                      )}
                                                      onClick={onRowClick}
                                                      onTogglePinned={handleTogglePinned}
                                                      selectionMode={false}
                                                      isSelected={selectedIds.has(conversation.id)}
                                                      onToggleSelected={onToggleSelected}
                                                      onProjectAssigned={expandProject}
                                                      showGoalSessionMarkers={
                                                        showGoalSessionMarkers
                                                      }
                                                    />
                                                  );
                                                }
                                                const entry = favoriteGroups.get(ref.id);
                                                if (entry === undefined) return null;
                                                const expandedKey = `fav:${section.id}:${entry.group.name}`;
                                                return (
                                                  <FavoriteProjectCopy
                                                    key={favItemId(section.id, "project", ref.id)}
                                                    entry={entry}
                                                    sectionId={section.id}
                                                    index={index}
                                                    canonicalInstanceKeys={canonicalInstanceKeys}
                                                    expanded={expandedProjects.includes(
                                                      expandedKey,
                                                    )}
                                                    onToggleCollapsed={() =>
                                                      toggleProjectExpanded(expandedKey)
                                                    }
                                                    windowConversations={entry.group.conversations}
                                                    activeConversationId={displayedActiveId}
                                                    selectedNewSessionProjectName={
                                                      selectedNewSessionProjectName
                                                    }
                                                    onSelectProjectNewSessionTarget={
                                                      onSelectProjectNewSessionTarget
                                                    }
                                                    pinnedConversationIds={pinnedConversationIds}
                                                    activeOverride={activeOverride}
                                                    frozenSortKeys={frozenKeys}
                                                    scrollRoot={body.scrollRoot}
                                                    onRowClick={onRowClick}
                                                    onTogglePinned={handleTogglePinned}
                                                    selectionMode={projectsSelecting}
                                                    selectedIds={selectedIds}
                                                    onToggleSelected={onToggleSelected}
                                                    onProjectAssigned={expandProject}
                                                    onConversationsLoaded={
                                                      handleFolderConversationsLoaded
                                                    }
                                                  />
                                                );
                                              })}
                                            </ul>
                                          </SortableContext>
                                        )}
                                      />
                                    </PinOrderContext.Provider>
                                  </PinDropZone>
                                )}
                              </SidebarSection>
                            );
                          }
                          case "projects": {
                            return (
                              <SidebarSection
                                key={section.id}
                                section={section}
                                fallbackScrollRoot={scrollContainerRef}
                                {...sectionShellProps}
                              >
                                {(body, optionsAction, headerDrag) => (
                                  <SectionGroup
                                    headerDrag={headerDrag}
                                    title={section.name}
                                    collapsed={sectionCollapsed}
                                    onToggleCollapsed={toggleCollapsed}
                                    marker={marker?.state ?? null}
                                    backgroundActivityCount={marker?.backgroundActivityCount ?? 0}
                                    headerAction={optionsAction}
                                  >
                                    <SortableContext
                                      items={groups.map(({ group }) => projectDragId(group.name))}
                                      strategy={verticalListSortingStrategy}
                                    >
                                      <SectionBody body={body} className="flex flex-col gap-px">
                                        {groups.map(({ group }, index) =>
                                          renderProjectFolder(
                                            group,
                                            {
                                              disabled:
                                                projectSortAlphabetical ||
                                                !projectOrder.data ||
                                                saveOrder.isPending ||
                                                selectionMode ||
                                                editingIds.size > 0,
                                              dragDisabled: selectionMode || editingIds.size > 0,
                                              first: index === 0,
                                              last: index === groups.length - 1,
                                              move: (destination) =>
                                                group.id !== null &&
                                                moveProjectInSection(
                                                  section.id,
                                                  group.id,
                                                  (ids, from) => {
                                                    if (destination === "top") return 0;
                                                    if (destination === "bottom")
                                                      return ids.length - 1;
                                                    const adjacent =
                                                      groups[
                                                        index + (destination === "up" ? -1 : 1)
                                                      ];
                                                    if (
                                                      adjacent === undefined ||
                                                      adjacent.group.id === null
                                                    )
                                                      return from;
                                                    const adjacentIndex = ids.indexOf(
                                                      adjacent.group.id,
                                                    );
                                                    return adjacentIndex < 0 ? from : adjacentIndex;
                                                  },
                                                ),
                                              insertion: projectInsertion(groups, group.name),
                                            },
                                            body.scrollRoot,
                                          ),
                                        )}
                                        {groups.length === 0 && !sectionCollapsed && (
                                          <p className="px-2 py-1 text-ui text-muted-foreground">
                                            No projects in this section
                                          </p>
                                        )}
                                      </SectionBody>
                                    </SortableContext>
                                  </SectionGroup>
                                )}
                              </SidebarSection>
                            );
                          }
                          case "other_projects":
                            // Projects claimed by no `projects` section, in the
                            // global project order. The header keeps the full
                            // project-list actions (create, order, expand all).
                            return (
                              <SidebarSection
                                key={section.id}
                                section={section}
                                fallbackScrollRoot={scrollContainerRef}
                                {...sectionShellProps}
                              >
                                {(body, optionsAction, headerDrag) => (
                                  <SectionGroup
                                    headerDrag={headerDrag}
                                    title={section.name}
                                    collapsed={sectionCollapsed}
                                    onToggleCollapsed={toggleCollapsed}
                                    marker={marker?.state ?? null}
                                    backgroundActivityCount={marker?.backgroundActivityCount ?? 0}
                                    afterHeader={
                                      projectsSelecting ? (
                                        <BulkActionBar
                                          selectedIds={selectedIds}
                                          allConversations={projectSessionPool}
                                          onDeselectAll={onDeselectAll}
                                          onExit={onExitSelectionMode}
                                          onProjectAssigned={expandProject}
                                        />
                                      ) : undefined
                                    }
                                    headerAction={
                                      !selectionMode ? (
                                        <>
                                          <ProjectHeaderActions
                                            onOrderChange={(manual) => {
                                              const ranks = new Map(
                                                projectOrder.data?.ordered_project_ids?.map(
                                                  (id, index) => [id, index],
                                                ),
                                              );
                                              const restored = [...projects].sort(
                                                (a, b) =>
                                                  (ranks.get(a.id ?? "") ?? Infinity) -
                                                  (ranks.get(b.id ?? "") ?? Infinity),
                                              );
                                              saveOrder.mutate(manual ? restored : null);
                                            }}
                                            manualOrder={projectOrder.data?.sort_mode === "manual"}
                                            orderDisabled={
                                              saveOrder.isPending || !projectOrder.data
                                            }
                                            projectNames={unclaimedProjectGroups.map(
                                              (group) => group.name,
                                            )}
                                            collapsed={sectionCollapsed}
                                            expandedProjects={expandedProjects}
                                            hasProjectSessions={projectSessionPool.length > 0}
                                            onExpandAll={expandAllProjects}
                                            onCollapseAll={collapseAllProjects}
                                            onProjectCreated={expandProject}
                                            onEnterSelectionMode={() =>
                                              onEnterSelectionMode("projects")
                                            }
                                          />
                                          {optionsAction}
                                        </>
                                      ) : undefined
                                    }
                                  >
                                    <SortableContext
                                      items={unclaimedProjectGroups.map((group) =>
                                        projectDragId(group.name),
                                      )}
                                      strategy={verticalListSortingStrategy}
                                    >
                                      <SectionBody body={body} className="flex flex-col gap-px">
                                        {groups.map(({ group }, index) =>
                                          renderProjectFolder(
                                            group,
                                            {
                                              disabled:
                                                !projectOrder.data ||
                                                saveOrder.isPending ||
                                                selectionMode ||
                                                editingIds.size > 0,
                                              dragDisabled: layout.sections.some(
                                                (candidate) => candidate.kind === "projects",
                                              )
                                                ? selectionMode || editingIds.size > 0
                                                : undefined,
                                              first: index === 0,
                                              last: index === groups.length - 1,
                                              move: (destination) =>
                                                moveProject(group.name, destination),
                                              insertion: projectInsertion(groups, group.name),
                                            },
                                            body.scrollRoot,
                                          ),
                                        )}
                                        {groups.length === 0 && !sectionCollapsed && (
                                          <p className="px-2 py-1 text-ui text-muted-foreground">
                                            No projects
                                          </p>
                                        )}
                                      </SectionBody>
                                    </SortableContext>
                                  </SectionGroup>
                                )}
                              </SidebarSection>
                            );
                          case "other_sessions":
                            // Always rendered, even with no rows: the header carries
                            // the filter menu, so hiding it on an empty slice would
                            // strand the viewer with no way to pick another filter.
                            return (
                              <SidebarSection
                                key={section.id}
                                section={section}
                                fallbackScrollRoot={scrollContainerRef}
                                {...sectionShellProps}
                              >
                                {(body, optionsAction, headerDrag) => (
                                  // Drop a session here to send it to the flat
                                  // "Chats" list — where unfiled, unpinned sessions
                                  // live. Active while dragging a filed session
                                  // (removes it from its project) or a pinned one
                                  // (unpins it), since both have somewhere to land.
                                  <ChatsDropZone
                                    active={
                                      activeDrag != null &&
                                      (activeDrag.project != null || activeDrag.isPinned)
                                    }
                                  >
                                    <ConversationSection
                                      headerDrag={headerDrag}
                                      title={section.name}
                                      active={noProjectNewSessionTargetSelected}
                                      onSelect={onSelectNoProjectNewSessionTarget}
                                      selectionLabel="Use No Project for new sessions"
                                      conversations={flatSessions}
                                      activeConversationId={displayedActiveId}
                                      marker={marker?.state ?? null}
                                      backgroundActivityCount={marker?.backgroundActivityCount ?? 0}
                                      emptyMessage={
                                        sessionStatus ? undefined : SIDEBAR_FILTER_EMPTY[activeTab]
                                      }
                                      footer={
                                        <>
                                          {sessionStatus}
                                          {/* Pagination extends this list, so the
                                          sentinel lives inside its body (and
                                          scrolls with it when capped). */}
                                          {hasMorePages && (
                                            <InfiniteScrollSentinel
                                              scopeKey={activeTab}
                                              budgetRef={autoLoadBudget}
                                              maxAutoLoads={displayPagination.maxAutoLoads}
                                              hasMore={hasMorePages}
                                              isFetching={isFetchingNextPage}
                                              fetchMore={fetchNextPage}
                                              scrollRoot={body.scrollRoot}
                                            />
                                          )}
                                        </>
                                      }
                                      pinnedConversationIds={pinnedConversationIds}
                                      collapsed={sectionCollapsed}
                                      onToggleCollapsed={toggleCollapsed}
                                      rowMeta={(conversation) => {
                                        const key = `${section.id}:${conversation.id}`;
                                        const isCanonical =
                                          canonicalInstanceKeys.get(conversation.id) === key;
                                        return {
                                          instanceKey: isCanonical ? conversation.id : key,
                                          canonical: isCanonical,
                                          moveCopy: true,
                                        };
                                      }}
                                      onRowClick={onRowClick}
                                      onTogglePinned={handleTogglePinned}
                                      selectionMode={sessionsSelecting}
                                      selectedIds={selectedIds}
                                      onToggleSelected={onToggleSelected}
                                      onProjectAssigned={expandProject}
                                      bodyRef={body.bodyRef}
                                      bodyMaxHeight={body.maxHeight}
                                      afterHeader={
                                        sessionsSelecting ? (
                                          <BulkActionBar
                                            selectedIds={selectedIds}
                                            allConversations={sections.sessions}
                                            onDeselectAll={onDeselectAll}
                                            onExit={onExitSelectionMode}
                                            onProjectAssigned={expandProject}
                                          />
                                        ) : undefined
                                      }
                                      headerAction={
                                        // The filter stays reachable while bulk-selecting;
                                        // switching scope just exits selection. The read-all
                                        // and select entry points hide while selection owns
                                        // the header.
                                        !selectionMode ? (
                                          <div className="flex items-center gap-0.5">
                                            {unreadConversations.length > 0 && (
                                              <Tooltip>
                                                <TooltipTrigger asChild>
                                                  <Button
                                                    type="button"
                                                    variant="ghost"
                                                    size="icon-xs"
                                                    aria-label="Mark all sessions as read"
                                                    data-testid="mark-all-sessions-read"
                                                    className="text-muted-foreground max-md:hidden"
                                                    onClick={(event) => {
                                                      event.stopPropagation();
                                                      markConversationsSeen(unreadConversations);
                                                    }}
                                                  >
                                                    <MailOpenIcon className="size-3.5" />
                                                  </Button>
                                                </TooltipTrigger>
                                                <TooltipContent side="bottom">
                                                  Mark all as read
                                                </TooltipContent>
                                              </Tooltip>
                                            )}
                                            <Tooltip>
                                              <TooltipTrigger asChild>
                                                <Button
                                                  asChild
                                                  variant="ghost"
                                                  size="icon-xs"
                                                  aria-label="New session"
                                                  data-testid="sessions-new-session"
                                                  className="text-muted-foreground"
                                                >
                                                  <Link
                                                    to="/"
                                                    componentId="sidebar.sessions_new_chat"
                                                    onClick={(event) => {
                                                      event.stopPropagation();
                                                      onActiveTabChange("mine");
                                                      onRowClick(event);
                                                    }}
                                                  >
                                                    <MessageCirclePlusIcon className="size-3.5" />
                                                  </Link>
                                                </Button>
                                              </TooltipTrigger>
                                              <TooltipContent side="bottom">
                                                New session
                                              </TooltipContent>
                                            </Tooltip>
                                            <Tooltip>
                                              <TooltipTrigger asChild>
                                                <Button
                                                  type="button"
                                                  variant="ghost"
                                                  size="icon-xs"
                                                  aria-label="Select sessions"
                                                  data-testid="toggle-selection-mode"
                                                  className="text-muted-foreground"
                                                  onClick={(event) => {
                                                    event.stopPropagation();
                                                    onEnterSelectionMode("sessions");
                                                  }}
                                                >
                                                  <ListChecksIcon className="size-3.5" />
                                                </Button>
                                              </TooltipTrigger>
                                              <TooltipContent side="bottom">
                                                Select sessions
                                              </TooltipContent>
                                            </Tooltip>
                                            {optionsAction}
                                          </div>
                                        ) : undefined
                                      }
                                      persistentHeaderAction={
                                        <SessionFilterMenu
                                          value={activeTab}
                                          onChange={onActiveTabChange}
                                          multiUser={multiUser}
                                        />
                                      }
                                    />
                                  </ChatsDropZone>
                                )}
                              </SidebarSection>
                            );
                          case "recent": {
                            const recentEmptyMessage = recentUnavailable
                              ? "Recent sessions need a newer server."
                              : recentQuery.isLoading
                                ? undefined
                                : "Sessions you message or answer will show here.";
                            return (
                              <SidebarSection
                                key={section.id}
                                section={section}
                                fallbackScrollRoot={scrollContainerRef}
                                {...sectionShellProps}
                              >
                                {(body, optionsAction, headerDrag) => (
                                  <ConversationSection
                                    headerDrag={headerDrag}
                                    title={section.name}
                                    conversations={flatSessions}
                                    activeConversationId={displayedActiveId}
                                    pinnedConversationIds={pinnedConversationIds}
                                    marker={marker?.state ?? null}
                                    backgroundActivityCount={marker?.backgroundActivityCount ?? 0}
                                    collapsed={sectionCollapsed}
                                    onToggleCollapsed={toggleCollapsed}
                                    onRowClick={onRowClick}
                                    onTogglePinned={handleTogglePinned}
                                    selectionMode={false}
                                    selectedIds={selectedIds}
                                    onToggleSelected={onToggleSelected}
                                    onProjectAssigned={expandProject}
                                    headerAction={optionsAction}
                                    bodyRef={body.bodyRef}
                                    bodyMaxHeight={body.maxHeight}
                                    emptyMessage={recentEmptyMessage}
                                    rowMeta={(conversation) => {
                                      const key = `${section.id}:${conversation.id}`;
                                      const isCanonical =
                                        canonicalInstanceKeys.get(conversation.id) === key;
                                      return {
                                        instanceKey: isCanonical ? conversation.id : key,
                                        canonical: isCanonical,
                                        projectLabel: projectLabelFor(conversation),
                                        dragDisabled: true,
                                      };
                                    }}
                                  />
                                )}
                              </SidebarSection>
                            );
                          }
                        }
                      },
                    )}
                    {!rendersSectionHeader && <NewSectionFallback />}
                  </SortableContext>
                )}
                {/* Browsers keep audio locked until a gesture; the hint clears
                    itself the moment the context unlocks. */}
                <SoundAlertsLockedHint />
              </div>
            </PinSavingContext.Provider>
          </RowEditHoldContext.Provider>
          {/* The dragged row's preview follows the pointer: a compact card showing
          the session's title. Portaled to <body>: the aside always carries a CSS
          translate (the mobile slide-in), which makes it the containing block for
          fixed descendants, so an inline overlay would resolve its viewport
          coordinates against the aside's box and drift off the cursor whenever
          the aside sits away from (0,0) — e.g. the floating peek card. */}
          {createPortal(
            <DragOverlay
              dropAnimation={null}
              className="pointer-events-none"
              style={dragOrigin.current}
            >
              {activeDrag || draggedProject || draggedSection || draggedFavorite ? (
                <div
                  className="pointer-events-none max-w-[16rem] truncate rounded-md border bg-card-solid px-3 py-2 text-ui shadow-tooltip"
                  style={dragOrigin.current ? { maxWidth: "none" } : undefined}
                >
                  {draggedProject ??
                    draggedFavorite?.label ??
                    (draggedSection !== null
                      ? layout.sections.find((section) => section.id === draggedSection)?.name
                      : null) ??
                    activeDrag?.label}
                </div>
              ) : null}
            </DragOverlay>,
            getEmbedRoot() ?? document.body,
          )}
        </DndContext>
      </SidebarLayoutContext.Provider>
    </SidebarRowDataProvider>
  );
}

/** Wraps the flat "Chats" section as an ungroup drop target: a filed session
    released here is removed from its project (back to the flat list, where
    unfiled sessions live). `active` gates the droppable so it only intercepts
    drops while a filed session is being dragged — at rest it's an inert
    wrapper. Outline-only highlight on drag-over (no background fill), matching
    the project folders. */
function ChatsDropZone({ active, children }: { active: boolean; children: ReactNode }) {
  const { setNodeRef, isOver } = useDroppable({
    id: "chats-ungroup",
    data: { type: "ungroup" },
    disabled: !active,
  });
  return (
    <div
      ref={setNodeRef}
      data-testid="sidebar-chats-drop-zone"
      className={cn(
        "rounded-[var(--radius-otto-sm)] transition-colors duration-200 ease-[var(--ease-otto)]",
        active && isOver && DROP_TARGET_HIGHLIGHT,
      )}
    >
      {children}
    </div>
  );
}

/** Wraps the "Pinned" section as a pin drop target: a session released here is
    pinned, which (via the list's pin-precedence) floats it out of any project
    into this section. `active` gates the droppable so it only intercepts drops
    while dragging an unpinned session — at rest, or for an already-pinned
    session, it's an inert wrapper. Outline-only highlight on drag-over,
    matching the project folders and {@link ChatsDropZone}. */
function PinDropZone({ active, children }: { active: boolean; children: ReactNode }) {
  const { setNodeRef, isOver } = useDroppable({
    id: "pinned-pin",
    data: { type: "pin" },
    disabled: !active,
  });
  return (
    <div
      ref={setNodeRef}
      data-testid="sidebar-pin-drop-zone"
      className={cn(
        "rounded-[var(--radius-otto-sm)] transition-colors duration-200 ease-[var(--ease-otto)]",
        active && isOver && DROP_TARGET_HIGHLIGHT,
      )}
    >
      {children}
    </div>
  );
}

/** Fallback ungroup target: a dashed strip shown at the top of the list ONLY
    while dragging a filed session when there are no ungrouped chats (so the
    {@link ChatsDropZone}-wrapped "Chats" section isn't rendered and there'd
    otherwise be nowhere to drop). Releasing on it removes the session from its
    project. The dashed border is the strip's own placeholder identity; the
    drag-over highlight is the shared subtle background tint. */
function UngroupDropZone() {
  const { setNodeRef, isOver } = useDroppable({ id: "__ungroup__", data: { type: "ungroup" } });
  return (
    <div
      ref={setNodeRef}
      data-testid="sidebar-ungroup-drop-zone"
      className={cn(
        "flex items-center gap-1.5 rounded-md border border-dashed border-border px-2 py-1.5 text-muted-foreground text-sm transition-colors",
        isOver && cn(DROP_TARGET_HIGHLIGHT, "text-foreground"),
      )}
    >
      <FolderMinusIcon className="size-3.5 shrink-0" />
      Drop here to remove from project
    </div>
  );
}

/**
 * Aggregate the sidebar marker for a project from its conversations, using the
 * same foreground precedence a row uses (awaiting > running > starting > error
 * > unseen), while summing background activity independently so both can be
 * rendered.
 */
interface ProjectMarkerState {
  state: SessionState | null;
  backgroundActivityCount: number;
}

function projectMarkerState(
  conversations: Conversation[],
  errors: readonly (LatestSessionError | null)[],
  startingConversationId: string | null,
  showGoalSessionMarkers: boolean,
): ProjectMarkerState {
  let awaiting = 0;
  let running = false;
  let backgroundActivityCount = 0;
  let starting = false;
  let error = false;
  let disconnected = false;
  let unseen = false;
  for (const [i, c] of conversations.entries()) {
    backgroundActivityCount += c.background_activity_count ?? 0;
    const state = getSessionState(c, errors[i]);
    if (state?.kind === "awaiting") {
      awaiting += state.count;
    } else if (state?.kind === "running") {
      running = true;
    } else if (c.id === startingConversationId) {
      starting = true;
    } else if (state?.kind === "error") {
      error = true;
    } else if (state?.kind === "disconnected") {
      disconnected = true;
    } else if (
      !(showGoalSessionMarkers && c.goal_state === "active") &&
      isConversationUnseen(c.id, c.updated_at, getConversationForegroundStatus(c))
    ) {
      unseen = true;
    }
  }
  const state: SessionState | null =
    awaiting > 0
      ? { kind: "awaiting", count: awaiting }
      : running
        ? { kind: "running" }
        : starting
          ? { kind: "starting" }
          : error
            ? { kind: "error" }
            : disconnected
              ? { kind: "disconnected" }
              : unseen
                ? { kind: "unseen" }
                : null;
  return { state, backgroundActivityCount };
}

// The shared collapsible header used by every sidebar section and section
// group, so they all align and animate identically (icon · title · markers ·
// hover-chevron).
function SectionHeader({
  headerDrag,
  title,
  icon,
  marker,
  backgroundActivityCount = 0,
  active = false,
  hasAction,
  hasPersistentAction,
  actionFocusVisible,
  collapsed,
  onToggleCollapsed,
  onSelect,
  selectionLabel,
  contextMenu,
  contextMenuDisabled,
}: {
  headerDrag?: ProjectHeaderDrag;
  title: string;
  icon?: ReactNode;
  marker?: SessionState | null;
  /** Sum of background work hidden inside this collapsed section. */
  backgroundActivityCount?: number;
  /** Whether this header represents the current page context. */
  active?: boolean;
  /** Whether the section also renders header controls overlaid at the right
      edge. Those overlays are painted whenever hover isn't available (phones,
      touch tablets/laptops at any width), so the collapsed badges reserve the
      full control column there; only at rest on hover-capable desktops do the
      badges return to the rows' badge column. */
  hasAction?: boolean;
  /** Whether an always-visible control sits at the header's right edge (the
      Sessions filter), which the collapsed badges must clear even at rest on
      hover-capable desktops. */
  hasPersistentAction?: boolean;
  /** Match badge fades to an overlay revealed by focus-visible rather than
      focus-within. Section groups use this to ignore pointer-returned focus. */
  actionFocusVisible?: boolean;
  collapsed: boolean;
  onToggleCollapsed: () => void;
  /** When present, the name chooses a new-session destination and the
      separately rendered chevron remains the only expand/collapse control. */
  onSelect?: () => void;
  selectionLabel?: string;
  /** Optional context-menu content for the header button. */
  contextMenu?: ReactNode;
  /** Suppresses the header context menu while another interaction owns it. */
  contextMenuDisabled?: boolean;
}) {
  const showsMarker = collapsed && (marker != null || backgroundActivityCount > 0);
  // Touch and narrow layouts reserve space for the visible controls. On hover
  // desktops the marker returns to the right edge until the controls appear.
  const clusterRestMargin = showsMarker ? (icon ? "-mr-1" : "mr-1") : "mr-2";
  const clusterHoverDesktopMargin = hasPersistentAction
    ? "[@media((hover:hover)_and_(pointer:fine))]:md:mr-7"
    : showsMarker
      ? icon
        ? "[@media((hover:hover)_and_(pointer:fine))]:md:-mr-1"
        : "[@media((hover:hover)_and_(pointer:fine))]:md:mr-1"
      : "[@media((hover:hover)_and_(pointer:fine))]:md:mr-2";
  const markerFade =
    "[@media((hover:hover)_and_(pointer:fine))]:md:group-hover/section:opacity-0 [@media((hover:hover)_and_(pointer:fine))]:md:group-has-[[data-state=open]]/header:opacity-0";
  const actionFocusFade = actionFocusVisible
    ? "[@media((hover:hover)_and_(pointer:fine))]:md:group-has-[[data-header-controls]_:focus-visible]/header:opacity-0"
    : "[@media((hover:hover)_and_(pointer:fine))]:md:group-has-[[data-header-controls]:focus-within]/header:opacity-0";
  const button = (
    <button
      ref={
        headerDrag
          ? (node) => {
              headerDrag.setNodeRef(node);
              headerDrag.setActivatorNodeRef(node);
            }
          : undefined
      }
      {...headerDrag?.attributes}
      aria-disabled={undefined}
      // Touch retains scrolling and long-press menus, which include move actions.
      onMouseDown={(event) => headerDrag?.listeners?.onMouseDown?.(event)}
      onKeyDown={(event) => headerDrag?.listeners?.onKeyDown?.(event)}
      data-project-order-name={headerDrag ? title : undefined}
      type="button"
      aria-expanded={onSelect ? undefined : !collapsed}
      aria-current={!onSelect && active ? "page" : undefined}
      aria-pressed={onSelect ? active : undefined}
      aria-label={onSelect ? (selectionLabel ?? `Use ${title} for new sessions`) : undefined}
      onClick={(event) => {
        // A pointer click would otherwise leave the header focus-within,
        // keeping the hover-revealed controls up at rest. Keyboard toggles
        // (detail 0) keep focus for the ring.
        if (event.detail > 0) event.currentTarget.blur();
        if (onSelect) onSelect();
        else onToggleCollapsed();
      }}
      className={cn(
        headerDrag &&
          !headerDrag.attributes["aria-disabled"] &&
          "cursor-grab active:cursor-grabbing",
        headerDrag?.isDragging && "opacity-40",
        contextMenu && "select-none [-webkit-touch-callout:none]",
        icon
          ? cn(
              SIDEBAR_ROW,
              "group flex w-full items-center border-0 text-left text-foreground transition-colors",
              SIDEBAR_HOVER_HIGHLIGHT,
              contextMenu && SIDEBAR_OPEN_MENU_HIGHLIGHT,
              active && SIDEBAR_ACTIVE_HIGHLIGHT,
              hasAction &&
                !showsMarker &&
                "pr-8 [@media((hover:hover)_and_(pointer:fine))]:pr-14 [@media((hover:hover)_and_(pointer:fine))]:md:pr-2 [@media((hover:hover)_and_(pointer:fine))]:md:group-hover/header:pr-14 [@media((hover:hover)_and_(pointer:fine))]:md:group-has-[[data-header-controls]:focus-within]/header:pr-14 [@media((hover:hover)_and_(pointer:fine))]:md:group-has-[[data-state=open]]/header:pr-14",
            )
          : "group flex h-7 w-full items-center gap-1 border-0 pr-0 pl-2 text-left text-sm font-normal text-muted-foreground transition-colors hover:text-foreground",
        onSelect && "pl-8",
        onSelect && !icon && active && cn("rounded-md", SIDEBAR_ACTIVE_HIGHLIGHT),
      )}
    >
      {icon ? (
        // Headers with a leading icon (project folders) swap the folder for a
        // chevron on desktop hover/focus, so the caret takes the icon's place
        // rather than trailing the name. Mobile (no hover) keeps the folder
        // icon and shows the trailing chevron below.
        <span className="relative flex size-4 shrink-0 items-center justify-center">
          <span
            className={cn(
              "flex",
              !onSelect &&
                "md:transition-opacity md:group-hover:opacity-0 md:group-focus-visible:opacity-0",
            )}
          >
            {icon}
          </span>
          {!onSelect && (
            <ChevronRightIcon
              className={cn(
                "absolute size-3.5 opacity-0 transition-[transform,opacity]",
                !collapsed && "rotate-90",
                "hidden md:flex md:group-hover:opacity-100 md:group-focus-visible:opacity-100",
              )}
            />
          )}
        </span>
      ) : null}
      <span className="min-w-0 truncate">{title}</span>
      {/* Trailing chevron, rotating on expand. Headers without a leading icon
            reveal it on desktop hover/focus; icon headers show it only on mobile
            (no hover) since desktop swaps the folder for the chevron above. */}
      {!onSelect && (
        <ChevronRightIcon
          className={cn(
            "size-3.5 shrink-0 transition-[transform,opacity]",
            !collapsed && "rotate-90",
            icon
              ? "md:hidden"
              : "md:opacity-0 md:group-hover:opacity-100 md:group-focus-visible:opacity-100",
          )}
        />
      )}
      {/* A hidden row inside this collapsed section carries a marker — surface
            the exact same badge a row would show, pinned to the right edge. */}
      {showsMarker && (
        <span
          className={cn(
            "ml-auto flex shrink-0 items-center justify-center transition-opacity",
            // Touch project rows have one menu; fine pointers also get a shortcut.
            hasAction
              ? cn(
                  icon ? "mr-7 [@media((hover:hover)_and_(pointer:fine))]:mr-14" : "mr-14",
                  clusterHoverDesktopMargin,
                )
              : hasPersistentAction
                ? "mr-7"
                : clusterRestMargin,
            // Dot/spinner markers get the fixed size-6 centered box (center
            // lands 16px from the edge, matching the rows). The "awaiting"
            // pill keeps its natural width so its label isn't clipped.
            isDotMarker(marker ?? null) && backgroundActivityCount === 0 && "w-6",
            // Fade out so the revealed kebab takes the marker's place,
            // mirroring a row's time/marker slot.
            hasAction && cn(markerFade, actionFocusFade),
          )}
        >
          {marker && <SessionStateBadge state={marker} />}
          {backgroundActivityCount > 0 && (
            <BackgroundActivityBadge count={backgroundActivityCount} />
          )}
        </span>
      )}
    </button>
  );

  return (
    <h2 className="relative">
      {contextMenu && !contextMenuDisabled ? (
        <ContextMenu>
          <ContextMenuTrigger asChild>{button}</ContextMenuTrigger>
          {contextMenu}
        </ContextMenu>
      ) : (
        button
      )}
      {onSelect && (
        <button
          type="button"
          aria-label={title}
          aria-expanded={!collapsed}
          onClick={(event) => {
            event.stopPropagation();
            if (event.detail > 0) event.currentTarget.blur();
            onToggleCollapsed();
          }}
          className="-translate-y-1/2 absolute top-1/2 left-1 z-10 flex size-6 items-center justify-center rounded text-muted-foreground hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <ChevronRightIcon
            className={cn("size-3.5 transition-transform", !collapsed && "rotate-90")}
          />
        </button>
      )}
    </h2>
  );
}

// Scope filter on the Sessions heading. A radio group: the options are
// mutually exclusive slices of one list.
function SessionFilterMenu({
  value,
  onChange,
  multiUser,
}: {
  value: SidebarTab;
  onChange: (value: SidebarTab) => void;
  multiUser: boolean;
}) {
  const filters = multiUser
    ? SIDEBAR_FILTERS
    : SIDEBAR_FILTERS.filter((filter) => filter.value !== "shared");
  return (
    <DropdownMenu>
      <Tooltip disableHoverableContent>
        <TooltipTrigger asChild>
          {/* Separate nodes keep the Radix tooltip and menu trigger states independent. */}
          <span className="inline-flex shrink-0">
            <DropdownMenuTrigger asChild>
              <Button
                type="button"
                variant="ghost"
                size="icon-xs"
                aria-label="Filter sessions"
                data-testid="session-filter"
                className="text-muted-foreground"
                onClick={(event) => event.stopPropagation()}
              >
                <ListFilterIcon className="size-3.5" />
              </Button>
            </DropdownMenuTrigger>
          </span>
        </TooltipTrigger>
        <TooltipContent side="bottom" data-noninteractive-tooltip>
          Filter sessions
        </TooltipContent>
      </Tooltip>
      <DropdownMenuContent align="end" className="min-w-44 [&_[role=menuitemradio]]:text-ui">
        <DropdownMenuLabel className="text-muted-foreground text-sm">Display</DropdownMenuLabel>
        <DropdownMenuRadioGroup
          value={value}
          onValueChange={(next) => onChange(next as SidebarTab)}
        >
          {filters.map((filter) => (
            <DropdownMenuRadioItem
              key={filter.value}
              value={filter.value}
              data-testid={`session-filter-${filter.value}`}
            >
              {filter.label}
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function ProjectHeaderActions({
  onOrderChange,
  manualOrder,
  orderDisabled,
  projectNames,
  collapsed,
  expandedProjects,
  hasProjectSessions,
  onExpandAll,
  onCollapseAll,
  onProjectCreated,
  onEnterSelectionMode,
}: {
  onOrderChange: (manual: boolean) => void;
  manualOrder: boolean;
  orderDisabled: boolean;
  projectNames: string[];
  collapsed: boolean;
  expandedProjects: string[];
  /** Whether any project holds sessions — gates the "Select sessions" item, so
      it isn't offered when there's nothing under any folder to select. */
  hasProjectSessions: boolean;
  onExpandAll: (projectNames: string[]) => void;
  onCollapseAll: () => void;
  onProjectCreated: (projectName: string) => void;
  onEnterSelectionMode: () => void;
}) {
  const showExpandControls = !collapsed && projectNames.length > 0;
  const allExpanded =
    projectNames.length > 0 && projectNames.every((name) => expandedProjects.includes(name));
  const anyExpanded = projectNames.some((name) => expandedProjects.includes(name));
  // Hide the menu when there are no projects or sessions to organize.
  const showMenu = showExpandControls || hasProjectSessions || projectNames.length > 0;

  return (
    <div className="flex items-center gap-0.5">
      <NewProjectButton onCreated={onProjectCreated} />
      {showMenu && (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              type="button"
              variant="ghost"
              size="icon-xs"
              aria-label="Project list actions"
              data-testid="project-list-actions"
              className="text-muted-foreground"
              onClick={(event) => event.stopPropagation()}
            >
              <MoreHorizontalIcon className="size-3.5" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="min-w-40">
            <DropdownMenuLabel>Project list actions</DropdownMenuLabel>
            <DropdownMenuSeparator />
            <DropdownMenuSub>
              <DropdownMenuSubTrigger disabled={orderDisabled}>
                Sort projects by
              </DropdownMenuSubTrigger>
              <DropdownMenuSubContent className="min-w-40">
                <DropdownMenuRadioGroup
                  value={manualOrder ? "manual" : "alphabetical"}
                  onValueChange={(value) => onOrderChange(value === "manual")}
                >
                  <DropdownMenuRadioItem value="alphabetical">Alphabetically</DropdownMenuRadioItem>
                  <DropdownMenuRadioItem value="manual">Manual order</DropdownMenuRadioItem>
                </DropdownMenuRadioGroup>
              </DropdownMenuSubContent>
            </DropdownMenuSub>
            {/* Gated independently: each hides only when it would be a no-op, so
                a mixed set offers both. */}
            {showExpandControls && !allExpanded && (
              <DropdownMenuItem
                data-testid="expand-all-projects"
                onSelect={() => onExpandAll(projectNames)}
              >
                <Maximize2Icon className="size-3.5" />
                Expand all
              </DropdownMenuItem>
            )}
            {showExpandControls && anyExpanded && (
              <DropdownMenuItem data-testid="collapse-all-projects" onSelect={onCollapseAll}>
                <Minimize2Icon className="size-3.5" />
                Collapse all
              </DropdownMenuItem>
            )}
            {hasProjectSessions && (
              <DropdownMenuItem
                data-testid="projects-select-sessions"
                onSelect={() => onEnterSelectionMode()}
              >
                <ListChecksIcon className="size-3.5" />
                Select sessions
              </DropdownMenuItem>
            )}
          </DropdownMenuContent>
        </DropdownMenu>
      )}
    </div>
  );
}

// A collapsible group that nests other sections under a single header (e.g.
// "Projects" wrapping each project folder). Reuses SectionHeader so the group
// header is visually identical to a leaf section header.
function SectionGroup({
  headerDrag,
  title,
  collapsed,
  onToggleCollapsed,
  marker,
  backgroundActivityCount = 0,
  headerAction,
  afterHeader,
  children,
}: {
  headerDrag?: ProjectHeaderDrag;
  title: string;
  collapsed: boolean;
  onToggleCollapsed: () => void;
  /** Aggregate marker of the rows hidden while the group is collapsed. */
  marker?: SessionState | null;
  /** Sum of background work hidden inside this collapsed group. */
  backgroundActivityCount?: number;
  /** Optional control overlaid at the group header's right edge (e.g. the
      "collapse all projects" toggle). Always shown without hover support and
      hover/focus-revealed on hover-capable desktop displays. */
  headerAction?: ReactNode;
  /** Optional content rendered directly under the header, above the children
      (and shown even when collapsed) — e.g. the bulk-selection action bar. */
  afterHeader?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section>
      <div className="group/header relative">
        <SectionHeader
          headerDrag={headerDrag}
          title={title}
          marker={marker}
          backgroundActivityCount={backgroundActivityCount}
          hasAction={headerAction != null}
          actionFocusVisible
          collapsed={collapsed}
          onToggleCollapsed={onToggleCollapsed}
        />
        {headerAction && (
          // Always visible (and hit-testable) except on md+ displays with a
          // fine, hover-capable primary pointer — phones, touch tablets and
          // coarse-pointer convertibles have no reliable hover, and the "New
          // project" control lives here (the only way to create a project).
          // On mouse/trackpad desktops it's hover/keyboard-focus-revealed.
          // Reveal on :focus-visible (keyboard) — NOT :focus-within — so
          // clicking the button with the mouse doesn't leave it stuck visible:
          // React reuses the same node when it swaps expand↔revert, so the
          // clicked button keeps focus afterward.
          <div
            data-header-controls
            className="-translate-y-1/2 absolute top-1/2 right-1 flex items-center transition-opacity [@media((hover:hover)_and_(pointer:fine))]:md:opacity-0 [@media((hover:hover)_and_(pointer:fine))]:md:has-[:focus-visible]:opacity-100 [@media((hover:hover)_and_(pointer:fine))]:md:group-has-[[data-state=open]]/header:opacity-100 [@media((hover:hover)_and_(pointer:fine))]:md:group-hover/header:opacity-100"
          >
            {headerAction}
          </div>
        )}
      </div>
      {afterHeader}
      {!collapsed && <div className="flex flex-col gap-px pt-1">{children}</div>}
    </section>
  );
}

function ConversationSection({
  headerDrag,
  title,
  icon,
  marker,
  backgroundActivityCount,
  active,
  conversations,
  activeConversationId,
  pinnedConversationIds,
  collapsed,
  onToggleCollapsed,
  onSelect,
  selectionLabel,
  onRowClick,
  onTogglePinned,
  selectionMode,
  selectedIds,
  onToggleSelected,
  emptyMessage,
  indentRows,
  headerAction,
  headerContextMenu,
  persistentHeaderAction,
  afterHeader,
  footer,
  onProjectAssigned,
  rowMeta,
  renderRows,
  hasRows,
  bodyRef,
  bodyMaxHeight,
}: {
  headerDrag?: ProjectHeaderDrag;
  title?: string;
  /** Optional icon rendered before the title (e.g. project folder icon). */
  icon?: ReactNode;
  /** When collapsed, the aggregate marker of hidden rows (same badge as a row). */
  marker?: SessionState | null;
  /** Aggregate background work surfaced only while the section is collapsed. */
  backgroundActivityCount?: number;
  /** Whether this section header represents the current page context. */
  active?: boolean;
  conversations: Conversation[];
  /** The active conversation's resolved top-level root id, or null — each row
      derives its own `isActive` by comparing against this. Resolved once by the
      list owner so a row never runs the (multi-step) active-root query itself. */
  activeConversationId: string | null;
  pinnedConversationIds: string[];
  /** Whether this section is currently collapsed. */
  collapsed: boolean;
  onToggleCollapsed: () => void;
  onSelect?: () => void;
  selectionLabel?: string;
  onRowClick: (e: MouseEvent<HTMLAnchorElement>) => void;
  onTogglePinned: (conversationId: string) => void;
  selectionMode: boolean;
  selectedIds: Set<string>;
  onToggleSelected: (conversationId: string, shiftKey?: boolean) => void;
  /** Placeholder shown when expanded with no rows (e.g. an empty project). */
  emptyMessage?: ReactNode;
  /** Indent the rows one extra step (used to nest a project's chats). */
  indentRows?: boolean;
  /** Optional control overlaid at the header's right edge. */
  headerAction?: ReactNode;
  /** Optional context-menu content opened from the header button. */
  headerContextMenu?: ReactNode;
  /** Optional control that remains visible at the header's right edge. */
  persistentHeaderAction?: ReactNode;
  /** Optional content rendered directly under the header, above the rows (and
      shown even when collapsed) — e.g. the bulk-selection action bar. */
  afterHeader?: ReactNode;
  /** Optional content rendered after the rows inside the expanded body (e.g. a
      project folder's own infinite-scroll sentinel / loading row). */
  footer?: ReactNode;
  /** Called with the project name when a row is filed into one, so the sidebar
      can expand that (possibly brand-new) project folder. */
  onProjectAssigned?: (projectName: string) => void;
  /** Per-row identity and canonical marker for sections that can duplicate a
      session (recent). Defaults to the row's own id as canonical. */
  rowMeta?: (conversation: Conversation) => ConversationRowMeta;
  /** Replaces the default row list (favorites interleaves folders and rows). */
  renderRows?: () => ReactNode;
  /** Whether the body has rows; defaults to `conversations.length > 0`. */
  hasRows?: boolean;
  /** Ref to the body container, so a capped section can scroll it and root the
      infinite-scroll observers on it. */
  bodyRef?: RefObject<HTMLDivElement | null>;
  /** Caps the body (max height in px) and makes it scroll. */
  bodyMaxHeight?: number | null;
}) {
  const { showGoalSessionMarkers } = useSessionNavigationPreferences();
  // An untitled section is always open — there's no header to collapse it.
  const isCollapsed = title != null && collapsed;
  const protectsCollapsedBadge = isCollapsed && marker != null;
  return (
    <section className="group/section relative">
      {title && (
        // Header + its hover-revealed kebab share a `group/header` scope so the
        // kebab keys off hovering the header alone — NOT the whole section,
        // which would also reveal it when hovering a child row.
        <div className="group/header relative">
          <SectionHeader
            title={title}
            icon={icon}
            marker={marker}
            backgroundActivityCount={backgroundActivityCount}
            active={active}
            hasAction={headerAction != null}
            hasPersistentAction={persistentHeaderAction != null}
            collapsed={isCollapsed}
            onToggleCollapsed={onToggleCollapsed}
            onSelect={onSelect}
            selectionLabel={selectionLabel}
            headerDrag={headerDrag}
            contextMenu={headerContextMenu}
            contextMenuDisabled={selectionMode}
          />
          {(headerAction || persistentHeaderAction) && (
            // The pointer-events gate lives on this OUTER positioned box: CSS
            // hit-testing on a pointer-events-none child falls through to its
            // nearest interactive ancestor, so gating only the inner wrapper
            // would leave this box swallowing clicks aimed at the badges
            // beneath it. The always-visible control re-enables hit-testing
            // for itself; the inner wrapper carries the opacity reveal only.
            // The focus reveal is scoped to focus WITHIN this control cluster
            // (not the whole header): keyboard focus on the header button must
            // not paint hit-testable controls over the still-visible badges —
            // the same scoping the badge fades use, keeping fade and reveal
            // symmetric.
            <div
              data-header-controls
              className={cn(
                "-translate-y-1/2 absolute top-1/2 right-1 flex items-center gap-0.5",
                protectsCollapsedBadge &&
                  "[@media((hover:hover)_and_(pointer:fine))]:md:pointer-events-none [@media((hover:hover)_and_(pointer:fine))]:md:group-has-[[data-header-controls]:focus-within]/header:pointer-events-auto [@media((hover:hover)_and_(pointer:fine))]:md:group-hover/header:pointer-events-auto [@media((hover:hover)_and_(pointer:fine))]:md:group-has-[[data-state=open]]/header:pointer-events-auto [@media((hover:hover)_and_(pointer:fine))]:md:group-has-[[data-testid=session-filter][aria-expanded=true]]/header:pointer-events-auto [@media((hover:hover)_and_(pointer:fine))]:md:has-[[aria-expanded=true]]:pointer-events-auto",
              )}
            >
              {headerAction && (
                // Reveal keys off focus WITHIN the controls cluster (not the
                // whole header): clicking the toggle to expand/collapse focuses
                // the toggle inside SectionHeader — not this cluster — so it no
                // longer pins the actions visible, and keyboard-focusing the
                // control itself still reveals it.
                <div
                  className={cn(
                    "flex items-center rounded transition-opacity",
                    "[@media((hover:hover)_and_(pointer:fine))]:md:opacity-0 [@media((hover:hover)_and_(pointer:fine))]:md:group-has-[[data-header-controls]:focus-within]/header:opacity-100 [@media((hover:hover)_and_(pointer:fine))]:md:group-hover/header:opacity-100 [@media((hover:hover)_and_(pointer:fine))]:md:group-has-[[data-state=open]]/header:opacity-100 [@media((hover:hover)_and_(pointer:fine))]:md:group-has-[[data-testid=session-filter][aria-expanded=true]]/header:opacity-100 [@media((hover:hover)_and_(pointer:fine))]:md:has-[[aria-expanded=true]]:opacity-100",
                  )}
                >
                  {headerAction}
                </div>
              )}
              {persistentHeaderAction && (
                <div className="pointer-events-auto flex items-center">
                  {persistentHeaderAction}
                </div>
              )}
            </div>
          )}
        </div>
      )}
      {afterHeader}
      {!isCollapsed && (
        <div
          ref={bodyRef}
          data-testid="sidebar-section-body"
          className={cn("pt-1", bodyMaxHeight != null && "overflow-y-auto")}
          style={bodyMaxHeight != null ? { maxHeight: bodyMaxHeight } : undefined}
        >
          {(hasRows ?? conversations.length > 0) === false && emptyMessage ? (
            // Expanded but empty — a project with no loaded chats (indented, in a
            // dashed well) or a top-level list whose filter matched nothing.
            indentRows ? (
              <div
                className={cn(
                  SIDEBAR_ROW,
                  "mt-1 mr-2 ml-8 flex flex-col items-start justify-center gap-1.5 px-0 py-1 pb-2 text-left md:py-1 md:pb-2",
                )}
              >
                <p className="text-ui text-muted-foreground">{emptyMessage}</p>
              </div>
            ) : (
              <p className="px-2 py-1 text-ui text-muted-foreground">{emptyMessage}</p>
            )
          ) : renderRows !== undefined ? (
            renderRows()
          ) : (
            // Indent project chats a step under the project-folder name above.
            <ul className={cn("flex flex-col gap-px", indentRows && "pl-6")}>
              {conversations.map((conv) => {
                const meta = rowMeta?.(conv) ?? {
                  instanceKey: conv.id,
                  canonical: true,
                };
                return (
                  <ConversationRow
                    key={meta.instanceKey}
                    conversation={conv}
                    instanceKey={meta.instanceKey}
                    canonical={meta.canonical}
                    pinReorderCopy={meta.pinReorderCopy}
                    moveCopy={meta.moveCopy}
                    dragDisabled={meta.dragDisabled}
                    projectLabel={meta.projectLabel}
                    isActive={conv.id === activeConversationId}
                    isPinned={pinnedConversationIds.includes(conv.id)}
                    onClick={onRowClick}
                    onTogglePinned={onTogglePinned}
                    selectionMode={selectionMode}
                    isSelected={selectedIds.has(conv.id)}
                    onToggleSelected={onToggleSelected}
                    onProjectAssigned={onProjectAssigned}
                    showGoalSessionMarkers={showGoalSessionMarkers}
                  />
                );
              })}
            </ul>
          )}
          {footer}
        </div>
      )}
    </section>
  );
}

// The minimal item-prop shape shared by the dropdown- and context-menu
// primitive families (both wrappers accept a superset). Typing the bundle
// against this — rather than `ComponentProps<typeof DropdownMenuItem>` — lets
// either family satisfy `MenuComponents` so `ConversationMenuItems` can author
// the menu body once and render it under either menu kind.
interface MenuItemProps {
  asChild?: boolean;
  children?: ReactNode;
  className?: string;
  disabled?: boolean;
  textValue?: string;
  variant?: "default" | "destructive";
  // Radix's menu `onSelect` receives a native Event in both families.
  onSelect?: (event: Event) => void;
  "data-testid"?: string;
}

export interface MenuComponents {
  Item: ComponentType<MenuItemProps>;
  Separator: ComponentType<{ className?: string }>;
  Sub: ComponentType<{ children?: ReactNode }>;
  SubTrigger: ComponentType<{
    children?: ReactNode;
    className?: string;
    "data-testid"?: string;
  }>;
  SubContent: ComponentType<{ children?: ReactNode; className?: string }>;
}

// Two stable bundles, one per Radix menu family. Annotated so a future prop
// divergence surfaces here rather than at the call site.
const dropdownBundle: MenuComponents = {
  Item: DropdownMenuItem,
  Separator: DropdownMenuSeparator,
  Sub: DropdownMenuSub,
  SubTrigger: DropdownMenuSubTrigger,
  SubContent: DropdownMenuSubContent,
};

const contextBundle: MenuComponents = {
  Item: ContextMenuItem,
  Separator: ContextMenuSeparator,
  Sub: ContextMenuSub,
  SubTrigger: ContextMenuSubTrigger,
  SubContent: ContextMenuSubContent,
};

/**
 * The conversation row's action menu body — authored once and rendered under
 * both the kebab {@link DropdownMenu} and the row's right-click {@link ContextMenu}
 * via the {@link MenuComponents} bundle, so the two menus stay identical.
 *
 * Radix requires a menu's Content and its Item/Sub* descendants to come from the
 * same primitive family (roving focus / keyboard nav), so the items can't simply
 * be shared as elements — they're rendered through the injected `components` set.
 */
function ConversationMenuItems({
  components: C,
  conversation,
  isPinned,
  isArchived,
  isOwner,
  sharingOff,
  isSingleUser,
  canStop,
  canResume,
  resumePending,
  onResume,
  canMarkUnread,
  currentProject,
  onTogglePinned,
  onAddToFavorites,
  onRemoveFromFavorites,
  onMarkUnread,
  onMarkRead,
  onProjectAssigned,
  moveToProject,
  setShareOpen,
  setForkOpen,
  setIsEditing,
  setStopOpen,
  setDeleteOpen,
  setLeaveOpen,
  setMenuOpen,
  runArchive,
}: {
  components: MenuComponents;
  conversation: Conversation;
  isPinned: boolean;
  isArchived: boolean;
  isOwner: boolean;
  // Server-wide sharing kill switch (OMNIGENT_SHARING_MODE=off): disables the
  // Share item for everyone, independent of the per-user ownership check.
  sharingOff: boolean;
  // Single-user mode: hide the Share item entirely (no other users to share
  // with), rather than disabling it like sharingOff does.
  isSingleUser: boolean;
  canStop: boolean;
  canResume: boolean;
  resumePending: boolean;
  onResume: () => void;
  // Whether "Mark as unread" applies: any row not already showing the
  // unread dot (the active thread and running sessions included).
  canMarkUnread: boolean;
  currentProject: string | null;
  onTogglePinned: (conversationId: string) => void;
  onAddToFavorites?: (conversationId: string) => void;
  onRemoveFromFavorites?: (conversationId: string) => void;
  onMarkUnread: () => void;
  onMarkRead: () => void;
  onProjectAssigned?: (projectName: string) => void;
  moveToProject: ReturnType<typeof useMoveToProject>;
  setShareOpen: (open: boolean) => void;
  setForkOpen: (open: boolean) => void;
  setIsEditing: (editing: boolean) => void;
  setStopOpen: (open: boolean) => void;
  setDeleteOpen: (open: boolean) => void;
  setLeaveOpen: (open: boolean) => void;
  // Closes the controlled kebab after a project pick; a no-op for the
  // (uncontrolled) context menu, which Radix closes on select automatically.
  setMenuOpen: (open: boolean) => void;
  runArchive: () => void;
}) {
  const atPinCap = useContext(PinCapacityContext);
  const pinSaving = useContext(PinSavingContext);
  // Sound mutes are account-scoped and synced; the menu mounts lazily, so
  // reading them here doesn't subscribe every row to preference changes.
  const { account: soundAccount } = useSoundAlertPreferences();
  const soundsMuted = soundAccount.mutedSessionIds.includes(conversation.id);
  // Mobile lacks the horizontal room for a side-opening submenu, so the
  // project picker replaces the menu body in place instead of flying out
  // to the side. `view` swaps between the main actions and that sub-view;
  // desktop always renders the native side-flyout submenu regardless.
  const isMobile = useIsMobileViewport();
  const { trackClick } = useOmnigentAnalytics();
  const [view, setView] = useState<"main" | "projects">("main");

  // The project pick / create / remove flow — shared verbatim by the desktop
  // side-flyout submenu and the mobile in-place sub-view so both behave
  // identically (same moveToProject.mutate, confirmation, and menu close).
  const handleProjectSelect = (project: string) => {
    setMenuOpen(false);
    // Moving to another project is harmless — apply it now, and expand that
    // (possibly new) project so the session is visible in it rather than
    // hidden in a collapsed folder.
    if (project !== "") {
      moveToProject.mutate({ id: conversation.id, project });
      onProjectAssigned?.(project);
      return;
    }
    // Removing just unfiles the session (project_id=""). No confirmation: a
    // first-class project persists when emptied, so removal deletes nothing —
    // the folder stays and can be deleted explicitly from its own kebab.
    moveToProject.mutate({ id: conversation.id, project: "" });
  };

  // Mobile project sub-view: replaces the entire menu body in place (the
  // "Back" row flips `view` without closing the menu or navigating). Reachable
  // only via the mobile project item below, which sits behind the same
  // `isOwner` gate.
  if (isMobile && view === "projects") {
    return (
      <>
        <C.Item
          data-testid="project-picker-back"
          className="whitespace-nowrap"
          // Keep the menu open — just flip back to the main actions.
          onSelect={(e) => {
            e.preventDefault();
            setView("main");
          }}
        >
          <ChevronLeftIcon className="size-3.5" />
          Back
        </C.Item>
        <C.Separator />
        <ProjectPickerMenu
          components={C}
          currentProject={currentProject}
          onSelect={handleProjectSelect}
        />
      </>
    );
  }

  return (
    <>
      {/* Pin/Unpin — mobile-only (md:hidden); desktop uses the
          hover-revealed quick-pin button. Archived rows omit it (archive
          outranks pin). */}
      {!isArchived && (
        <C.Item
          data-testid="pin-conversation"
          disabled={pinSaving || (!isPinned && atPinCap)}
          className="md:hidden"
          onSelect={() => onTogglePinned(conversation.id)}
        >
          {isPinned ? <PinOffIcon className="size-3.5" /> : <PinIcon className="size-3.5" />}
          {isPinned ? "Unpin" : "Pin"}
        </C.Item>
      )}
      {/* Desktop favorites affordance: pin (server) plus the layout ref in one
          action. Hidden below `md`, where the Pin/Unpin item above covers it. */}
      {!isArchived && (onAddToFavorites !== undefined || onRemoveFromFavorites !== undefined) && (
        <C.Item
          data-testid="favorite-conversation"
          disabled={pinSaving || (!isPinned && atPinCap)}
          className="max-md:hidden"
          onSelect={() => {
            setMenuOpen(false);
            if (isPinned) onRemoveFromFavorites?.(conversation.id);
            else onAddToFavorites?.(conversation.id);
          }}
        >
          {isPinned ? <PinOffIcon className="size-3.5" /> : <PinIcon className="size-3.5" />}
          {isPinned ? "Remove from favorites" : "Add to favorites"}
        </C.Item>
      )}
      {/* Single-user mode has no other users to share with — omit the item
          entirely rather than showing it disabled. */}
      {!isSingleUser &&
        (isOwner && !sharingOff ? (
          <C.Item data-testid="share-conversation" onSelect={() => setShareOpen(true)}>
            <ShareIcon className="size-3.5" />
            Share
          </C.Item>
        ) : (
          <Tooltip>
            <TooltipTrigger asChild>
              <div>
                <C.Item data-testid="share-conversation" disabled>
                  <ShareIcon className="size-3.5" />
                  Share
                </C.Item>
              </div>
            </TooltipTrigger>
            {/* Sharing-off is server-wide, so it outranks the per-user owner
                reason when both apply. */}
            <TooltipContent side="left">
              {sharingOff
                ? "Sharing has been disabled for this Omnigent server."
                : "Only the session owner can share this session"}
            </TooltipContent>
          </Tooltip>
        ))}
      <C.Item data-testid="fork-conversation" onSelect={() => setForkOpen(true)}>
        <GitForkIcon className="size-3.5" />
        Fork
      </C.Item>
      <C.Item
        data-testid="copy-session-id"
        onSelect={() => {
          void copyText(conversation.id).then(
            () => showToast("Session ID copied"),
            () => showToast("Couldn't copy the session ID"),
          );
        }}
      >
        <CopyIcon className="size-3.5" />
        Copy session ID
      </C.Item>
      {isOwner ? (
        <C.Item
          data-testid="rename-conversation"
          onSelect={() => {
            trackClick("sidebar.conversation.rename", "button");
            setIsEditing(true);
          }}
        >
          <PencilIcon className="size-3.5" />
          Rename
        </C.Item>
      ) : (
        <Tooltip>
          <TooltipTrigger asChild>
            <div>
              <C.Item data-testid="rename-conversation" disabled>
                <PencilIcon className="size-3.5" />
                Rename
              </C.Item>
            </div>
          </TooltipTrigger>
          <TooltipContent side="left">
            Only the session owner can rename this session
          </TooltipContent>
        </Tooltip>
      )}
      {/* Every row offers one read-state action: "Mark as unread" re-lights
          the pink dot so a session can be flagged to revisit (including the
          one you're currently viewing); a row already showing the dot offers
          the reverse, "Mark as read", which clears it. */}
      {canMarkUnread ? (
        <C.Item
          data-testid="mark-unread-conversation"
          onSelect={() => {
            onMarkUnread();
            setMenuOpen(false);
          }}
        >
          <MailIcon className="size-3.5" />
          Mark as unread
        </C.Item>
      ) : (
        <C.Item
          data-testid="mark-read-conversation"
          onSelect={() => {
            onMarkRead();
            setMenuOpen(false);
          }}
        >
          <MailOpenIcon className="size-3.5" />
          Mark as read
        </C.Item>
      )}
      <C.Item
        data-testid={soundsMuted ? "unmute-sounds-conversation" : "mute-sounds-conversation"}
        onSelect={() => {
          // Read at select time so back-to-back mutes don't overwrite each
          // other with the account snapshot this menu rendered with.
          const preferences = readSoundAlertPreferences();
          const mutedSessionIds = preferences.mutedSessionIds.includes(conversation.id)
            ? preferences.mutedSessionIds.filter((id) => id !== conversation.id)
            : [...preferences.mutedSessionIds, conversation.id];
          writeSoundAlertPreferences({ ...preferences, mutedSessionIds });
          setMenuOpen(false);
        }}
      >
        {soundsMuted ? <BellIcon className="size-3.5" /> : <BellOffIcon className="size-3.5" />}
        {soundsMuted ? "Unmute sounds" : "Mute sounds"}
      </C.Item>
      {/* Projects are a My-sessions-only tool, so filing is owner-only — a
          shared session shows no project affordance. */}
      {isOwner &&
        (isMobile ? (
          // Mobile: no room for a side flyout, so this item swaps the menu
          // body to the project picker in place (see the `view === "projects"`
          // branch above). `preventDefault` keeps the menu open on select.
          <C.Item
            data-testid="move-to-project"
            className="whitespace-nowrap"
            onSelect={(e) => {
              e.preventDefault();
              setView("projects");
            }}
          >
            <FolderInputIcon className="size-3.5" />
            {/* "Add to project" until the session is filed, then "Move
                session" to switch or remove it. */}
            {currentProject ? "Move session" : "Add to project"}
          </C.Item>
        ) : (
          <C.Sub>
            <C.SubTrigger data-testid="move-to-project" className="whitespace-nowrap">
              <FolderInputIcon className="size-3.5" />
              {currentProject ? "Move session" : "Add to project"}
            </C.SubTrigger>
            <C.SubContent className="min-w-56">
              {/* A native submenu flyout — no separate popover layer, so no
                  open/dismiss race with the parent menu. */}
              <ProjectPickerMenu
                components={C}
                currentProject={currentProject}
                onSelect={handleProjectSelect}
              />
            </C.SubContent>
          </C.Sub>
        ))}
      {/* Stop / Resume / Archive / Delete are grouped at the bottom, below a
          divider: lifecycle-ending actions separated from the everyday
          ones above. */}
      <C.Separator />
      {canResume && (
        <C.Item
          data-testid="resume-conversation"
          disabled={!isOwner || resumePending}
          onSelect={() => {
            setMenuOpen(false);
            onResume();
          }}
        >
          {resumePending ? (
            <Loader2Icon className="size-3.5 animate-spin" />
          ) : (
            <PlayIcon className="size-3.5" />
          )}
          <span title={!isOwner ? "Only the session owner can resume this session" : undefined}>
            {resumePending ? "Resuming…" : "Resume session"}
          </span>
        </C.Item>
      )}
      {/* Stop session — only on stoppable sessions whose runner isn't
        already known-offline (canStop). Owner-gated like Delete:
        non-owners see it disabled with an explanatory tooltip. */}
      {canStop &&
        (isOwner ? (
          <C.Item
            data-testid="stop-conversation"
            variant="destructive"
            onSelect={() => setStopOpen(true)}
          >
            <CircleStopIcon className="size-3.5" />
            Stop session
          </C.Item>
        ) : (
          <Tooltip>
            <TooltipTrigger asChild>
              <div>
                <C.Item data-testid="stop-conversation" disabled>
                  <CircleStopIcon className="size-3.5" />
                  Stop session
                </C.Item>
              </div>
            </TooltipTrigger>
            <TooltipContent side="left">
              Only the session owner can stop this session
            </TooltipContent>
          </Tooltip>
        ))}
      {isOwner ? (
        <C.Item data-testid="archive-conversation" onSelect={runArchive}>
          {isArchived ? (
            <ArchiveRestoreIcon className="size-3.5" />
          ) : (
            <ArchiveIcon className="size-3.5" />
          )}
          {isArchived ? "Unarchive" : "Archive"}
        </C.Item>
      ) : (
        <Tooltip>
          <TooltipTrigger asChild>
            <div>
              <C.Item data-testid="archive-conversation" disabled>
                {isArchived ? (
                  <ArchiveRestoreIcon className="size-3.5" />
                ) : (
                  <ArchiveIcon className="size-3.5" />
                )}
                {isArchived ? "Unarchive" : "Archive"}
              </C.Item>
            </div>
          </TooltipTrigger>
          <TooltipContent side="left">
            Only the session owner can {isArchived ? "unarchive" : "archive"} this session
          </TooltipContent>
        </Tooltip>
      )}
      {/* One destructive slot, resolved by ownership — NOT two items. The owner
          deletes the session; a shared-with viewer leaves it (gives up their own
          grant). Non-owners used to get Delete rendered disabled here, an
          always-dead row; Leave is the action that row should have offered all
          along, so it reuses the slot, the trash icon, and the destructive
          styling rather than adding a button beneath it. Single-user mode has no
          sharing, so it keeps the plain owner Delete. */}
      {isOwner || isSingleUser ? (
        <C.Item
          data-testid="delete-conversation"
          variant="destructive"
          onSelect={() => setDeleteOpen(true)}
        >
          <Trash2Icon className="size-3.5" />
          Delete
        </C.Item>
      ) : (
        <C.Item
          data-testid="leave-conversation"
          variant="destructive"
          onSelect={() => setLeaveOpen(true)}
        >
          <Trash2Icon className="size-3.5" />
          Leave session
        </C.Item>
      )}
    </>
  );
}

function SessionErrorHint() {
  return (
    <p className="mt-1 flex items-center gap-1.5 text-sm text-destructive">
      <CircleAlertIcon aria-hidden className="size-3.5 shrink-0" />
      <span>Latest message is an error</span>
    </p>
  );
}

function SessionTooltipContent({
  conversation,
  hostsById,
  hasError,
}: {
  conversation: Conversation;
  hostsById: ReadonlyMap<string, Host>;
  hasError: boolean;
}) {
  const host = conversation.host_id ? hostsById.get(conversation.host_id) : undefined;
  const locationLabel = !conversation.host_id
    ? "Local machine"
    : host?.sandbox_provider
      ? sandboxOptionLabel(host.sandbox_provider)
      : (host?.name ?? conversation.host_id);

  return (
    <TooltipContent
      side="right"
      align="start"
      sideOffset={8}
      data-testid="session-tooltip-content"
      // Mirror PinnedProjectFlyoutContent's compact HoverCard look: title,
      // then muted, small-icon metadata lines.
      className="w-64 max-w-[calc(100vw-2rem)] flex-col items-stretch rounded-lg bg-popover p-2.5 text-popover-foreground whitespace-normal shadow-menu ring-1 ring-foreground/10"
    >
      <p className="sidebar-compact-text line-clamp-3 font-medium">
        {conversation.title ?? conversation.id}
        <span className="font-normal text-muted-foreground">
          {" · "}
          {relativeTime(conversation.updated_at * 1000)}
        </span>
      </p>
      <p
        data-testid="session-tooltip-location"
        className="mt-1 flex items-center gap-1.5 text-sm text-muted-foreground"
      >
        <LaptopIcon aria-hidden className="size-3.5 shrink-0" />
        <span className="truncate">{locationLabel}</span>
      </p>
      {conversation.workspace && (
        <p
          data-testid="session-tooltip-workspace"
          className="mt-1 flex items-center gap-1.5 text-sm text-muted-foreground"
        >
          <FolderIcon aria-hidden className="size-3.5 shrink-0" />
          <span className="truncate">{conversation.workspace}</span>
        </p>
      )}
      {conversation.worktree && conversation.worktree !== conversation.workspace && (
        <p
          data-testid="session-tooltip-worktree"
          className="mt-1 flex items-center gap-1.5 text-sm text-muted-foreground"
        >
          <FolderGit2Icon aria-hidden className="size-3.5 shrink-0" />
          <span className="truncate">{conversation.worktree}</span>
        </p>
      )}
      {conversation.git_branch && (
        <p
          data-testid="session-tooltip-branch"
          className="mt-1 flex items-center gap-1.5 text-sm text-muted-foreground"
        >
          <GitBranchIcon aria-hidden className="size-3.5 shrink-0" />
          <span className="truncate">{conversation.git_branch}</span>
        </p>
      )}
      {hasError && <SessionErrorHint />}
    </TooltipContent>
  );
}

function splitWorkspacePath(workspace: string): { prefix: string; tail: string } {
  const match = /^(.*[\\/])([^\\/]+)$/.exec(workspace);
  return match ? { prefix: match[1]!, tail: match[2]! } : { prefix: "", tail: workspace };
}

function SessionWorkspaceDetail({ workspace }: { workspace: string }) {
  const { prefix, tail } = splitWorkspacePath(workspace);
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          data-testid="session-workspace-detail"
          aria-label={`Working directory: ${workspace}`}
          className="mx-2 mb-1 ml-8 flex min-w-0 max-w-[calc(100%-6rem)] items-center gap-1 rounded px-1 text-left text-xs text-muted-foreground hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          onPointerDown={(event) => {
            event.preventDefault();
            event.stopPropagation();
          }}
          onClick={(event) => event.stopPropagation()}
          onDoubleClick={(event) => event.stopPropagation()}
        >
          <FolderIcon aria-hidden className="size-3 shrink-0" />
          <span
            data-testid="session-workspace-path"
            className="flex min-w-0 max-w-full items-baseline overflow-hidden"
          >
            {prefix && (
              <span data-testid="session-workspace-prefix" className="min-w-0 truncate">
                {prefix}
              </span>
            )}
            <span data-testid="session-workspace-tail" className="shrink-0">
              {tail}
            </span>
          </span>
        </button>
      </PopoverTrigger>
      <PopoverContent
        side="bottom"
        align="start"
        collisionPadding={16}
        className="w-auto max-w-[calc(100vw-2rem)] p-2.5 text-sm"
      >
        <p className="font-medium">Working directory</p>
        <p className="mt-1 max-w-80 break-all font-mono text-xs text-muted-foreground">
          {workspace}
        </p>
      </PopoverContent>
    </Popover>
  );
}

// Max gap between the first click and the dblclick of one double-click.
// Browsers pair clicks within ~500ms; the margin absorbs event-loop delay.
const DOUBLE_CLICK_PAIR_WINDOW_MS = 750;

function ConversationRowImpl({
  conversation,
  instanceKey,
  canonical,
  pinReorderCopy,
  moveCopy,
  dragDisabled,
  favoriteItem,
  projectLabel,
  isActive,
  isPinned,
  onClick,
  onTogglePinned,
  selectionMode,
  isSelected,
  onToggleSelected,
  onProjectAssigned,
  showGoalSessionMarkers,
}: {
  conversation: Conversation;
  /** Unique dnd id for this copy; defaults to the session id. */
  instanceKey?: string;
  /** Whether this copy carries the canonical marker and drag registration. */
  canonical?: boolean;
  /** Favorites copy: pin-reorder target + pin-reorder drag. */
  pinReorderCopy?: boolean;
  /** Sessions copy: still drags (to file / unfile) even when non-canonical. */
  moveCopy?: boolean;
  dragDisabled?: boolean;
  /** Favorites row: its section and index, so it's a `fav-item` reorder target. */
  favoriteItem?: { sectionId: string; index: number };
  /** Trailing muted project name; recent copies only. */
  projectLabel?: string;
  // Computed by the list owner against the resolved top-level root (so a
  // sub-agent view keeps its owning row highlighted). A prop, not a per-row
  // read, so only the two rows whose value flips re-render on a switch.
  isActive: boolean;
  isPinned: boolean;
  onClick: (e: MouseEvent<HTMLAnchorElement>) => void;
  onTogglePinned: (conversationId: string) => void;
  selectionMode: boolean;
  isSelected: boolean;
  onToggleSelected: (conversationId: string, shiftKey?: boolean) => void;
  onProjectAssigned?: (projectName: string) => void;
  showGoalSessionMarkers: boolean;
}) {
  const atPinCap = useContext(PinCapacityContext);
  const pinSaving = useContext(PinSavingContext);
  const layoutContext = useSidebarLayoutContext();
  const resolvedInstanceKey = instanceKey ?? conversation.id;
  const isCanonical = canonical ?? true;
  // The copy's role decides the drag: a favorites-section copy only reorders
  // pins (or leaves favorites), regardless of which copy is canonical; a folder
  // / Sessions copy only files / unfiles and never changes the pin.
  const reorderOnly = pinReorderCopy === true;
  let pinTooltip = isPinned ? "Unpin" : "Pin";
  if (pinSaving) pinTooltip = "Saving pins…";
  else if (!isPinned && atPinCap) pinTooltip = "Unpin a session first";
  const hostsById = useContext(HostsByIdContext);
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  // A client-only `temp:` row (navigate-first create window): no server session
  // yet, so per-row mutations are disabled until it's rekeyed to the real id —
  // otherwise they'd POST to `/v1/sessions/temp:*`. The row still navigates.
  const isProvisionalRow = conversation.provisional === true;
  // Mobile has no real hover, so a tap that navigates would also trip the
  // project flyout's HoverCard and leave it lingering over the chat. Gate the
  // flyout off below the `md` breakpoint (see `projectFlyoutName`).
  const isMobile = useContext(IsMobileContext);
  // Desktop keeps the active row centered; a phone drawer opens at the top.
  const rowRef = useRef<HTMLLIElement>(null);
  useEffect(() => {
    if (!isActive || !isCanonical || isMobile) return;
    rowRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [isActive, isCanonical, isMobile]);
  const rename = useRenameConversation();
  const del = useStopAndDeleteConversation();
  const archive = useArchiveConversation();
  const archiveWorktreePrompt = useArchiveWorktreePrompt();
  const leave = useLeaveSession();
  const moveToProject = useMoveToProject();
  // The kebab's user-facing "Stop session" action. Archiving does NOT go
  // through here — the server stops the session itself once the archived
  // flag commits, so a hidden session never keeps a runner alive.
  const stopSession = useStopSession();
  const resumeSession = useMutation({
    mutationFn: async () => {
      const result = await retrySession(conversation.id);
      if (!result.recovered) throw new Error("No recovery was performed; refresh and try again");
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["conversations"] });
      void queryClient.invalidateQueries({ queryKey: ["project-sessions"] });
      void queryClient.invalidateQueries({ queryKey: ["session", conversation.id] });
      navigate(`/c/${conversation.id}?view=terminal`);
    },
    onError: (error) => showToast(`Couldn't resume the session: ${error.message}`),
  });
  const isArchived = conversation.archived === true;
  const [isEditing, setIsEditing] = useState(false);
  // Hold the list's sort order while this row's rename input is open — the
  // pointer usually drifts out of the sidebar during typing, and a reorder
  // then would shuffle rows around (or move + blur) the input. Cleanup covers
  // commit, cancel, and unmount alike. Layout effect (not passive): rename
  // can start with the pointer already outside the list (context menu is a
  // portal), and a passive effect would leave a post-paint frame where churn
  // could reorder — and blur — the just-mounted input before the hold lands.
  const reportRowEditing = useContext(RowEditHoldContext);
  const activateRow = useContext(RowActivationContext);
  useLayoutEffect(() => {
    if (!isEditing) return;
    reportRowEditing(conversation.id, true);
    return () => reportRowEditing(conversation.id, false);
  }, [isEditing, conversation.id, reportRowEditing]);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [stopOpen, setStopOpen] = useState(false);
  // The kebab menu is controlled so the project submenu can close the whole
  // menu after a pick (a plain click inside the submenu wouldn't otherwise).
  const [menuOpen, setMenuOpen] = useState(false);
  // Opt-in "delete local branch" checkbox (worktree sessions only).
  const [deleteBranch, setDeleteBranch] = useState(false);
  // Unhandled comments die with the session; fetch them when the delete
  // confirmation opens so the dialog can name the count.
  const { data: deleteComments } = useComments(deleteOpen ? conversation.id : undefined);
  const deleteCommentsLine = unhandledCommentsDeleteLine(deleteComments ?? []);
  const [shareOpen, setShareOpen] = useState(false);
  const [forkOpen, setForkOpen] = useState(false);
  const [leaveOpen, setLeaveOpen] = useState(false);
  const gitBranch = conversation.git_branch ?? null;
  // The directory a branch cleanup removes: the recorded worktree, else the
  // session's launch directory. Shown in the delete offer; the gate above
  // stays the recorded branch.
  const branchWorktree = gitBranch !== null ? effectiveWorktree(conversation) : null;
  // Every row action gates on ownership alone — the sidebar carries no
  // effective-permission level, so rename/share/move/drag are owner-only and
  // non-owners get a read-only row. (Finer-grained edit/manage affordances
  // live on the open-session view, which fetches the caller's real level.)
  // Also the id Leave revokes: leaving is a self-revoke, so it needs the
  // viewer's own id — resolved by the time a non-owned row renders, since
  // `isOwner` below is derived from it. From context (ViewerIdContext).
  const viewerId = useContext(ViewerIdContext);
  const isOwner = isOwnedByViewer(conversation, viewerId);
  // Server-wide sharing kill switch (OMNIGENT_SHARING_MODE=off) reported by
  // /v1/info — disables the row's Share item even for managers. Fail open
  // (share enabled) while the capability probe is still loading. From context.
  const serverInfo = useContext(ServerInfoContext);
  const sharingOff = serverInfo !== "loading" && serverInfo.sharing_mode === "off";
  // Single-user mode has no other users to share with, so the Share item is
  // hidden entirely (not just disabled) — mirrors the header Share button.
  const isSingleUser = isSingleUserMode(serverInfo);
  // Gates the kebab's "Stop session" item. `false` = runner known-offline
  // (already stopped — hide the destructive control); `undefined` = not yet
  // observed, don't block. A stopped host-bound session offers Resume in the
  // same menu, without sending another message.
  const runnerOnline = useSessionRunnerOnline(conversation.id);
  const canStop =
    isSessionStoppable({
      labels: conversation.labels,
      hostId: conversation.host_id,
      runnerId: conversation.runner_id,
    }) && runnerOnline !== false;
  const canResume = !isArchived && Boolean(conversation.host_id) && runnerOnline === false;

  // The session's current project NAME, or null when unfiled — drives the
  // kebab submenu label ("Add to project" vs "Move session") and the pinned
  // flyout. Dual-read: prefer the first-class membership (project_id → name via
  // the list-level map from context — no per-row query), falling back to the
  // legacy omni_project label.
  const projectNamesById = useContext(ProjectNamesContext);
  const firstClassProjectName =
    conversation.project_id != null ? projectNamesById.get(conversation.project_id) : undefined;
  const currentProject = firstClassProjectName ?? conversation.labels?.[PROJECT_LABEL_KEY] ?? null;
  const unfiledWorkspace = currentProject === null ? conversation.workspace : "";
  // Pinned sessions are lifted OUT of their project folder into the flat
  // "Pinned" section, so the row no longer shows which project it belongs to.
  // For those rows only, surface the project in a hover flyout. Non-pinned
  // rows already sit inside their project folder, so they don't need it.
  // Disabled on mobile: there's no hover, so a tap would open the HoverCard
  // and leave it overlaying the chat after navigation. Forcing null there
  // routes the row through the plain ContextMenu/link path and restores the
  // native `title` tooltip.
  const projectFlyoutName = !isMobile && isPinned ? currentProject : null;
  // First-class projects can carry a chosen emoji; label-only projects have
  // none, so the flyout falls back to the folder glyph for those.
  const projectIconsById = useContext(ProjectIconsContext);
  const projectFlyoutIcon =
    conversation.project_id != null
      ? (projectIconsById.get(conversation.project_id) ?? null)
      : null;

  // The title the user just committed. The rename's cache write reaches this
  // row as a prop from the list above, which re-renders a tick after the row's
  // own `setIsEditing(false)` — until then the row would repaint the old name.
  const [pendingTitle, setPendingTitle] = useState<string | null>(null);
  useEffect(() => {
    if (pendingTitle === null) return;
    // Cleared once the prop carries the committed name, or once the PATCH
    // settles — the hook overlays the server's title (or rolls back on
    // failure) before flipping status, so the prop is authoritative by then.
    if (conversation.title === pendingTitle || rename.isSuccess || rename.isError) {
      setPendingTitle(null);
    }
  }, [conversation.title, pendingTitle, rename.isSuccess, rename.isError]);

  const label = pendingTitle ?? conversationDisplayLabel(conversation);
  // Subscribed so the just-recorded optimistic label flips the row
  // immediately instead of at the next conversations poll.
  const optimisticTitle = useOptimisticTitle(conversation.id);
  const isProvisionalLabel =
    pendingTitle === null && conversation.title == null && optimisticTitle !== undefined;
  const hasDraft = useHasSessionDraft(conversation.id);
  // The dot shows when the conversation is content-unseen AND either the
  // row isn't the one you're viewing OR you explicitly marked it unread.
  // `isConversationUnseen` still gates on status, so a *running* turn never
  // shows the dot — marking a working session unread is recorded but stays
  // invisible until the turn finishes (then the dot lights like any unseen
  // row). The explicit override only lifts the active-row suppression, so
  // flagging the thread you're currently viewing surfaces the dot at once.
  // A write for another conversation leaves this primitive snapshot unchanged,
  // so useSyncExternalStore skips the heavy row render. The status fed in is the
  // fork's foreground status rather than the raw one, so a session busy only in
  // the background still reads as idle here.
  const readState = useConversationReadState(
    conversation.id,
    conversation.updated_at,
    getConversationForegroundStatus(conversation),
  );
  const isLogicallyUnread = readState.unseen && (!isActive || readState.explicitlyUnread);
  // "Mark as unread" is offered on any row not already showing the dot.
  const canMarkUnread = !isLogicallyUnread;
  // Badge precedence: a pending approval ("Needs response") outranks the
  // unread dot — a session that's both unread and awaiting input should
  // surface the actionable approval tag. The row still renders bold (the
  // unread signal) via `hasUnseenMessages` below. Failures join approvals
  // ahead of the dot without clearing read state.
  const errorConversations = useMemo(() => [conversation], [conversation]);
  const [latestError] = useSessionErrorStates(errorConversations);
  // The bound session's launch/relaunch window: a send is in flight (local
  // status "streaming") or the runner is auto-creating the PTY
  // (`terminalPending`), but the server hasn't confirmed `running` yet — a
  // cold boot, or a send waking a disconnected runner. Without this the row
  // shows nothing while the session is visibly "Starting up…" in the chat.
  // Only the bound (open) conversation has this store state; other rows read
  // false, and the server-derived states above win once they land.
  const isStartingUp = useChatStore(
    (s) => s.conversationId === conversation.id && (s.status === "streaming" || s.terminalPending),
  );
  const mark = rowMark(conversation, {
    unseen: isLogicallyUnread,
    latestError,
    showGoalMarkers: showGoalSessionMarkers,
    starting: isStartingUp,
  });
  const goalState = mark.goal === "none" ? null : mark.goal;
  // A session already framed by an active goal marker advertises the goal
  // instead of the dot, so it never double-signals.
  const hasUnseenMessages = isLogicallyUnread && goalState !== "active";
  const sessionState: SessionState | null =
    mark.state === "awaiting"
      ? { kind: "awaiting", count: mark.awaitingCount }
      : mark.state === "running"
        ? { kind: "running" }
        : mark.state === "starting"
          ? { kind: "starting" }
          : mark.state === "error"
            ? { kind: "error" }
            : mark.state === "disconnected"
              ? { kind: "disconnected" }
              : mark.state === "unseen"
                ? { kind: "unseen" }
                : null;
  // Cold keep-warm: recolor the unseen dot / awaiting tag, and show a blue
  // dot of its own when the idle row has no other session state to display.
  const isCold = conversation.warm_state === "cold";
  const keepWarm = conversation.keep_warm ?? null;
  const hasColdIdleDot = mark.state === "cold";
  const backgroundActivityCount = Math.max(0, conversation.background_activity_count ?? 0);
  const hasBackgroundActivity = backgroundActivityCount > 0;
  const hasGoalMarker = goalState === "active" || goalState === "paused";
  // Drafts share the row's trailing indicator slot, but the active session's
  // composer already makes its draft visible. Live session state wins while
  // present; otherwise only an inactive row needs the draft marker.
  const showDraftIndicator = hasDraft && !isActive && !hasBackgroundActivity && !hasGoalMarker;
  const hasSessionIndicator =
    sessionState !== null ||
    hasColdIdleDot ||
    hasBackgroundActivity ||
    hasGoalMarker ||
    showDraftIndicator;
  const showSharedIndicator = !isOwner;
  const hasTrailingIndicator = hasSessionIndicator || showSharedIndicator;
  const compactMarkerCount =
    ((sessionState !== null && sessionState.kind !== "awaiting") || hasColdIdleDot ? 1 : 0) +
    (hasBackgroundActivity ? 1 : 0) +
    (hasGoalMarker ? 1 : 0);

  // Drag-and-drop: a row is grabbable when the viewer owns it (re-filing is
  // owner-only, like the Move-to-project kebab item), outside selection /
  // archive / rename modes. Dragging it onto a project folder files it there;
  // onto "Chats" unfiles it; onto "Pinned" pins it. The list-level <DndContext>
  // routes the drop; the row only advertises itself and its source project +
  // pinned state via the draggable `data`.
  const {
    listeners: dragListeners,
    setNodeRef: setDragNodeRef,
    isDragging,
  } = useDraggable({
    id: resolvedInstanceKey,
    data: {
      type: "session",
      id: conversation.id,
      label,
      project: currentProject,
      isPinned,
      reorderOnly,
    },
    disabled:
      !isOwner ||
      selectionMode ||
      isArchived ||
      isEditing ||
      isProvisionalRow ||
      dragDisabled ||
      (!isCanonical && !pinReorderCopy && !moveCopy),
  });
  // A drag ends with a synthetic click on the row's <Link> (mousedown + mouseup
  // on the same anchor still fires a click); swallow that one click so a drag
  // doesn't also navigate into the session. Flagged when a drag finishes,
  // cleared on the next tick (after the click that follows pointer-up).
  const justDraggedRef = useRef(false);
  const wasDraggingRef = useRef(false);
  useEffect(() => {
    const was = wasDraggingRef.current;
    wasDraggingRef.current = isDragging;
    if (!was || isDragging) return undefined;
    justDraggedRef.current = true;
    const timer = setTimeout(() => {
      justDraggedRef.current = false;
    }, 0);
    return () => clearTimeout(timer);
  }, [isDragging]);
  // In the Pinned section each row is also a reorder target for other pins. A
  // favorites row instead exposes a `fav-item` target so a mixed (project +
  // session) reorder can land on it.
  const pinOrder = useContext(PinOrderContext);
  const { setNodeRef: setPinOrderNodeRef } = useDroppable(
    favoriteItem !== undefined
      ? {
          id: favItemId(favoriteItem.sectionId, "session", conversation.id),
          data: {
            type: "fav-item",
            sectionId: favoriteItem.sectionId,
            refType: "session",
            refId: conversation.id,
            index: favoriteItem.index,
          },
          // Favorites reorder writes pins, so it follows the same gate as the
          // pin-order target (off while a write is saving, or pins unsupported).
          disabled: pinOrder === null,
        }
      : {
          id: `pin-order:${resolvedInstanceKey}`,
          data: { type: "pin-order", id: conversation.id },
          disabled: !pinOrder || !isPinned || !pinReorderCopy,
        },
  );
  // A pin dragged down lands below this row; one dragged up, or a new pin, above it.
  let pinInsertion: "before" | "after" | undefined;
  if (pinOrder?.draggingId && pinOrder.overId === conversation.id) {
    const from = pinOrder.ids.indexOf(pinOrder.draggingId);
    pinInsertion = from >= 0 && from < pinOrder.ids.indexOf(conversation.id) ? "after" : "before";
  }
  // Merge the drag/drop node refs with the row ref used for scroll-into-view.
  const setRowRef = useCallback(
    (node: HTMLLIElement | null) => {
      rowRef.current = node;
      setDragNodeRef(node);
      setPinOrderNodeRef(node);
    },
    [setDragNodeRef, setPinOrderNodeRef],
  );
  // Timestamps of the last two clicks this row received, for the dblclick
  // rename guard: the list can reorder between the two clicks of a
  // double-click (an updated_at bump slides another row under the cursor),
  // and only a row that saw both clicks may enter rename.
  const recentClickTimesRef = useRef<number[]>([]);

  if (isEditing) {
    return (
      <li>
        <ConversationEditRow
          // Prefer the just-committed name so a rename reopened before the
          // prop catches up starts from what the row shows.
          initialTitle={pendingTitle ?? conversation.title ?? ""}
          onCommit={(title) => {
            // Bail on no-op edits so we don't fire an unnecessary PATCH.
            const trimmed = title.trim();
            if (trimmed && trimmed !== (pendingTitle ?? conversation.title ?? "")) {
              // Set with the same event as `setIsEditing` so both land in one
              // render: the row swaps the input for the new name directly.
              setPendingTitle(trimmed);
              rename.mutate({ id: conversation.id, title: trimmed });
            }
            setIsEditing(false);
          }}
          onCancel={() => setIsEditing(false)}
        />
      </li>
    );
  }

  function confirmDelete() {
    // Fire-and-forget: close the dialog and drop the row immediately so the
    // user isn't blocked on the (potentially slow) DELETE — server-side
    // teardown can take seconds. The mutation removes the row from the
    // cached lists optimistically, which unmounts this component, so
    // anything that must happen on delete either runs here or lives in the
    // hook (a mutate-level callback would never fire).
    setDeleteOpen(false);
    setDeleteBranch(false);
    // Viewing the session being deleted? Leave now, so the chat surface
    // doesn't sit on an id that's about to 404.
    if (isActive) navigate("/", { replace: true });
    del.mutate({ id: conversation.id, deleteBranch: gitBranch !== null && deleteBranch });
  }

  function runArchive() {
    if (isArchived) {
      runUnarchive();
      return;
    }
    archiveWorktreePrompt.requestArchive([conversation], (deleteWorktreeIds) =>
      archiveNow(deleteWorktreeIds.has(conversation.id)),
    );
  }

  function archiveNow(deleteWorktree: boolean) {
    // The archive PATCH sends only the flag: the server stops the session (and
    // tears down a host-spawned runner) in the background once it's committed.
    // A client stop too would race that one against the same runner, and the
    // loser gets a 503 from the already-killed pane.
    //
    // The row leaves the sidebar optimistically (useArchiveConversation flips
    // the cached `archived` flag in onMutate; the list filters archived rows
    // out client-side), so this component unmounts on the next frame. Leaving
    // the archived session's chat surface therefore has to happen HERE,
    // synchronously — not in an onSuccess callback that fires a round-trip
    // later with a stale `isActive`, which used to jump the user off whatever
    // session they'd switched to meanwhile. Mirrors confirmDelete.
    if (isActive) navigate("/", { replace: true });
    archive.mutate({ id: conversation.id, archived: true, deleteWorktree });
    // Offer an Undo (and point at where the session went) — fire NOW, not in a
    // mutate onSuccess: the optimistic overlay unmounts this row on the next
    // frame, and per-call mutate callbacks don't fire once their observer
    // unmounts. A failed archive reconciles the row back with its own error
    // toast. The toast is driven imperatively (module state + app-level
    // Toaster), so it survives this row unmounting.
    showArchiveUndoToast(queryClient, [conversation], navigate);
  }

  function runUnarchive() {
    const nextArchived = !isArchived;
    archive.mutate({ id: conversation.id, archived: nextArchived });
  }

  function confirmLeave() {
    // Leave is a self-revoke, so it needs the viewer's own id. The menu item is
    // gated on the row NOT being owned by the viewer, which is only decidable
    // once the id has resolved — so this is non-null wherever it's reachable.
    if (viewerId === null) return;
    // Close immediately — the row drops out of the list on success, so there's
    // nothing left to show progress against. A failure surfaces as a toast
    // (the row is still there to retry from).
    setLeaveOpen(false);
    leave.mutate(
      { id: conversation.id, viewerId },
      {
        onSuccess: () => {
          // The session 404s for this user now, so don't leave them staring at
          // its chat surface. Mirrors delete/archive's post-mutation navigate.
          if (isActive) navigate("/", { replace: true });
        },
        onError: (err) => {
          const detail = err instanceof Error && err.message ? `: ${err.message}` : "";
          showToast(`Couldn't leave the session${detail}`);
        },
      },
    );
  }

  // Shared by the kebab dropdown and the right-click context menu so the two
  // menus render identical items. `setMenuOpen` is supplied per-call (the
  // controlled kebab passes the real setter; the uncontrolled context menu a
  // no-op — Radix closes it on select).
  const menuItemProps = {
    conversation,
    isPinned,
    isArchived,
    isOwner,
    sharingOff,
    isSingleUser,
    canStop,
    canResume,
    resumePending: resumeSession.isPending,
    onResume: () => resumeSession.mutate(),
    canMarkUnread,
    currentProject,
    onTogglePinned,
    onAddToFavorites: layoutContext?.addFavorite,
    onRemoveFromFavorites: layoutContext?.removeFavorite,
    onMarkUnread: () => markConversationUnread(conversation.id, conversation.updated_at),
    onMarkRead: () => markConversationRead(conversation.id, conversation.updated_at),
    onProjectAssigned,
    moveToProject,
    setShareOpen,
    setForkOpen,
    setIsEditing,
    setStopOpen,
    setDeleteOpen,
    setLeaveOpen,
    runArchive,
  };

  // The clickable row surface. Extracted so it can be rendered bare (selection
  // mode) or wrapped in the right-click ContextMenuTrigger below.
  const rowLink = (
    <Link
      to={selectionMode ? "#" : `/c/${conversation.id}`}
      componentId="sidebar.conversation_switcher"
      className={cn(
        SIDEBAR_ROW,
        "relative flex flex-col justify-center text-left text-foreground transition-colors",
        SIDEBAR_HOVER_HIGHLIGHT,
        // Full width (not 100%+1rem) so the highlight stays inset from the
        // right edge, aligning with the project/folder rows above.
        "w-full",
        // Narrow rows reserve a separate slot for the always-visible menu.
        !selectionMode &&
          (sessionState?.kind === "awaiting"
            ? hasBackgroundActivity && hasGoalMarker
              ? "pr-[10.25rem] max-md:pr-[12.5rem]"
              : hasBackgroundActivity || hasGoalMarker
                ? "pr-[8.75rem] max-md:pr-44"
                : "pr-29 max-md:pr-38"
            : compactMarkerCount >= 3
              ? "pr-17 max-md:pr-26"
              : compactMarkerCount === 2
                ? "pr-12 max-md:pr-21"
                : hasTrailingIndicator
                  ? "pr-8 max-md:pr-17"
                  : "pr-2 max-md:pr-11"),
        // The narrowed reserve must track exactly when the trailing controls
        // appear and the state marker fades — both keyed on `:focus-visible`.
        // `focus-within` also fires for a plain click, which shrank the reserve
        // on the selected row while the marker stayed put, sliding the title
        // under it.
        !selectionMode && "md:group-hover:pr-20 md:group-has-[:focus-visible]:pr-20",
        !selectionMode && menuOpen && "md:pr-20",
        selectionMode && "pr-2 pl-8",
        !selectionMode && isActive && SIDEBAR_ACTIVE_HIGHLIGHT,
        selectionMode && isSelected && SIDEBAR_ACTIVE_HIGHLIGHT,
      )}
      data-goal-state={goalState ?? undefined}
      onClick={(e) => {
        recentClickTimesRef.current = [...recentClickTimesRef.current.slice(-1), performance.now()];
        // Swallow the click that trails a drag so it doesn't navigate.
        if (justDraggedRef.current) {
          e.preventDefault();
          return;
        }
        if (selectionMode) {
          e.preventDefault();
          e.stopPropagation();
          onToggleSelected(conversation.id, e.shiftKey);
          return;
        }
        activateRow(conversation.id, e);
        onClick(e);
      }}
      onDoubleClick={(e) => {
        if (selectionMode) return;
        if (!isOwner) return;
        if (isProvisionalRow) return; // no rename before the real session exists
        e.preventDefault();
        // The dblclick's own second click was already recorded above, so
        // exactly ONE recent click means the first click landed on a different
        // row — the list reordered mid-double-click and renaming here would
        // hit the wrong session. Zero recent clicks (synthetic dblclick with
        // no click events, e.g. in tests) stays allowed.
        const now = performance.now();
        const recent = recentClickTimesRef.current.filter(
          (t) => now - t <= DOUBLE_CLICK_PAIR_WINDOW_MS,
        );
        if (recent.length === 1) return;
        setIsEditing(true);
      }}
      title={isMobile ? (conversation.title ?? conversation.id) : undefined}
    >
      {/* Row 1: the session name. Working, needs-approval, unseen, and draft
          markers render in the shared trailing indicator slot below. */}
      <div className="flex w-full items-center gap-1.5">
        <AgentBadge
          agentId={
            conversation.labels[AGENT_TEMPLATE_LABEL] ??
            conversation.agent_template_id ??
            conversation.agent_id ??
            null
          }
        />
        <span
          className={cn(
            "relative min-w-0 truncate",
            // The optimistic first-prompt label is a placeholder until the
            // server's title lands — dim it so it doesn't read as final.
            isProvisionalLabel && "italic text-muted-foreground",
          )}
        >
          {label}
          {hasUnseenMessages && <span className="sr-only"> (unread)</span>}
        </span>
        {projectLabel && (
          <span
            data-testid="conversation-project-label"
            className="max-w-[76px] shrink-0 truncate text-muted-foreground text-xs"
          >
            {projectLabel}
          </span>
        )}
      </div>
    </Link>
  );

  // Provisional (`temp:`) row: navigable, but no mutating affordances (kebab,
  // context menu, pin, archive, drag) until the real session exists — those
  // would POST to `/v1/sessions/temp:*`. Rekey to the real id (`hydrateLocal-
  // Conversation`) drops `provisional` and the full row renders.
  if (isProvisionalRow) {
    return (
      <li ref={rowRef} className="group relative">
        {rowLink}
      </li>
    );
  }

  return (
    // Drag props on the <li> so the whole row is grabbable; `isDragging` dims
    // it. `setRowRef` merges the drag node ref with the scroll-into-view ref.
    <li
      ref={setRowRef}
      data-sidebar-session-id={conversation.id}
      data-sidebar-canonical={isCanonical ? "true" : undefined}
      onMouseDown={(event) => {
        // Portaled dialogs bubble through this row but must not start a drag.
        if (event.currentTarget.contains(event.target as Node)) {
          dragListeners?.onMouseDown?.(event);
        }
      }}
      onTouchStart={(event) => {
        if (event.currentTarget.contains(event.target as Node)) {
          dragListeners?.onTouchStart?.(event);
        }
      }}
      className={cn("group relative", isDragging && "opacity-40")}
    >
      {pinInsertion && pinOrder?.draggingId !== conversation.id && (
        <span
          data-testid="pin-order-insertion"
          className="pointer-events-none absolute inset-x-0 z-10 h-0.5 bg-primary"
          style={pinInsertion === "before" ? { top: 0 } : { bottom: 0 }}
        />
      )}
      {/* Right-click anywhere on the row opens the same actions as the kebab.
          Suppressed in selection mode (bulk-select owns the row), where the
          bare link is rendered instead. ContextMenuTrigger preventDefaults the
          native contextmenu event, so right-click never navigates; asChild
          merges its handler onto the Link, preserving left-click / double-click.
          Pinned, project-owned rows nest a HoverCardTrigger around the Link so
          hovering surfaces the project flyout — the trigger sits innermost so
          both the context menu and the hover card keep their handlers/refs on
          the Link. */}
      {selectionMode ? (
        projectFlyoutName ? (
          <HoverCard openDelay={150} closeDelay={0}>
            <HoverCardTrigger asChild>{rowLink}</HoverCardTrigger>
            <PinnedProjectFlyoutContent
              title={conversation.title ?? conversation.id}
              projectName={projectFlyoutName}
              projectIcon={projectFlyoutIcon}
              gitBranch={gitBranch}
              hasError={sessionState?.kind === "error"}
            />
          </HoverCard>
        ) : isMobile ? (
          rowLink
        ) : (
          <Tooltip>
            <TooltipTrigger asChild>{rowLink}</TooltipTrigger>
            <SessionTooltipContent
              conversation={conversation}
              hostsById={hostsById}
              hasError={sessionState?.kind === "error"}
            />
          </Tooltip>
        )
      ) : projectFlyoutName ? (
        <HoverCard openDelay={150} closeDelay={0}>
          <ContextMenu>
            <ContextMenuTrigger asChild>
              <HoverCardTrigger asChild>{rowLink}</HoverCardTrigger>
            </ContextMenuTrigger>
            <ContextMenuContent className="min-w-44">
              <ConversationMenuItems
                components={contextBundle}
                setMenuOpen={() => {}}
                {...menuItemProps}
              />
            </ContextMenuContent>
          </ContextMenu>
          <PinnedProjectFlyoutContent
            title={conversation.title ?? conversation.id}
            projectName={projectFlyoutName}
            projectIcon={projectFlyoutIcon}
            gitBranch={gitBranch}
            hasError={sessionState?.kind === "error"}
          />
        </HoverCard>
      ) : isMobile ? (
        <ContextMenu>
          <ContextMenuTrigger asChild>{rowLink}</ContextMenuTrigger>
          <ContextMenuContent className="min-w-44">
            <ConversationMenuItems
              components={contextBundle}
              setMenuOpen={() => {}}
              {...menuItemProps}
            />
          </ContextMenuContent>
        </ContextMenu>
      ) : (
        <Tooltip>
          <ContextMenu>
            <ContextMenuTrigger asChild>
              <div className="w-full">
                <TooltipTrigger asChild>{rowLink}</TooltipTrigger>
              </div>
            </ContextMenuTrigger>
            <ContextMenuContent className="min-w-44">
              <ConversationMenuItems
                components={contextBundle}
                setMenuOpen={() => {}}
                {...menuItemProps}
              />
            </ContextMenuContent>
          </ContextMenu>
          <SessionTooltipContent
            conversation={conversation}
            hostsById={hostsById}
            hasError={sessionState?.kind === "error"}
          />
        </Tooltip>
      )}
      {!selectionMode && unfiledWorkspace && (
        <SessionWorkspaceDetail workspace={unfiledWorkspace} />
      )}
      {selectionMode ? (
        <span className="-translate-y-1/2 pointer-events-none absolute top-1/2 left-2 flex items-center">
          {isSelected ? (
            <SquareCheckIcon className="size-4 text-primary" />
          ) : (
            <SquareIcon className="size-4 text-muted-foreground" />
          )}
        </span>
      ) : hasSessionIndicator ? (
        <span
          className={cn(
            SESSION_STATE_SLOT_CLASS,
            "right-1 max-md:right-10",
            unfiledWorkspace && "top-4",
            // The wide "awaiting" pill keeps its natural width; every other
            // marker (running/starting/unseen/cold dot, or the draft pencil)
            // sits in the fixed centered box so it lines up under the kebab.
            (isDotMarker(sessionState) || hasColdIdleDot) &&
              (compactMarkerCount >= 3
                ? "w-16 justify-center gap-1"
                : compactMarkerCount === 2
                  ? "w-11 justify-center gap-1"
                  : SESSION_STATE_DOT_SLOT_CLASS),
            sessionState?.kind === "awaiting" &&
              (hasBackgroundActivity || hasGoalMarker) &&
              "gap-1",
          )}
        >
          {sessionState !== null ? (
            <SessionStateBadge state={sessionState} cold={isCold} keepWarm={keepWarm} />
          ) : hasColdIdleDot ? (
            <ColdIdleDot keepWarm={keepWarm} />
          ) : showDraftIndicator ? (
            <span
              role="img"
              aria-label="Draft"
              data-testid="conversation-draft-indicator"
              className="inline-flex h-5 shrink-0 items-center justify-center text-muted-foreground"
            >
              <MessageCircleDashedIcon aria-hidden className="size-3.5" />
            </span>
          ) : null}
          {hasBackgroundActivity && <BackgroundActivityBadge count={backgroundActivityCount} />}
          {hasGoalMarker && <GoalActivityBadge state={goalState} />}
        </span>
      ) : null}
      {!selectionMode && showSharedIndicator && (
        <span
          role="img"
          aria-label="Shared session"
          title="Shared with you"
          className={cn(
            "-translate-y-1/2 pointer-events-none absolute top-1/2 inline-flex h-5 w-6 shrink-0 items-center justify-center text-muted-foreground transition-opacity md:group-hover:opacity-0 md:group-has-[:focus-visible]:opacity-0 md:group-has-[[aria-expanded=true]]:opacity-0",
            hasSessionIndicator ? "right-8 max-md:right-17" : "right-1 max-md:right-10",
          )}
        >
          <UsersIcon className="size-3.5" aria-hidden="true" />
        </span>
      )}
      {/* Trailing controls (pin + kebab) share one absolutely-positioned flex
          row, so their spacing is defined once (gap-0.5) and stays aligned
          with the project-folder header actions, which use the same pattern.
          The kebab is the rightmost child (pinned to right-1); the pin sits a
          gap to its left. Hidden entirely while selecting (bulk mode owns the
          row controls). */}
      {!selectionMode && (
        <ContextMenu>
          <ContextMenuTrigger asChild>
            <div
              className={cn(
                "-translate-y-1/2 absolute top-1/2 right-1 flex items-center gap-0.5",
                unfiledWorkspace && "top-4",
              )}
            >
              {/* Archived rows omit the pin entirely: pinning is meaningless there
              (archive outranks pin), so there's no pin action even on hover. */}
              {!isArchived && (
                <Tooltip disableHoverableContent>
                  <TooltipContent>
                    <TooltipArrow />
                    {pinTooltip}
                  </TooltipContent>
                  <TooltipTrigger asChild>
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon-xs"
                      aria-label={isPinned ? "Unpin conversation" : "Pin conversation"}
                      data-testid="quick-pin-conversation"
                      aria-disabled={pinSaving || (!isPinned && atPinCap)}
                      className={cn(
                        // Desktop-only quick affordance: hidden on mobile (the kebab's
                        // Pin item below covers that), hover/focus-revealed from `md`
                        // up. Pinned rows no longer keep a persistent pin marker, since
                        // the "Pinned" section header (and pinned-first ordering inside
                        // a project) already conveys the pinned state. Revealed glyph:
                        // unpin if pinned, pin otherwise.
                        //
                        // `md:inline-flex` (not `md:block`): the Button base is
                        // `inline-flex` and relies on it for `items-center
                        // justify-center` to center the icon. `md:block` would override
                        // that display and collapse the centering, leaving the glyph
                        // pinned to the top-left of the button — so keep the flex
                        // display when revealing it.
                        "text-muted-foreground transition-opacity",
                        "hidden md:inline-flex",
                        "md:opacity-0 md:group-hover:opacity-100",
                        "md:group-has-[:focus-visible]:opacity-100 md:group-has-[[aria-expanded=true]]:opacity-100",
                      )}
                      onClick={(e) => {
                        // Keep the toggle click off the surrounding Link (no navigation).
                        e.preventDefault();
                        e.stopPropagation();
                        if (pinSaving) return;
                        onTogglePinned(conversation.id);
                      }}
                    >
                      {isPinned ? (
                        <PinOffIcon className="size-3.5" data-icon-size="14" />
                      ) : (
                        <PinIcon className="size-3.5" data-icon-size="14" />
                      )}
                    </Button>
                  </TooltipTrigger>
                </Tooltip>
              )}
              {/* Archive is owner-only, same as the kebab's Archive item; non-owners
              don't get the quick affordance and instead see that item disabled
              with an explanation. */}
              {isOwner && (
                <Tooltip disableHoverableContent>
                  <TooltipContent>
                    <TooltipArrow />
                    {isArchived ? "Unarchive" : "Archive"}
                  </TooltipContent>
                  <TooltipTrigger asChild>
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon-xs"
                      aria-label={isArchived ? "Unarchive conversation" : "Archive conversation"}
                      data-testid="quick-archive-conversation"
                      className={cn(
                        "text-muted-foreground transition-opacity",
                        "hidden md:inline-flex",
                        "md:opacity-0 md:group-hover:opacity-100",
                        "md:group-has-[:focus-visible]:opacity-100 md:group-has-[[aria-expanded=true]]:opacity-100",
                      )}
                      onClick={(e) => {
                        // Keep the toggle click off the surrounding Link (no navigation).
                        e.preventDefault();
                        e.stopPropagation();
                        if (!isArchived) {
                          runArchive();
                        } else {
                          runUnarchive();
                        }
                      }}
                    >
                      {isArchived ? (
                        <ArchiveRestoreIcon className="size-3.5" data-icon-size="14" />
                      ) : (
                        <ArchiveIcon className="size-3.5" data-icon-size="14" />
                      )}
                    </Button>
                  </TooltipTrigger>
                </Tooltip>
              )}

              <DropdownMenu open={menuOpen} onOpenChange={setMenuOpen}>
                <DropdownMenuTrigger asChild>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon-xs"
                    aria-label="Conversation actions"
                    data-testid="conversation-actions"
                    // Keep lifecycle actions reachable on touch-sized screens.
                    className={cn(
                      "text-muted-foreground transition-opacity",
                      "inline-flex size-8 md:size-6",
                      "md:opacity-0 md:group-hover:opacity-100 md:group-has-[:focus-visible]:opacity-100",
                      "md:aria-expanded:opacity-100",
                    )}
                    onClick={(e) => {
                      // Keep the trigger click from bubbling into the Link.
                      e.preventDefault();
                      e.stopPropagation();
                    }}
                  >
                    <MoreHorizontalIcon className="size-3.5" data-icon-size="14" />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" className="min-w-44">
                  <ConversationMenuItems
                    components={dropdownBundle}
                    setMenuOpen={setMenuOpen}
                    {...menuItemProps}
                  />
                </DropdownMenuContent>
              </DropdownMenu>
            </div>
          </ContextMenuTrigger>
          <ContextMenuContent className="min-w-44">
            <ConversationMenuItems
              components={contextBundle}
              setMenuOpen={() => {}}
              {...menuItemProps}
            />
          </ContextMenuContent>
        </ContextMenu>
      )}
      {/* Mount only while open — one per row, its hook tree + JSX would
          otherwise run closed on every row re-render. */}
      {shareOpen && (
        <PermissionsModal
          sessionId={conversation.id}
          open={shareOpen}
          onOpenChange={setShareOpen}
        />
      )}
      {forkOpen && (
        <ForkSessionDialog
          sourceSessionId={conversation.id}
          sourceTitle={conversation.title}
          sourceWorkspace={effectiveWorktree(conversation)}
          sourceHostId={conversation.host_id}
          sourceGitBranch={conversation.git_branch}
          open
          onOpenChange={setForkOpen}
        />
      )}
      <Dialog
        open={deleteOpen}
        onOpenChange={(open) => {
          setDeleteOpen(open);
          // Reset the checkbox on close so it doesn't carry over.
          if (!open) setDeleteBranch(false);
        }}
      >
        {/* Body only while open — see the share modal above. */}
        {deleteOpen && (
          <DialogContent
            // Don't trigger the surrounding Link when the modal opens
            // — the dialog content is a portal, but defensively belt-
            // and-braces the click path.
            onClick={(e) => e.stopPropagation()}
          >
            <DialogHeader>
              <DialogTitle>Delete conversation?</DialogTitle>
              <DialogDescription>
                <span className="font-medium break-all">{label}</span> and all of its history will
                be removed. This cannot be undone.
              </DialogDescription>
            </DialogHeader>
            {deleteCommentsLine !== null && (
              <p className="text-sm text-muted-foreground">{deleteCommentsLine}</p>
            )}
            {gitBranch !== null && (
              <div className="flex flex-col gap-2 rounded-md border border-destructive/40 bg-destructive/5 p-3">
                <p className="text-sm text-muted-foreground">
                  Optionally clean up the git worktree. These actions are{" "}
                  <span className="font-semibold text-destructive">irreversible</span>.
                </p>
                <label className="flex cursor-pointer items-start gap-2 text-ui">
                  <input
                    type="checkbox"
                    data-testid="delete-branch-checkbox"
                    checked={deleteBranch}
                    onChange={(e) => setDeleteBranch(e.target.checked)}
                    className="mt-0.5 size-4 shrink-0 accent-destructive"
                  />
                  <GitBranchIcon className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" />
                  <span className="min-w-0">
                    Delete local branch{" "}
                    <code className="break-all rounded bg-muted px-1 py-0.5 text-sm">
                      {gitBranch}
                    </code>
                    {branchWorktree !== null && (
                      <span
                        data-testid="delete-branch-worktree-path"
                        className="mt-0.5 block break-all font-mono text-xs text-muted-foreground"
                      >
                        {branchWorktree}
                      </span>
                    )}
                  </span>
                </label>
              </div>
            )}
            {/* Drop the default footer divider + muted bar so the actions
              blend into the dialog body (same background). */}
            <DialogFooter className="border-t-0 bg-transparent">
              <Button
                type="button"
                variant="ghost"
                onClick={() => setDeleteOpen(false)}
                disabled={del.isPending}
              >
                Cancel
              </Button>
              <Button
                type="button"
                variant="destructive"
                onClick={confirmDelete}
                disabled={del.isPending}
                componentId="sidebar.conversation.delete"
              >
                Delete
              </Button>
            </DialogFooter>
          </DialogContent>
        )}
      </Dialog>
      {archiveWorktreePrompt.dialog}
      <Dialog open={leaveOpen} onOpenChange={setLeaveOpen}>
        {leaveOpen && (
          <DialogContent
            // Keep dialog clicks off the surrounding Link (same defensive
            // handling as the delete dialog above).
            onClick={(e) => e.stopPropagation()}
          >
            <DialogHeader>
              <DialogTitle>Leave session?</DialogTitle>
              <DialogDescription>
                <span className="font-medium break-all">{label}</span> will be removed from your
                sidebar. Nothing is deleted — the session and its history stay with its owner, who
                can share it with you again.
              </DialogDescription>
            </DialogHeader>
            <DialogFooter className="border-t-0 bg-transparent">
              <Button
                type="button"
                variant="ghost"
                onClick={() => setLeaveOpen(false)}
                disabled={leave.isPending}
              >
                Cancel
              </Button>
              <Button
                type="button"
                variant="destructive"
                data-testid="confirm-leave-conversation"
                onClick={confirmLeave}
                disabled={leave.isPending}
                componentId="sidebar.conversation.leave"
              >
                Leave
              </Button>
            </DialogFooter>
          </DialogContent>
        )}
      </Dialog>
      {/* The stale-error reset lives on the kebab item's onSelect (the only
          open path) — onOpenChange only fires for Radix-initiated closes. */}
      <Dialog open={stopOpen} onOpenChange={setStopOpen}>
        {stopOpen && (
          <DialogContent
            // Keep dialog clicks off the surrounding Link (same defensive
            // handling as the delete dialog above).
            onClick={(e) => e.stopPropagation()}
          >
            <DialogHeader>
              <DialogTitle>Stop session?</DialogTitle>
              <DialogDescription>
                This terminates the running session for <span className="font-medium">{label}</span>
                {conversation.host_id
                  ? " and stops its runner, including side chats running on it."
                  : "."}{" "}
                Conversation histories are kept.
              </DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <Button type="button" variant="ghost" onClick={() => setStopOpen(false)}>
                Cancel
              </Button>
              <Button
                type="button"
                variant="destructive"
                data-testid="stop-session-confirm"
                onClick={() => {
                  // Close now and stop in the background — keeping the modal open
                  // for the whole kill blocks the rest of the sidebar. A failure
                  // surfaces as a toast since the dialog is already gone.
                  setStopOpen(false);
                  stopSession.mutate(conversation.id, {
                    onError: (err) => {
                      const detail = err instanceof Error && err.message ? `: ${err.message}` : "";
                      showToast(`Couldn't stop the session${detail}`);
                    },
                  });
                }}
                componentId="sidebar.conversation.stop"
              >
                Stop session
              </Button>
            </DialogFooter>
          </DialogContent>
        )}
      </Dialog>
    </li>
  );
}

// The `conversation` fields the row renders. The comparator below compares
// these (not object identity), so a live-updates merge that only touches an
// unrendered field (e.g. an updated_at bump) doesn't re-render the row.
// Keep in sync with the row + its helpers (conversationDisplayLabel,
// getSessionState, isOwnedByViewer, isSessionStoppable).
const RENDERED_CONVERSATION_FIELDS: readonly (keyof Conversation)[] = [
  "id",
  "title",
  "archived",
  "status",
  "updated_at",
  "git_branch",
  "host_id",
  "workspace",
  "worktree",
  "runner_id",
  "project_id",
  "owner",
  "pending_elicitations_count",
  "child_pending_elicitations_count",
  "goal_state",
  "foreground_status",
  "background_activity_count",
  "warm_state",
  "keep_warm",
  "agent_id",
  "agent_template_id",
];

function conversationRenderEqual(a: Conversation, b: Conversation): boolean {
  if (a === b) return true;
  for (const key of RENDERED_CONVERSATION_FIELDS) {
    if (key === "keep_warm") {
      // Structured value: each frame/merge yields a fresh object, so compare
      // by content or every snapshot would re-render every cold row.
      if (JSON.stringify(a.keep_warm ?? null) !== JSON.stringify(b.keep_warm ?? null)) return false;
      continue;
    }
    if (a[key] !== b[key]) return false;
  }
  // `labels` is an object; compare by value (rename/project moves ride here).
  return JSON.stringify(a.labels) === JSON.stringify(b.labels);
}

// Memoized with a render-field comparator (not shallow identity): a row
// re-renders only when its displayed data or `isActive` changes. Handler props
// are stabilized at the list owner so they don't defeat it.
const ConversationRow = memo(ConversationRowImpl, (prev, next) => {
  return (
    prev.instanceKey === next.instanceKey &&
    prev.canonical === next.canonical &&
    prev.pinReorderCopy === next.pinReorderCopy &&
    prev.moveCopy === next.moveCopy &&
    prev.dragDisabled === next.dragDisabled &&
    prev.favoriteItem?.sectionId === next.favoriteItem?.sectionId &&
    prev.favoriteItem?.index === next.favoriteItem?.index &&
    prev.projectLabel === next.projectLabel &&
    prev.isActive === next.isActive &&
    prev.isPinned === next.isPinned &&
    prev.showGoalSessionMarkers === next.showGoalSessionMarkers &&
    prev.selectionMode === next.selectionMode &&
    prev.isSelected === next.isSelected &&
    prev.onClick === next.onClick &&
    prev.onTogglePinned === next.onTogglePinned &&
    prev.onToggleSelected === next.onToggleSelected &&
    prev.onProjectAssigned === next.onProjectAssigned &&
    conversationRenderEqual(prev.conversation, next.conversation)
  );
});

/**
 * Hover flyout body for a pinned, project-owned conversation row.
 *
 * Pinning lifts a session out of its project folder into the flat "Pinned"
 * section, dropping the visual project cue the folder provided. Hovering the
 * row surfaces it again: the session title, project name, and optional branch.
 * Mirrors {@link AgentHoverCard}'s Cursor-style placement (right / top-aligned)
 * and the muted, small-icon foreground used elsewhere in the sidebar.
 */
function PinnedProjectFlyoutContent({
  title,
  projectName,
  projectIcon,
  gitBranch,
  hasError,
}: {
  title: string;
  projectName: string;
  projectIcon: string | null;
  gitBranch: string | null;
  hasError: boolean;
}) {
  return (
    <HoverCardContent
      side="right"
      align="start"
      sideOffset={8}
      className="w-64"
      data-testid="pinned-project-flyout"
    >
      {/* Titles have no length cap (server + rename input are unbounded), so
          clamp to 3 wrapped lines to keep the card tidy — full text stays in
          the DOM. */}
      <p className="sidebar-compact-text line-clamp-3 font-medium">{title}</p>
      <p className="mt-1 flex items-center gap-1.5 text-sm text-muted-foreground">
        <ProjectRowIcon icon={projectIcon} />
        <span className="truncate">{projectName}</span>
      </p>
      {gitBranch && (
        <p
          data-testid="pinned-project-flyout-branch"
          className="mt-1 flex items-center gap-1.5 text-sm text-muted-foreground"
        >
          <GitBranchIcon aria-hidden className="size-3.5 shrink-0" />
          <span className="truncate">{gitBranch}</span>
        </p>
      )}
      {hasError && <SessionErrorHint />}
    </HoverCardContent>
  );
}

function ProjectFolderActions({
  projectName,
  projectId,
  onNavigate,
  onSelectTarget,
  actions,
}: {
  projectName: string;
  projectId: string | null;
  onNavigate: (e: MouseEvent<HTMLAnchorElement>) => void;
  onSelectTarget: () => void;
  actions: ProjectFolderMenuActions;
}) {
  return (
    <div className="flex items-center gap-0.5">
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            asChild
            variant="ghost"
            size="icon-xs"
            aria-label={`New session in ${projectName}`}
            data-testid="project-new-session"
            className="hidden text-muted-foreground [@media((hover:hover)_and_(pointer:fine))]:flex"
          >
            <Link
              to={`/?project=${encodeURIComponent(projectName)}`}
              onClick={(e) => {
                e.stopPropagation();
                if (isPlainNavigationClick(e)) onSelectTarget();
                onNavigate(e);
              }}
            >
              <MessageCirclePlusIcon className="size-3.5" data-icon-size="14" />
            </Link>
          </Button>
        </TooltipTrigger>
        <TooltipContent side="bottom">New session in project</TooltipContent>
      </Tooltip>
      <ProjectFolderMenu
        projectName={projectName}
        projectId={projectId}
        onNavigate={onNavigate}
        onSelectTarget={onSelectTarget}
        actions={actions}
      />
    </div>
  );
}

// ── ProjectFolderMenu ─────────────────────────────────────────────────────────

/** The menu body shared by the project-folder kebab and context menu. */
function ProjectFolderMenuItems({
  components: C,
  projectName,
  projectId,
  onNavigate,
  onSelectTarget,
  actions,
  hideNewSessionOnDesktop = false,
}: {
  components: MenuComponents;
  projectName: string;
  projectId: string | null;
  onNavigate: (e: MouseEvent<HTMLAnchorElement>) => void;
  onSelectTarget: () => void;
  actions: ProjectFolderMenuActions;
  hideNewSessionOnDesktop?: boolean;
}) {
  const { onMenuOpen, onMenuClose } = actions;
  const layoutContext = useSidebarLayoutContext();
  const queryClient = useQueryClient();
  useEffect(() => {
    onMenuOpen();
    return onMenuClose;
  }, [onMenuOpen, onMenuClose]);

  // The project is a favorite when a favorites section references its id.
  const isFavorite =
    projectId !== null &&
    (layoutContext?.layout.sections.some(
      (section) =>
        section.kind === "favorites" &&
        (section.items ?? []).some((ref) => ref.type === "project" && ref.id === projectId),
    ) ??
      false);
  const toggleFavorite = () => {
    if (layoutContext === null) return;
    if (isFavorite && projectId !== null) {
      layoutContext.saveLayout((current) =>
        removeFavorite(current, { type: "project", id: projectId }),
      );
      return;
    }
    void addProjectToFavoritesWithPromotion({
      projectId,
      projectName,
      saveLayout: layoutContext.saveLayout,
      queryClient,
    });
  };

  return (
    <>
      <C.Item
        asChild
        data-testid="project-new-session-menu"
        className={
          hideNewSessionOnDesktop ? "[@media((hover:hover)_and_(pointer:fine))]:hidden" : undefined
        }
      >
        <Link
          to={`/?project=${encodeURIComponent(projectName)}`}
          onClick={(e) => {
            e.stopPropagation();
            if (isPlainNavigationClick(e)) onSelectTarget();
            onNavigate(e);
          }}
        >
          <MessageCirclePlusIcon className="size-3.5" />
          New session
        </Link>
      </C.Item>
      <C.Item data-testid="rename-project" onSelect={actions.openRename}>
        <PencilIcon className="size-3.5" />
        Rename project
      </C.Item>
      <C.Item data-testid="project-settings" onSelect={actions.openSettings}>
        <Settings2Icon className="size-3.5" />
        Project settings
      </C.Item>
      {layoutContext !== null && (
        <C.Item data-testid="favorite-project" onSelect={toggleFavorite}>
          {isFavorite ? <PinOffIcon className="size-3.5" /> : <PinIcon className="size-3.5" />}
          {isFavorite ? "Remove from favorites" : "Add to favorites"}
        </C.Item>
      )}
      {actions.ordering && (
        <C.Sub>
          <C.SubTrigger data-testid="move-project">
            <ArrowUpDownIcon className="size-3.5" />
            Move
          </C.SubTrigger>
          <C.SubContent>
            {(["up", "down", "top", "bottom"] as const).map((destination) => (
              <C.Item
                key={destination}
                disabled={
                  actions.ordering!.disabled ||
                  (destination === "up" || destination === "top"
                    ? actions.ordering!.first
                    : actions.ordering!.last)
                }
                onSelect={() => actions.ordering!.move(destination)}
              >
                {destination === "top" || destination === "bottom"
                  ? `Move to ${destination}`
                  : `Move ${destination}`}
              </C.Item>
            ))}
          </C.SubContent>
        </C.Sub>
      )}
      <MoveProjectToSectionMenu components={C} projectId={projectId} projectName={projectName} />
      <C.Separator />
      <C.Item data-testid="delete-project" variant="destructive" onSelect={actions.openDelete}>
        <Trash2Icon className="size-3.5" />
        Delete project
      </C.Item>
    </>
  );
}

interface ProjectFolderMenuActions {
  ordering?: {
    disabled: boolean;
    first: boolean;
    last: boolean;
    move: (destination: "up" | "down" | "top" | "bottom") => void;
  };
  openRename: () => void;
  openSettings: () => void;
  openDelete: () => void;
  onMenuOpen: () => void;
  onMenuClose: () => void;
}

/** Owns the dialogs and mutations shared by both project-folder menus. */
function useProjectFolderMenu(
  projectName: string,
  projectId: string | null,
  icon?: string | null,
): { actions: ProjectFolderMenuActions; dialogs: ReactNode } {
  const [menuOpen, setMenuOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [renameOpen, setRenameOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [emojiOpen, setEmojiOpen] = useState(false);
  const [renameValue, setRenameValue] = useState(projectName);
  const renameInputRef = useRef<HTMLInputElement>(null);
  // The icon staged in the rename modal, committed only on Confirm:
  //   undefined = untouched (show the saved icon), string = a picked emoji,
  //   null = staged removal. Reset to `undefined` each time the modal opens.
  const [pendingIcon, setPendingIcon] = useState<string | null | undefined>(undefined);
  const deleteProject = useDeleteProject();
  const renameProject = useRenameProject();
  const updateConfig = useUpdateProjectConfig();
  // Fetch the full config only while the menu or rename modal is open, so we can
  // merge the icon onto the other stored defaults (host / workspace / agent)
  // without a per-folder request on every sidebar render — and without wiping
  // those defaults on save.
  const { data: iconConfig, isError: iconConfigError } = useProjectConfig(
    menuOpen || renameOpen ? projectId : null,
  );
  // The config PATCH replaces the whole blob, so an ICON save must merge onto a
  // fully-loaded config or it silently wipes the other defaults. "Ready" means
  // the config actually resolved (`!== undefined` — `isLoading` alone is false
  // on a query *error* too, leaving no data to merge onto) — except a
  // label-only folder (`projectId === null`), whose base is legitimately `{}`.
  // This gates only the icon path; renaming the name never needs the config.
  const configReady = projectId === null || iconConfig !== undefined;
  const savedIcon = iconConfig !== undefined ? iconConfig?.icon : icon;
  // What the modal's tile shows: the staged pick when touched, else the saved
  // icon. `null` (staged removal) renders as the empty folder.
  const displayIcon = pendingIcon !== undefined ? pendingIcon : savedIcon;

  const actions = useMemo<ProjectFolderMenuActions>(
    () => ({
      openRename: () => {
        setRenameValue(projectName);
        setPendingIcon(undefined);
        setRenameOpen(true);
      },
      openSettings: () => setSettingsOpen(true),
      openDelete: () => setDeleteOpen(true),
      onMenuOpen: () => setMenuOpen(true),
      onMenuClose: () => setMenuOpen(false),
    }),
    [projectName],
  );

  const dialogs = (
    <>
      <Dialog open={renameOpen} onOpenChange={setRenameOpen}>
        <DialogContent
          onClick={(e) => e.stopPropagation()}
          onOpenAutoFocus={(e) => {
            e.preventDefault();
            renameInputRef.current?.focus();
            renameInputRef.current?.select();
          }}
          // emoji-mart preventDefaults the pointer event, so Radix's own
          // outside-dismissal never fires for clicks elsewhere in the modal.
          // Catch them in the capture phase and close the picker ourselves,
          // unless the pointer is inside the picker or on its trigger tile.
          onPointerDownCapture={(e) => {
            if (!emojiOpen) return;
            const target = e.target as Element;
            if (
              target.closest('[data-slot="popover-content"]') ||
              target.closest('[data-testid="rename-project-icon"]')
            )
              return;
            setEmojiOpen(false);
          }}
        >
          <DialogHeader>
            <DialogTitle>Rename project</DialogTitle>
          </DialogHeader>
          {/* A <form> so Enter in the input submits natively (Radix Dialog
              doesn't wrap children in one) instead of relying on a manual
              key handler + button lookup. */}
          <form
            onSubmit={async (e) => {
              e.preventDefault();
              const newName = renameValue.trim();
              const nameChanged = newName !== "" && newName !== projectName;
              const iconChanged = pendingIcon !== undefined && pendingIcon !== (savedIcon ?? null);
              // Only the icon write needs a loaded config to merge onto; a
              // name-only rename must proceed even if the config fetch failed.
              if (iconChanged && !configReady) return;
              try {
                // Name first: it promotes a label-only folder (creating the
                // first-class row) and reconciles members. Capture the resolved
                // id so the icon write below targets that row — passing the
                // stale render-time `null` would make the config write try to
                // create the project a second time and 409 on the duplicate.
                let targetId = projectId;
                if (nameChanged) {
                  targetId = await renameProject.mutateAsync({
                    id: projectId,
                    oldName: projectName,
                    newName,
                  });
                }
                if (iconChanged) {
                  const next = { ...(iconConfig ?? {}) };
                  if (pendingIcon === null) delete next.icon;
                  else next.icon = pendingIcon;
                  await updateConfig.mutateAsync({
                    id: targetId,
                    name: nameChanged ? newName : projectName,
                    config: next,
                  });
                }
              } catch {
                // Errors surface via the mutation state below. A rename that
                // lands before a failed icon write is already persisted; only
                // the icon needs retrying.
                return;
              }
              setRenameOpen(false);
              setMenuOpen(false);
            }}
          >
            {/* One combined control: emoji tile (left) + name (right) share a
                single border, so it reads as a single input. The tile opens a
                picker popover; the pick is staged and committed on Confirm. */}
            <div className="flex items-stretch overflow-hidden rounded-lg border border-input">
              <Popover open={emojiOpen} onOpenChange={setEmojiOpen}>
                <PopoverTrigger asChild>
                  <button
                    type="button"
                    aria-label="Change project icon"
                    data-testid="rename-project-icon"
                    disabled={!configReady}
                    className={cn(
                      "flex size-[38px] shrink-0 cursor-pointer items-center justify-center outline-none transition-colors disabled:cursor-default disabled:opacity-50",
                      displayIcon ? "bg-muted" : "bg-tag-pink",
                    )}
                  >
                    {displayIcon ? (
                      <span className="text-xl leading-none">{displayIcon}</span>
                    ) : (
                      <SmilePlusIcon className="size-4 text-brand-accent" />
                    )}
                  </button>
                </PopoverTrigger>
                <PopoverContent
                  align="start"
                  // Publish the collision-aware available viewport height (Radix
                  // exposes it as a CSS var), less the optional "Remove icon"
                  // header and capped at the picker's natural size, so the
                  // .emoji-picker-popover rule in index.css shrinks emoji-mart to
                  // fit — it then scrolls its grid internally (nav + search
                  // pinned) instead of clipping on short screens.
                  collisionPadding={8}
                  style={
                    {
                      "--emoji-picker-height": `min(420px, calc(var(--radix-popover-content-available-height) - ${displayIcon ? "38px" : "0px"}))`,
                    } as CSSProperties
                  }
                  className="emoji-picker-popover flex max-h-[var(--radix-popover-content-available-height)] w-auto flex-col overflow-hidden p-0"
                  // The rename Dialog's scroll lock (react-remove-scroll)
                  // preventDefaults wheel events over the picker — it can't see
                  // emoji-mart's scroll region inside shadow DOM. Stop the wheel
                  // from reaching the document-level lock so the grid scrolls.
                  onWheel={(e) => e.stopPropagation()}
                  // Nested in the rename Dialog, emoji-mart's own focus handling
                  // swallows Radix's default outside-pointer dismissal, so a
                  // click elsewhere in the modal wouldn't close the picker.
                  // Close it explicitly on any outside interaction.
                >
                  {displayIcon ? (
                    <div className="shrink-0 border-b p-1">
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        className="w-full justify-start"
                        data-testid="rename-project-remove-icon"
                        onClick={() => {
                          setPendingIcon(null);
                          setEmojiOpen(false);
                        }}
                      >
                        <Trash2Icon className="size-3.5" />
                        Remove icon
                      </Button>
                    </div>
                  ) : null}
                  <EmojiPicker
                    onSelect={(native) => {
                      setPendingIcon(native);
                      setEmojiOpen(false);
                    }}
                  />
                </PopoverContent>
              </Popover>
              <input
                ref={renameInputRef}
                className="w-full bg-transparent px-3 py-2 text-ui outline-none"
                value={renameValue}
                onChange={(e) => setRenameValue(e.target.value)}
              />
            </div>
            {iconConfigError && (
              <p className="text-ui text-destructive" role="alert">
                Couldn&apos;t load this project&apos;s icon settings. You can still rename it;
                changing the icon is unavailable until this loads.
              </p>
            )}
            {(renameProject.isError || updateConfig.isError) && (
              <p className="text-ui text-destructive" role="alert">
                {((renameProject.error ?? updateConfig.error) as Error).message}
              </p>
            )}
            <DialogFooter className="border-t-0 bg-transparent">
              <Button
                type="button"
                variant="ghost"
                onClick={() => setRenameOpen(false)}
                disabled={renameProject.isPending || updateConfig.isPending}
              >
                Cancel
              </Button>
              <Button
                type="submit"
                data-testid="rename-project-confirm"
                loading={renameProject.isPending || updateConfig.isPending}
                disabled={renameValue.trim() === ""}
                componentId="sidebar.project.rename"
              >
                Confirm
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
      {/* Mount the settings dialog only while open — like the row share modal,
          it runs a full hook tree even when closed. */}
      {settingsOpen && (
        <ProjectSettingsDialog
          open={settingsOpen}
          onOpenChange={(o) => {
            setSettingsOpen(o);
            if (!o) setMenuOpen(false);
          }}
          projectId={projectId}
          projectName={projectName}
        />
      )}
      <Dialog open={deleteOpen} onOpenChange={setDeleteOpen}>
        <DialogContent onClick={(e) => e.stopPropagation()}>
          <DialogHeader>
            <DialogTitle>Delete project?</DialogTitle>
            <DialogDescription>
              This deletes the project{" "}
              <span className="rounded bg-muted px-1 py-0.5 font-mono text-[0.95em] break-all">
                {projectName}
              </span>{" "}
              and archives <span className="font-medium">all of its sessions</span>. Their history
              is kept. You can find and restore them anytime from Settings.
            </DialogDescription>
          </DialogHeader>
          {deleteProject.isError && (
            <p className="text-ui text-destructive" role="alert">
              Some sessions couldn't be archived (you may not own them); the rest were archived.
            </p>
          )}
          <DialogFooter className="border-t-0 bg-transparent">
            <Button
              type="button"
              variant="ghost"
              onClick={() => setDeleteOpen(false)}
              disabled={deleteProject.isPending}
            >
              Cancel
            </Button>
            <Button
              type="button"
              variant="destructive"
              loading={deleteProject.isPending}
              onClick={() => {
                deleteProject.mutate(
                  { id: projectId, name: projectName },
                  {
                    onSuccess: () => {
                      setDeleteOpen(false);
                      setMenuOpen(false);
                    },
                  },
                );
              }}
            >
              Delete project
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );

  return { actions, dialogs };
}

function ProjectFolderMenu({
  projectName,
  projectId,
  onNavigate,
  onSelectTarget,
  actions,
}: {
  projectName: string;
  projectId: string | null;
  onNavigate: (e: MouseEvent<HTMLAnchorElement>) => void;
  onSelectTarget: () => void;
  actions: ProjectFolderMenuActions;
}) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          aria-label={`Project actions for ${projectName}`}
          data-testid="project-actions"
          className="text-muted-foreground"
          onClick={(e) => e.stopPropagation()}
        >
          <MoreHorizontalIcon className="size-3.5" data-icon-size="14" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="min-w-40">
        <ProjectFolderMenuItems
          components={dropdownBundle}
          hideNewSessionOnDesktop
          projectName={projectName}
          projectId={projectId}
          onNavigate={onNavigate}
          onSelectTarget={onSelectTarget}
          actions={actions}
        />
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

// ── ProjectPickerMenu ─────────────────────────────────────────────────────────

/**
 * Project picker rendered as the body of a {@link DropdownMenuSubContent}.
 *
 * Lives inside the kebab menu's submenu flyout rather than a separate popover —
 * that avoids the open/dismiss race that made a standalone popover flash open
 * and vanish. The search / new-project inputs stop key events from bubbling so
 * the menu's built-in typeahead and arrow-key navigation don't hijack typing.
 */
function ProjectPickerMenu({
  components: C,
  currentProject,
  onSelect,
}: {
  components: MenuComponents;
  currentProject: string | null;
  onSelect: (project: string) => void;
}) {
  const { data: projects = [] } = useProjects();
  const [search, setSearch] = useState("");

  const trimmed = search.trim();
  const filtered = trimmed
    ? projects.filter((p) => p.name.toLowerCase().includes(trimmed.toLowerCase()))
    : projects;
  // Offer create only when the typed name isn't already an exact project.
  const canCreate =
    trimmed.length > 0 && !projects.some((p) => p.name.toLowerCase() === trimmed.toLowerCase());
  const currentProjectIcon = projects.find((p) => p.name === currentProject)?.icon;

  // Keep keystrokes inside the inputs from reaching the menu's typeahead /
  // navigation handlers (which would otherwise steal letters and arrows).
  const swallowKeys = (e: KeyboardEvent<HTMLInputElement>) => e.stopPropagation();

  return (
    <>
      {/* Combobox-style search: a leading magnifier inside a borderless input,
          with a divider beneath separating it from the results. */}
      <div className="flex items-center gap-2 border-b px-2 py-1.5">
        <SearchIcon className="size-3.5 shrink-0 text-muted-foreground" />
        <input
          className="w-full bg-transparent text-sm outline-none placeholder:text-muted-foreground"
          placeholder="Search or create project"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          onKeyDown={swallowKeys}
        />
      </div>
      <div className="max-h-48 overflow-y-auto">
        {filtered.map((p) => (
          <C.Item
            key={p.name}
            className="px-2 py-1"
            textValue={p.name}
            onSelect={() => onSelect(p.name)}
          >
            <ProjectRowIcon icon={p.icon} />
            <span className="flex-1 truncate text-left">{p.name}</span>
            {currentProject === p.name && (
              <CheckMarkIcon className="size-3.5 shrink-0 text-primary" />
            )}
          </C.Item>
        ))}
        {filtered.length === 0 && !canCreate && (
          <p className="px-2 py-1.5 text-sm text-muted-foreground">No projects yet.</p>
        )}
      </div>
      {canCreate && (
        <div className="border-t pt-1">
          <C.Item className="px-2 py-1" onSelect={() => onSelect(trimmed)}>
            <PlusIcon className="size-3.5 shrink-0 text-muted-foreground" />
            Create{" "}
            <span className="truncate rounded bg-muted px-1 py-0.5 font-mono text-[0.95em]">
              {trimmed}
            </span>
          </C.Item>
        </div>
      )}
      {currentProject && (
        <div className="border-t pt-1">
          <C.Item
            className="px-2 py-1"
            textValue={`Remove from ${currentProject}`}
            onSelect={() => onSelect("")}
          >
            <ProjectRowIcon icon={currentProjectIcon} />
            Remove from{" "}
            <span className="rounded bg-muted px-1 py-0.5 font-mono text-[0.95em]">
              {currentProject}
            </span>
          </C.Item>
        </div>
      )}
    </>
  );
}

// ── ConversationEditRow ──────────────────────────────────────────────────────

interface ConversationEditRowProps {
  initialTitle: string;
  onCommit: (title: string) => void;
  onCancel: () => void;
}

/**
 * Inline-edit shell for a conversation row.
 *
 * Auto-focuses on mount and selects the whole title so the user can
 * start typing to replace. Enter commits, Escape cancels, blur
 * commits — matches the spec's "lose focus or press enter" wording.
 * The blur-commits-on-Escape case is avoided by clearing the value
 * with the dedicated cancel handler before blur fires.
 */
function ConversationEditRow({ initialTitle, onCommit, onCancel }: ConversationEditRowProps) {
  const [value, setValue] = useState(initialTitle);
  const inputRef = useRef<HTMLInputElement>(null);
  // Set when the user explicitly cancels (Escape or X click); blur
  // checks this so we don't double-fire onCommit with the unedited
  // value when the input loses focus as part of unmounting.
  const cancelledRef = useRef(false);
  // Tracks an active IME composition (e.g. Japanese conversion) so the Enter
  // that confirms a candidate doesn't commit the rename. Mirrors the chat
  // composer guard (#132/#243).
  const isComposingRef = useRef(false);

  useEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
  }, []);

  function handleKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (isImeCompositionKeyEvent(e, isComposingRef.current)) return;
    if (e.key === "Enter") {
      e.preventDefault();
      onCommit(value);
      return;
    }
    if (e.key === "Escape") {
      e.preventDefault();
      cancelledRef.current = true;
      onCancel();
    }
  }

  function handleBlur() {
    if (cancelledRef.current) return;
    onCommit(value);
  }

  return (
    // Match the interactive row's responsive box metrics so entering edit mode
    // doesn't shift the list. pl-1 + the input's px-1 align with row titles.
    <div className="sidebar-compact-text flex h-8 items-center gap-1 rounded-[var(--radius-otto-sm)] bg-muted pr-1 pl-1 md:h-7">
      <input
        ref={inputRef}
        type="text"
        maxLength={USER_SESSION_TITLE_MAX_CHARS}
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onCompositionStart={() => {
          isComposingRef.current = true;
        }}
        onCompositionEnd={() => {
          isComposingRef.current = false;
        }}
        onKeyDown={handleKeyDown}
        onBlur={handleBlur}
        data-testid="rename-conversation-input"
        className="min-w-0 flex-1 truncate rounded bg-transparent px-1 py-0.5 outline-none md:select-text"
      />
      <Button
        type="button"
        variant="ghost"
        size="icon-xs"
        aria-label="Save rename"
        onMouseDown={(e) => {
          // Prevent the input's blur from firing before the commit.
          e.preventDefault();
        }}
        onClick={() => onCommit(value)}
      >
        <CheckIcon className="size-3.5" />
      </Button>
      <Button
        type="button"
        variant="ghost"
        size="icon-xs"
        aria-label="Cancel rename"
        onMouseDown={(e) => e.preventDefault()}
        onClick={() => {
          cancelledRef.current = true;
          onCancel();
        }}
      >
        <XIcon className="size-3.5" />
      </Button>
    </div>
  );
}

function BulkActionBar({
  selectedIds,
  allConversations,
  onDeselectAll,
  onExit,
  onProjectAssigned,
}: {
  selectedIds: Set<string>;
  allConversations: Conversation[];
  onDeselectAll: () => void;
  onExit: () => void;
  onProjectAssigned?: (projectName: string) => void;
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { conversationId: activeId } = useParams<{ conversationId: string }>();
  const bulkArchive = useBulkArchiveConversations();
  const archiveWorktreePrompt = useArchiveWorktreePrompt();
  const bulkDelete = useBulkDeleteConversations();
  const bulkMove = useBulkMoveToProject();
  const { data: projects = [] } = useProjects();
  const viewerId = useViewerId();

  const selectedConversations = useMemo(
    () => allConversations.filter((c) => selectedIds.has(c.id)),
    [allConversations, selectedIds],
  );

  // Subscribed so the read-state action below flips read↔unread the moment
  // the mirror is written (e.g. a background turn lighting a selected row's
  // dot while the bar is open). Computed per render — the selection is small.
  useUnseenTick();
  // Mirrors the row dot's condition (active-row suppression included), so the
  // offered direction always matches the dots the user sees.
  const unreadSelected = selectedConversations.filter(
    (c) =>
      isConversationUnseen(c.id, c.updated_at, c.status) &&
      (c.id !== activeId || isExplicitlyUnread(c.id)),
  );

  const ownedSelected = useMemo(
    () => selectedConversations.filter((c) => isOwnedByViewer(c, viewerId)),
    [selectedConversations, viewerId],
  );

  const archivedSelected = useMemo(
    () => ownedSelected.filter((c) => c.archived === true),
    [ownedSelected],
  );

  const nonArchivedSelected = useMemo(
    () => ownedSelected.filter((c) => c.archived !== true),
    [ownedSelected],
  );

  const allSelectedSameArchiveGroup =
    ownedSelected.length > 0 && (archivedSelected.length === 0 || nonArchivedSelected.length === 0);

  const count = selectedIds.size;
  const isBusy = bulkArchive.isPending || bulkDelete.isPending || bulkMove.isPending;

  // Delete acts only on owned rows, so surface that count on the control when it
  // differs from "N selected" — otherwise a mixed-ownership selection (reachable
  // in projects scope, where a folder can hold others' sessions) reads
  // "3 selected" while Delete hits fewer. Used for both the tooltip (visual) and
  // the aria-label (assistive tech). Archive needs no such hint: its gate
  // (`allSelectedSameArchiveGroup`) only enables it when every owned row shares
  // one archive state, and archived rows never appear in a selectable section.
  const deleteLabel =
    ownedSelected.length > 0 && ownedSelected.length !== count
      ? `Delete ${ownedSelected.length}`
      : "Delete";
  const deleteCommentsLine = bulkCommentsDeleteLine(
    ownedSelected.reduce((total, c) => total + (c.comments_count ?? 0), 0),
  );

  const [confirmDeleteOpen, setConfirmDeleteOpen] = useState(false);
  const [moveSearch, setMoveSearch] = useState("");
  // Worktree sessions among the selection each carry one local git branch
  // (git_branch); the delete modal lists them so each branch can be opted
  // into cleanup individually. `branchesToDelete` holds the session ids whose
  // branch the user ticked — default empty (opt-in, matching single-session
  // delete, since branch deletion is irreversible).
  const [branchesToDelete, setBranchesToDelete] = useState<Set<string>>(new Set());

  const worktreeSelected = useMemo(
    () => ownedSelected.filter((c) => c.git_branch),
    [ownedSelected],
  );
  const allBranchesSelected =
    worktreeSelected.length > 0 && worktreeSelected.every((c) => branchesToDelete.has(c.id));
  // Drives the header checkbox's indeterminate ([-]) state: some but not all
  // branches ticked.
  const someBranchesSelected =
    !allBranchesSelected && worktreeSelected.some((c) => branchesToDelete.has(c.id));

  function toggleBranch(id: string, checked: boolean) {
    setBranchesToDelete((prev) => {
      const next = new Set(prev);
      if (checked) next.add(id);
      else next.delete(id);
      return next;
    });
  }

  function toggleAllBranches() {
    setBranchesToDelete(
      allBranchesSelected ? new Set() : new Set(worktreeSelected.map((c) => c.id)),
    );
  }

  function handleMoveToProject(project: string) {
    const ids = ownedSelected.map((c) => c.id);
    if (ids.length === 0) return;
    bulkMove.mutate(
      { ids, project },
      {
        onSuccess: () => {
          if (project) onProjectAssigned?.(project);
          onExit();
        },
      },
    );
  }

  // Read state is per-viewer (not ownership-gated, matching the row menu),
  // so both actions apply to every selected row. The store writes are
  // synchronous with a fire-and-forget server sync, so the bar can exit
  // immediately, matching the other bulk actions.
  function handleMarkRead() {
    for (const c of unreadSelected) markConversationRead(c.id, c.updated_at);
    onExit();
  }

  function handleMarkUnread() {
    for (const c of selectedConversations) markConversationUnread(c.id, c.updated_at);
    onExit();
  }

  function handleArchive() {
    if (nonArchivedSelected.length === 0) return;
    const toArchive = nonArchivedSelected;
    archiveWorktreePrompt.requestArchive(toArchive, (deleteWorktreeIds) =>
      archiveNow(toArchive, deleteWorktreeIds),
    );
  }

  function archiveNow(toArchive: Conversation[], deleteWorktreeIds: ReadonlySet<string>) {
    // The rows leave the sidebar optimistically (useBulkArchiveConversations
    // flips their cached `archived` flag in onMutate), so this bar unmounts
    // with the selection. Navigate and deselect NOW rather than in a
    // mutate-level callback — a callback on the unmounted observer never fires,
    // and it would carry a stale `activeId` that could jump the user off a
    // session they switched to meanwhile. Mirrors handleDelete.
    if (activeId && toArchive.some((c) => c.id === activeId)) navigate("/", { replace: true });
    onDeselectAll();
    bulkArchive.mutate({ ids: toArchive.map((c) => c.id), archived: true, deleteWorktreeIds });
    // Offer Undo for the whole batch. Fire now, before this bar unmounts with
    // the cleared selection; the toast is driven by module state + the
    // app-level Toaster, so it outlives this component.
    showArchiveUndoToast(queryClient, toArchive, navigate);
  }

  function handleUnarchive() {
    if (archivedSelected.length === 0) return;
    onDeselectAll();
    bulkArchive.mutate({ ids: archivedSelected.map((c) => c.id), archived: false });
  }

  function handleDelete() {
    const ids = ownedSelected.map((c) => c.id);
    if (ids.length === 0) return;
    setConfirmDeleteOpen(false);
    // The rows leave the sidebar optimistically, so the selection is already
    // meaningless and the chat surface would be sitting on an id that's about
    // to 404. Both are settled here rather than in a mutate-level callback:
    // this bar unmounts with the selection, and callbacks on an unmounted
    // observer never fire.
    if (activeId && ids.includes(activeId)) navigate("/", { replace: true });
    onDeselectAll();
    bulkDelete.mutate({ ids, deleteBranchIds: branchesToDelete });
  }

  return (
    <>
      {archiveWorktreePrompt.dialog}
      <div className="mt-1 mb-1 flex flex-col gap-1.5">
        <div className="flex items-center gap-2 rounded-lg border border-border bg-transparent p-1.5">
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                type="button"
                variant="ghost"
                size="icon-xs"
                className="shrink-0"
                aria-label="Exit selection mode"
                data-testid="toggle-selection-mode"
                onClick={onExit}
              >
                <XIcon className="size-3.5" />
              </Button>
            </TooltipTrigger>
            <TooltipContent side="bottom">Exit selection</TooltipContent>
          </Tooltip>
          <span className="sidebar-compact-text shrink-0 whitespace-nowrap font-medium">
            {count} selected
          </span>

          <div className="ml-auto flex items-center gap-0.5">
            {unreadSelected.length > 0 ? (
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon-xs"
                    className="shrink-0"
                    disabled={isBusy}
                    onClick={handleMarkRead}
                    aria-label="Mark selected as read"
                    data-testid="bulk-mark-read"
                  >
                    <MailOpenIcon className="size-3.5" />
                  </Button>
                </TooltipTrigger>
                <TooltipContent side="bottom">Mark as read</TooltipContent>
              </Tooltip>
            ) : (
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon-xs"
                    className="shrink-0"
                    disabled={isBusy || count === 0}
                    onClick={handleMarkUnread}
                    aria-label="Mark selected as unread"
                    data-testid="bulk-mark-unread"
                  >
                    <MailIcon className="size-3.5" />
                  </Button>
                </TooltipTrigger>
                <TooltipContent side="bottom">Mark as unread</TooltipContent>
              </Tooltip>
            )}
            {!(allSelectedSameArchiveGroup && archivedSelected.length > 0) && (
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon-xs"
                    className="shrink-0"
                    disabled={isBusy || nonArchivedSelected.length === 0}
                    onClick={handleArchive}
                    aria-label="Archive selected"
                    data-testid="bulk-archive"
                  >
                    {bulkArchive.isPending ? (
                      <Loader2Icon className="size-3.5 animate-spin" />
                    ) : (
                      <ArchiveIcon className="size-3.5" />
                    )}
                  </Button>
                </TooltipTrigger>
                <TooltipContent side="bottom">Archive</TooltipContent>
              </Tooltip>
            )}
            {allSelectedSameArchiveGroup && archivedSelected.length > 0 && (
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon-xs"
                    className="shrink-0"
                    disabled={isBusy}
                    onClick={handleUnarchive}
                    aria-label="Unarchive selected"
                    data-testid="bulk-unarchive"
                  >
                    {bulkArchive.isPending ? (
                      <Loader2Icon className="size-3.5 animate-spin" />
                    ) : (
                      <ArchiveRestoreIcon className="size-3.5" />
                    )}
                  </Button>
                </TooltipTrigger>
                <TooltipContent side="bottom">Unarchive</TooltipContent>
              </Tooltip>
            )}
            <DropdownMenu
              onOpenChange={(open) => {
                if (!open) setMoveSearch("");
              }}
            >
              <Tooltip disableHoverableContent>
                <TooltipTrigger asChild>
                  {/* Separate nodes keep the Radix tooltip and menu trigger states independent. */}
                  <span className="inline-flex shrink-0">
                    <DropdownMenuTrigger asChild>
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon-xs"
                        disabled={isBusy || ownedSelected.length === 0}
                        aria-label="Move to project"
                        data-testid="bulk-move-to-project"
                      >
                        {bulkMove.isPending ? (
                          <Loader2Icon className="size-3.5 animate-spin" />
                        ) : (
                          <FolderInputIcon className="size-3.5" />
                        )}
                      </Button>
                    </DropdownMenuTrigger>
                  </span>
                </TooltipTrigger>
                <TooltipContent side="bottom" data-noninteractive-tooltip>
                  Move to project
                </TooltipContent>
              </Tooltip>
              <DropdownMenuContent align="end" className="w-52">
                <div className="flex items-center gap-2 border-b px-2 py-1.5">
                  <SearchIcon className="size-3.5 shrink-0 text-muted-foreground" />
                  <input
                    className="w-full bg-transparent text-sm outline-none placeholder:text-muted-foreground"
                    placeholder="Search projects"
                    value={moveSearch}
                    onChange={(e) => setMoveSearch(e.target.value)}
                    onKeyDown={(e) => e.stopPropagation()}
                  />
                </div>
                <div className="max-h-48 overflow-y-auto">
                  {(moveSearch
                    ? projects.filter((p) =>
                        p.name.toLowerCase().includes(moveSearch.toLowerCase()),
                      )
                    : projects
                  ).map((p) => (
                    <DropdownMenuItem
                      key={p.name}
                      className="px-2 py-1"
                      onSelect={() => handleMoveToProject(p.name)}
                    >
                      <span className="flex-1 truncate text-left">{p.name}</span>
                    </DropdownMenuItem>
                  ))}
                  {projects.length === 0 && (
                    <p className="px-2 py-1.5 text-sm text-muted-foreground">No projects yet.</p>
                  )}
                </div>
              </DropdownMenuContent>
            </DropdownMenu>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-xs"
                  className={cn("shrink-0", ownedSelected.length > 0 && "text-destructive")}
                  disabled={isBusy || ownedSelected.length === 0}
                  onClick={() => setConfirmDeleteOpen(true)}
                  aria-label={deleteLabel}
                  data-testid="bulk-delete"
                >
                  {bulkDelete.isPending ? (
                    <Loader2Icon className="size-3.5 animate-spin" />
                  ) : (
                    <Trash2Icon className="size-3.5" />
                  )}
                </Button>
              </TooltipTrigger>
              <TooltipContent side="bottom">{deleteLabel}</TooltipContent>
            </Tooltip>
          </div>
        </div>

        {(bulkArchive.isError || bulkDelete.isError || bulkMove.isError) && (
          <p className="text-sm text-destructive" role="alert">
            Some actions failed. Retry or dismiss.
          </p>
        )}
      </div>

      <Dialog
        open={confirmDeleteOpen}
        onOpenChange={(open) => {
          setConfirmDeleteOpen(open);
          // Reset the branch selection on close so it doesn't carry over to
          // the next delete.
          if (!open) setBranchesToDelete(new Set());
        }}
      >
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>Delete {ownedSelected.length} session(s)?</DialogTitle>
            <DialogDescription>
              This will permanently delete the selected sessions and all their history. This cannot
              be undone.
            </DialogDescription>
          </DialogHeader>
          {deleteCommentsLine !== null && (
            <p className="text-sm text-muted-foreground">{deleteCommentsLine}</p>
          )}
          {worktreeSelected.length > 0 && (
            <div className="flex flex-col gap-2 rounded-md border border-destructive/40 bg-destructive/5 p-3">
              <p className="text-sm text-muted-foreground">
                Optionally delete the local git branches for these worktree sessions. These actions
                are <span className="font-semibold text-destructive">irreversible</span>.
              </p>
              <div className="max-h-56 overflow-y-auto">
                <table className="w-full border-collapse text-left text-ui">
                  <thead>
                    <tr className="border-b border-destructive/20 text-sm text-muted-foreground">
                      <th scope="col" className="w-8 py-1.5 pr-2 font-medium">
                        <input
                          type="checkbox"
                          ref={(el) => {
                            if (el) el.indeterminate = someBranchesSelected;
                          }}
                          checked={allBranchesSelected}
                          onChange={toggleAllBranches}
                          aria-label="Select all branches"
                          data-testid="bulk-delete-branch-toggle-all"
                          className="mt-1 size-4 shrink-0 accent-destructive"
                        />
                      </th>
                      <th scope="col" className="py-1.5 pr-3 font-medium">
                        Branch
                      </th>
                      <th scope="col" className="py-1.5 font-medium">
                        Session
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {worktreeSelected.map((c) => (
                      <tr key={c.id} className="align-top">
                        <td className="py-2 pr-2">
                          <input
                            type="checkbox"
                            data-testid="bulk-delete-branch-checkbox"
                            checked={branchesToDelete.has(c.id)}
                            onChange={(e) => toggleBranch(c.id, e.target.checked)}
                            aria-label={`Delete branch ${c.git_branch}`}
                            className="mt-0.5 size-4 shrink-0 cursor-pointer accent-destructive"
                          />
                        </td>
                        <td className="py-2 pr-3">
                          <span className="flex items-start gap-1.5">
                            <GitBranchIcon className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" />
                            <span className="min-w-0">
                              <code className="break-all rounded bg-muted px-1 py-0.5 text-sm">
                                {c.git_branch}
                              </code>
                              {effectiveWorktree(c) !== null && (
                                <span
                                  data-testid="bulk-delete-branch-worktree-path"
                                  className="mt-0.5 block break-all font-mono text-xs text-muted-foreground"
                                >
                                  {effectiveWorktree(c)}
                                </span>
                              )}
                            </span>
                          </span>
                        </td>
                        <td className="py-2 text-sm text-muted-foreground">
                          <span className="line-clamp-2 break-all">
                            {conversationDisplayLabel(c)}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
          <DialogFooter className="border-t-0 bg-transparent">
            <Button
              type="button"
              variant="ghost"
              onClick={() => {
                setConfirmDeleteOpen(false);
                setBranchesToDelete(new Set());
              }}
              disabled={bulkDelete.isPending}
            >
              Cancel
            </Button>
            <Button
              type="button"
              variant="destructive"
              onClick={handleDelete}
              disabled={bulkDelete.isPending}
            >
              Delete {ownedSelected.length} session(s)
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

/**
 * Returns true on mobile viewports (below the `md` breakpoint of
 * 768px). Used to gate the auto-close-on-navigation behavior — on
 * mobile the sidebar is a full-screen overlay so dismissing on action
 * is what reveals the destination; on desktop the sidebar pushes content
 * aside and staying open is more useful.
 *
 * SSR-safe (returns false when window is undefined).
 */
export function isMobileViewport(): boolean {
  if (typeof window === "undefined") return false;
  return !window.matchMedia("(min-width: 768px)").matches;
}

// Project folders default to collapsed, so the persisted set is the EXPANDED
// names (empty by default = every project starts collapsed).
function readExpandedProjectSections(): string[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(EXPANDED_PROJECT_SECTIONS_STORAGE_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((value): value is string => typeof value === "string");
  } catch {
    return [];
  }
}

function writeExpandedProjectSections(names: string[]) {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(EXPANDED_PROJECT_SECTIONS_STORAGE_KEY, JSON.stringify(names));
  } catch {
    // Same as collapse state — a lost local preference is harmless.
  }
}
