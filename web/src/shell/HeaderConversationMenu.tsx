import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import {
  ArchiveIcon,
  ArchiveRestoreIcon,
  BellIcon,
  BellOffIcon,
  ChevronLeftIcon,
  CircleStopIcon,
  CopyIcon,
  DownloadIcon,
  FileTextIcon,
  EllipsisIcon,
  FolderInputIcon,
  GitBranchIcon,
  GitForkIcon,
  InfoIcon,
  MailIcon,
  MailOpenIcon,
  MessageCircleIcon,
  PencilIcon,
  PinIcon,
  PinOffIcon,
  PanelRightOpenIcon,
  PlayIcon,
  PlusIcon,
  ShareIcon,
  Trash2Icon,
} from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import {
  ARIA_MOD_KEY,
  ARCHIVE_SESSION_KEYS,
  FORK_SESSION_KEYS,
  CompactShortcutKeys,
  PIN_SESSION_KEYS,
} from "@/components/KeyboardShortcut";
import { SessionActionMenuItem } from "@/components/SessionActionMenuItem";
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
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useIsMutating, useMutation, useQueryClient } from "@tanstack/react-query";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isIndentedSibling } from "@/lib/sessionFamilies";
import { PIN_WRITE_MUTATION_KEY } from "@/lib/sessionListCache";
import { exportSessionTranscript, retrySession } from "@/lib/sessionsApi";
import { copyText } from "@/lib/clipboard";
import { isSessionStoppable } from "@/lib/sessionStop";
import { nativeCodingAgentForSubagentWrapper, WRAPPER_LABEL_KEY } from "@/lib/nativeCodingAgents";
import { useRunnerHealthRegistration, useSessionRunnerOnline } from "@/hooks/RunnerHealthProvider";
import { unhandledCommentsDeleteLine } from "@/lib/comments";
import { useComments } from "@/hooks/useComments";
import { effectiveWorktree } from "@/lib/types";
import { triggerBrowserDownload } from "@/hooks/useFileContent";
import {
  PINNED_LABEL_KEY,
  type Conversation,
  useArchiveConversation,
  useMoveToProject,
  useRenameConversation,
  useStopAndDeleteConversation,
  useStopSession,
  useTogglePinnedConversation,
} from "@/hooks/useConversations";
import { ProjectPicker } from "./ProjectPicker";
import {
  markConversationRead,
  markConversationUnread,
  useConversationReadState,
} from "@/hooks/useUnseenConversations";
import { useSoundAlertPreferences } from "@/hooks/useSoundAlertPreferences";
import { readSoundAlertPreferences, writeSoundAlertPreferences } from "@/lib/soundAlertPreferences";
import { useChatStore } from "@/store/chatStore";
import { useOmnigentAnalytics } from "@/lib/analytics";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { useDesktopCommand } from "@/lib/desktopCommands";
import { supportsDesktopCommands } from "@/lib/nativeBridge";
import { useIsEmbedded } from "@/lib/embedded";
import { getEmbedRoot } from "@/lib/host";
import { hasCommandModifier, isFocusInDialogOrMenu } from "@/lib/hotkeys";
import { useLocation, useNavigate } from "@/lib/routing";
import { USER_SESSION_TITLE_MAX_CHARS } from "@/lib/sessionTitles";
import { showArchiveUndoToast } from "./archiveUndoToast";
import { unpinWithUndo } from "./unpinUndoToast";
import { useArchiveWorktreePrompt } from "./ArchiveWorktreeDialog";
import type { ArchiveWorktreeMode } from "@/lib/archiveWorktreePreferences";
import { WorktreeDetailsDialog } from "@/components/WorktreeDetailsDialog";
import { cn } from "@/lib/utils";
import { MOBILE_GLASS_SURFACE } from "./mobileGlass";
import { conversationDisplayLabel } from "./sidebarNav";
import {
  MOBILE_ASSISTANT_ACTION_EVENT,
  type MobileAssistantDirectAction,
} from "@/lib/mobileAssistantPreferences";
import { MobileHeaderButton } from "./MobileHeaderButton";
import { SessionMenuHeader } from "./SessionMenuHeader";

interface HeaderConversationMenuProps {
  conversation: Conversation;
  workingDirectory?: string | null;
  currentProject: string | null;
  canShare: boolean;
  canFork: boolean;
  forkDisabledReason?: string;
  shareDisabled?: boolean;
  shareDisabledReason?: string;
  onShare: () => void;
  onFork: () => void;
  hasAgentInfo?: boolean;
  onAgentInfo?: () => void;
  /**
   * Whether the "Peer messages" entry should render — the server's
   * `session_peer_messaging` feature is on.
   */
  showPeerMessages?: boolean;
  /** Open the peer-messages held panel (`PeerHeldPanel`) for this session. */
  onPeerMessages?: () => void;
  /**
   * Mobile Chat/Terminal view switch (ViewModeMenuItems) — leads the menu on
   * terminal-first sessions and carries its own trailing separator. `null`
   * otherwise.
   */
  viewItems?: ReactNode;
  /** Mobile workspace-rail entries (Files · Agents · Shells · Logs). */
  workspaceItems?: ReactNode;
  trigger?: ReactNode;
  initiallyOpen?: boolean;
  canManage?: boolean;
  surface?: "header" | "row";
}

export function HeaderConversationMenu({
  conversation,
  workingDirectory = conversation.workspace,
  currentProject,
  canShare,
  canFork,
  forkDisabledReason,
  shareDisabled = false,
  shareDisabledReason,
  onShare,
  onFork,
  hasAgentInfo = false,
  onAgentInfo,
  showPeerMessages = false,
  onPeerMessages,
  viewItems = null,
  workspaceItems = null,
  trigger,
  initiallyOpen = false,
  canManage = true,
  surface = "header",
}: HeaderConversationMenuProps) {
  const navigate = useNavigate();
  const location = useLocation();
  const info = useServerInfo();
  const queryClient = useQueryClient();
  const isMobile = useIsMobileViewport();
  const isEmbedded = useIsEmbedded();
  const { trackClick } = useOmnigentAnalytics();
  const togglePinned = useTogglePinnedConversation();
  // Pin writes don't overlap, so Pin/Unpin is disabled while one is saving.
  const pinSaving = useIsMutating({ mutationKey: PIN_WRITE_MUTATION_KEY }) > 0;
  const rename = useRenameConversation();
  const moveToProject = useMoveToProject();
  const archive = useArchiveConversation();
  const archiveWorktreePrompt = useArchiveWorktreePrompt(conversation.id);
  const deleteConversation = useStopAndDeleteConversation();
  const stopSession = useStopSession();
  const { account: soundAccount } = useSoundAlertPreferences();
  const readState = useConversationReadState(
    conversation.id,
    conversation.updated_at,
    conversation.status,
  );
  const [menuOpen, setMenuOpen] = useState(initiallyOpen);
  const healthSessions = useMemo(
    () => (menuOpen ? [{ id: conversation.id }] : []),
    [menuOpen, conversation.id],
  );
  useRunnerHealthRegistration(healthSessions);
  const [projectPickerOpen, setProjectPickerOpen] = useState(false);
  const [renameOpen, setRenameOpen] = useState(false);
  const [renameTitle, setRenameTitle] = useState(conversation.title ?? "");
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [stopOpen, setStopOpen] = useState(false);
  const [deleteBranch, setDeleteBranch] = useState(false);
  // Unhandled comments die with the session; fetch them when the delete
  // confirmation opens so the dialog can name the count.
  const { data: deleteComments } = useComments(deleteOpen ? conversation.id : undefined);
  const deleteCommentsLine = unhandledCommentsDeleteLine(deleteComments ?? []);
  const [archivePending, setArchivePending] = useState(false);
  const [worktreeDetailsOpen, setWorktreeDetailsOpen] = useState(false);
  const mountedRef = useRef(false);
  const archiveRequestRef = useRef(false);
  const currentConversationIdRef = useRef(conversation.id);
  const currentLocationRef = useRef(location);
  const previousConversationId = useRef(conversation.id);
  const isPinned = conversation.labels?.[PINNED_LABEL_KEY] != null;
  const isArchived = conversation.archived === true;
  const isChild = conversation.parent_session_id != null;
  const runnerOnline = useSessionRunnerOnline(conversation.id);
  const canStop =
    !isArchived &&
    runnerOnline !== false &&
    (isChild ||
      isSessionStoppable({
        labels: conversation.labels,
        hostId: conversation.host_id,
        runnerId: conversation.runner_id,
      }));
  const canResume =
    !isArchived &&
    Boolean(conversation.host_id) &&
    runnerOnline === false &&
    !nativeCodingAgentForSubagentWrapper(conversation.labels[WRAPPER_LABEL_KEY]);
  const resume = useMutation({
    mutationFn: async () => {
      const result = await retrySession(conversation.id);
      if (!result.recovered) throw new Error("No recovery was performed; refresh and try again");
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["session", conversation.id] });
      navigate(`/c/${conversation.id}?view=terminal`);
    },
    onError: (error) => toast.error(`Couldn't resume the session: ${error.message}`),
  });
  const soundsMuted = soundAccount.mutedSessionIds.includes(conversation.id);
  const canAddAgent = !isArchived && info !== "loading" && info.sibling_sessions;
  const addAgentPath = `/?sibling=${encodeURIComponent(conversation.id)}`;
  const label = conversationDisplayLabel(conversation);
  // Mobile taps need a bigger target than the dense desktop row.
  const itemClass = cn("mobile-touch-target", isMobile && "gap-2.5 px-2.5 py-2");
  // Share and Agent info reach this menu on mobile from the header's legacy
  // Share · Agent info menu, which reported them under these ids. Keep
  // emitting the same ones so the metric series stays continuous, and only on
  // mobile — the desktop kebab is a different surface.
  const trackMobile = (componentId: string) => {
    if (isMobile) trackClick(componentId, "button");
  };
  const gitBranch = conversation.git_branch ?? null;
  // The directory a branch cleanup removes: the recorded worktree, else the
  // session's launch directory. Shown in the delete offer; the gate above
  // stays the recorded branch.
  const branchWorktree = gitBranch !== null ? effectiveWorktree(conversation) : null;

  currentConversationIdRef.current = conversation.id;
  currentLocationRef.current = location;

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    if (!renameOpen) setRenameTitle(conversation.title ?? "");
  }, [conversation.title, renameOpen]);

  useEffect(() => {
    if (!isMobile || surface !== "header" || !canManage) return;
    const onAction = (event: Event) => {
      if (
        (event as CustomEvent<{ actionId: MobileAssistantDirectAction }>).detail.actionId ===
        "renameSession"
      ) {
        setRenameOpen(true);
      }
    };
    window.addEventListener(MOBILE_ASSISTANT_ACTION_EVENT, onAction);
    return () => window.removeEventListener(MOBILE_ASSISTANT_ACTION_EVENT, onAction);
  }, [isMobile, surface, canManage]);

  useEffect(() => {
    if (previousConversationId.current === conversation.id) return;
    previousConversationId.current = conversation.id;
    setMenuOpen(false);
    setProjectPickerOpen(false);
    setRenameOpen(false);
    setRenameTitle(conversation.title ?? "");
    setDeleteOpen(false);
    setStopOpen(false);
    setWorktreeDetailsOpen(false);
    setDeleteBranch(false);
  }, [conversation.id, conversation.title]);

  useEffect(() => {
    if (!canAddAgent) return;
    const handleAddAgent = (event: globalThis.KeyboardEvent) => {
      if (
        event.defaultPrevented ||
        event.repeat ||
        event.isComposing ||
        !hasCommandModifier(event) ||
        !event.altKey ||
        event.shiftKey ||
        event.getModifierState("AltGraph") ||
        event.code !== "KeyA"
      ) {
        return;
      }
      const embedRoot = getEmbedRoot();
      if (isEmbedded && (!embedRoot || !event.composedPath().includes(embedRoot))) return;
      if (isFocusInDialogOrMenu() || document.activeElement?.closest(".xterm, .monaco-editor"))
        return;
      event.preventDefault();
      event.stopPropagation();
      navigate(addAgentPath);
    };
    window.addEventListener("keydown", handleAddAgent);
    return () => window.removeEventListener("keydown", handleAddAgent);
  }, [addAgentPath, canAddAgent, isEmbedded, navigate]);

  const closeMenu = () => {
    setMenuOpen(false);
    setProjectPickerOpen(false);
  };

  const handleProjectSelect = (project: string) => {
    closeMenu();
    moveToProject.mutate({ id: conversation.id, project });
  };

  const submitRename = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const title = renameTitle.trim();
    if (title && title !== (conversation.title ?? "")) {
      rename.mutate({ id: conversation.id, title });
    }
    setRenameOpen(false);
  };

  const confirmDelete = () => {
    setDeleteOpen(false);
    setDeleteBranch(false);
    if (location.pathname === `/c/${conversation.id}`) navigate("/", { replace: true });
    deleteConversation.mutate({
      id: conversation.id,
      deleteBranch: gitBranch !== null && deleteBranch,
    });
  };

  const exportConversation = async () => {
    try {
      const jsonl = await exportSessionTranscript(conversation.id);
      triggerBrowserDownload(
        new Blob([jsonl], { type: "application/jsonl" }),
        `${conversation.id}.jsonl`,
      );
    } catch {
      toast.error("Export failed");
    }
  };

  const copySessionId = async () => {
    try {
      await copyText(conversation.id);
      toast.success("Session ID copied");
    } catch {
      toast.error("Couldn't copy session ID");
    }
  };

  const archiveConversation = () => {
    // The local ref closes the same-tick gap before React Query's isPending
    // state reaches the menu, while the disabled state communicates progress
    // and blocks later duplicate selections.
    if (archiveRequestRef.current || archive.isPending) return;
    closeMenu();
    if (isArchived) {
      // Unarchiving keeps the user on the session — no redirect home and no
      // Undo toast (mirrors the sidebar row's Unarchive).
      archive.mutate({ id: conversation.id, archived: false });
      return;
    }
    archiveWorktreePrompt.requestArchive([conversation], (keepWorktreeIds, mode) => {
      void archiveNow(keepWorktreeIds.has(conversation.id), mode);
    });
  };

  const archiveNow = async (keepWorktree: boolean, worktreeArchiveMode?: ArchiveWorktreeMode) => {
    if (!mountedRef.current || currentConversationIdRef.current !== conversation.id) return;
    if (archiveRequestRef.current || archive.isPending) return;
    archiveRequestRef.current = true;
    setArchivePending(true);
    const requestedConversationId = conversation.id;
    const requestedLocation = location;

    try {
      await archive.mutateAsync({
        id: requestedConversationId,
        archived: true,
        keepWorktree,
        ...(worktreeArchiveMode ? { worktreeArchiveMode } : {}),
      });
    } catch {
      // useArchiveConversation owns the error toast and optimistic-cache
      // rollback. Staying on this route makes the restored session usable.
      return;
    } finally {
      archiveRequestRef.current = false;
      if (mountedRef.current) setArchivePending(false);
    }

    // The Undo toast survives this menu unmounting when an Agents row leaves
    // the active tree after its archive succeeds.
    showArchiveUndoToast(queryClient, [conversation], navigate);

    // A completed request must not pull the user away from a destination they
    // chose while it was in flight, nor from a newer active session rendered
    // into this same header instance.
    const activeLocation = currentLocationRef.current;
    if (
      !mountedRef.current ||
      currentConversationIdRef.current !== requestedConversationId ||
      activeLocation.pathname !== `/c/${requestedConversationId}` ||
      activeLocation.key !== requestedLocation.key ||
      activeLocation.pathname !== requestedLocation.pathname ||
      activeLocation.search !== requestedLocation.search ||
      activeLocation.hash !== requestedLocation.hash
    ) {
      return;
    }

    navigate("/", { replace: true });
  };

  const togglePin = () =>
    isPinned
      ? unpinWithUndo(queryClient, togglePinned.mutateAsync, conversation.id, conversation)
      : togglePinned.mutate({ id: conversation.id, pinned: true });
  // Desktop Session → Pin (⌥⌘P), Archive (⇧⌘A) and Fork (⌥⌘F) act on the session shown here.
  // Like the menu item, Pin waits out a pending pin write (from any row).
  useDesktopCommand("toggle-pin-session", pinSaving ? null : togglePin, { active: isPinned });
  useDesktopCommand("archive-session", archiveConversation, { active: isArchived });
  useDesktopCommand("fork-session", canFork && !forkDisabledReason ? onFork : null);
  const desktopShortcuts = supportsDesktopCommands();

  const mainItems = (
    <>
      {canAddAgent && (
        <>
          <DropdownMenuItem
            data-testid="add-sibling-session"
            className={itemClass}
            aria-keyshortcuts={`${ARIA_MOD_KEY}+Alt+A`}
            onSelect={() => navigate(addAgentPath)}
          >
            <PlusIcon className="size-3.5" />
            Add nested agent
          </DropdownMenuItem>
          <DropdownMenuSeparator />
        </>
      )}
      {/* Chat/Terminal renders its own trailing separator (null on other sessions). */}
      {viewItems}
      {!isChild && (
        <DropdownMenuItem
          data-testid="header-pin-conversation"
          className={itemClass}
          disabled={pinSaving}
          onSelect={togglePin}
        >
          {isPinned ? <PinOffIcon className="size-3.5" /> : <PinIcon className="size-3.5" />}
          {isPinned ? "Unpin" : "Pin"}
          {desktopShortcuts && (
            <CompactShortcutKeys keys={PIN_SESSION_KEYS} className="ml-auto pl-4" />
          )}
        </DropdownMenuItem>
      )}
      {canShare && (
        <DropdownMenuItem
          data-testid="header-share-conversation"
          className={itemClass}
          disabled={shareDisabled}
          title={shareDisabledReason}
          onSelect={
            shareDisabled
              ? undefined
              : () => {
                  trackMobile("chat.header.mobile_share");
                  onShare();
                }
          }
        >
          <ShareIcon className="size-3.5" />
          Share
        </DropdownMenuItem>
      )}
      {canFork && (
        <SessionActionMenuItem
          data-testid="header-fork-conversation"
          disabledReason={forkDisabledReason}
          className={itemClass}
          onSelect={onFork}
        >
          <GitForkIcon className="size-3.5" />
          Fork
          {desktopShortcuts && (
            <CompactShortcutKeys keys={FORK_SESSION_KEYS} className="ml-auto pl-4" />
          )}
        </SessionActionMenuItem>
      )}
      <DropdownMenuItem
        data-testid="header-export-conversation"
        className={itemClass}
        onSelect={() => void exportConversation()}
      >
        <DownloadIcon className="size-3.5" />
        Export
      </DropdownMenuItem>
      <DropdownMenuItem
        data-testid="header-view-instructions"
        className={itemClass}
        onSelect={() =>
          navigate(`/settings/instructions?session=${encodeURIComponent(conversation.id)}`)
        }
      >
        <FileTextIcon className="size-3.5" />
        View instructions
      </DropdownMenuItem>
      {showPeerMessages && onPeerMessages && (
        <DropdownMenuItem
          data-testid="header-peer-messages"
          className={itemClass}
          onSelect={onPeerMessages}
        >
          <MessageCircleIcon className="size-3.5" />
          Peer messages
        </DropdownMenuItem>
      )}
      {hasAgentInfo && onAgentInfo && (
        <DropdownMenuItem
          data-testid="header-agent-info"
          className={itemClass}
          onSelect={() => {
            trackMobile("chat.header.mobile_agent_info");
            onAgentInfo();
          }}
        >
          <InfoIcon className="size-3.5" />
          Agent info
        </DropdownMenuItem>
      )}
      <DropdownMenuItem
        data-testid="header-worktree-details"
        className={itemClass}
        onSelect={() => setWorktreeDetailsOpen(true)}
      >
        <InfoIcon className="size-3.5" /> Worktree details
      </DropdownMenuItem>
      {/* Rename is also reachable on desktop by clicking the breadcrumb title
          (HeaderTitle); on mobile the native shells hide the breadcrumb, so this
          menu is the sole entry point. */}
      <DropdownMenuItem
        data-testid="header-rename-conversation"
        className={itemClass}
        disabled={!canManage}
        title={canManage ? undefined : "Only the session owner can rename this session"}
        onSelect={() => setRenameOpen(true)}
      >
        <PencilIcon className="size-3.5" />
        Rename
      </DropdownMenuItem>
      {readState.explicitlyUnread || readState.unseen ? (
        <DropdownMenuItem
          data-testid="header-mark-read-conversation"
          className={itemClass}
          onSelect={() => markConversationRead(conversation.id, conversation.updated_at)}
        >
          <MailOpenIcon className="size-3.5" /> Mark as read
        </DropdownMenuItem>
      ) : (
        <DropdownMenuItem
          data-testid="header-mark-unread-conversation"
          className={itemClass}
          onSelect={() => markConversationUnread(conversation.id, conversation.updated_at)}
        >
          <MailIcon className="size-3.5" /> Mark as unread
        </DropdownMenuItem>
      )}
      {surface === "row" && (
        <>
          <DropdownMenuItem
            data-testid="header-copy-session-id"
            className={itemClass}
            onSelect={() => void copySessionId()}
          >
            <CopyIcon className="size-3.5" /> Copy session ID
          </DropdownMenuItem>
          <DropdownMenuItem
            data-testid="header-toggle-session-sounds"
            className={itemClass}
            onSelect={() => {
              const preferences = readSoundAlertPreferences();
              const mutedSessionIds = preferences.mutedSessionIds.includes(conversation.id)
                ? preferences.mutedSessionIds.filter((id) => id !== conversation.id)
                : [...preferences.mutedSessionIds, conversation.id];
              writeSoundAlertPreferences({ ...preferences, mutedSessionIds });
            }}
          >
            {soundsMuted ? <BellIcon className="size-3.5" /> : <BellOffIcon className="size-3.5" />}
            {soundsMuted ? "Unmute sounds" : "Mute sounds"}
          </DropdownMenuItem>
        </>
      )}
      {isChild && (
        <DropdownMenuItem
          data-testid="header-open-in-panel"
          className={itemClass}
          onSelect={() => {
            useChatStore.setState({
              sideChatToOpen: {
                childId: conversation.id,
                parentId: conversation.parent_session_id!,
              },
            });
            if (location.pathname !== `/c/${conversation.parent_session_id}`)
              navigate(`/c/${conversation.parent_session_id}`);
          }}
        >
          <PanelRightOpenIcon className="size-3.5" /> Open in panel
        </DropdownMenuItem>
      )}
      {/* Move to project is also reachable on desktop via the breadcrumb's
          folder tag (HeaderProjectTag); on mobile the native shells hide the
          breadcrumb, so this menu is the sole entry point. */}
      {!isChild &&
        !isIndentedSibling(conversation) &&
        (isMobile ? (
          // Mobile has no room for a side flyout, so this item swaps the menu body
          // to the project picker in place (see the `projectPickerOpen` branch).
          <DropdownMenuItem
            data-testid="header-move-to-project"
            className={cn("whitespace-nowrap", itemClass)}
            onSelect={(event) => {
              event.preventDefault();
              setProjectPickerOpen(true);
            }}
          >
            <FolderInputIcon className="size-3.5" />
            {currentProject ? "Move session" : "Add to project"}
          </DropdownMenuItem>
        ) : (
          <DropdownMenuSub>
            <DropdownMenuSubTrigger
              data-testid="header-move-to-project"
              className="whitespace-nowrap"
            >
              <FolderInputIcon className="size-3.5" />
              {currentProject ? "Move session" : "Add to project"}
            </DropdownMenuSubTrigger>
            {/* A native submenu flyout — no separate popover layer, so no
              open/dismiss race with the parent menu. */}
            <DropdownMenuSubContent className="min-w-56">
              <ProjectPicker currentProject={currentProject} onSelect={handleProjectSelect} />
            </DropdownMenuSubContent>
          </DropdownMenuSub>
        ))}
      {workspaceItems && (
        <>
          <DropdownMenuSeparator />
          {workspaceItems}
        </>
      )}
      <DropdownMenuSeparator />
      {canResume && (
        <DropdownMenuItem
          data-testid="header-resume-session"
          className={itemClass}
          disabled={!canManage || resume.isPending}
          title={canManage ? undefined : "Only the session owner can resume this session"}
          onSelect={() => resume.mutate()}
        >
          <PlayIcon className="size-3.5" /> {resume.isPending ? "Resuming…" : "Resume session"}
        </DropdownMenuItem>
      )}
      {surface === "row" && canStop && (
        <DropdownMenuItem
          data-testid="header-stop-session"
          className={itemClass}
          disabled={!canManage}
          title={canManage ? undefined : "Only the session owner can stop this session"}
          onSelect={() => setStopOpen(true)}
        >
          <CircleStopIcon className="size-3.5" /> Stop session
        </DropdownMenuItem>
      )}
      <DropdownMenuItem
        data-testid="header-archive-conversation"
        className={itemClass}
        disabled={!canManage || archivePending || archive.isPending}
        title={
          canManage ? undefined : "Only the session owner can archive or unarchive this session"
        }
        onSelect={archiveConversation}
      >
        {isArchived ? (
          <ArchiveRestoreIcon className="size-3.5" />
        ) : (
          <ArchiveIcon className="size-3.5" />
        )}
        {isArchived ? "Unarchive" : "Archive"}
        {desktopShortcuts && (
          <CompactShortcutKeys keys={ARCHIVE_SESSION_KEYS} className="ml-auto pl-4" />
        )}
      </DropdownMenuItem>
      <DropdownMenuItem
        data-testid="header-delete-conversation"
        className={itemClass}
        variant="destructive"
        disabled={!canManage}
        title={canManage ? undefined : "Only the session owner can delete this session"}
        onSelect={() => setDeleteOpen(true)}
      >
        <Trash2Icon className="size-3.5" />
        Delete this session
      </DropdownMenuItem>
    </>
  );

  return (
    <>
      <DropdownMenu
        open={menuOpen}
        // Radix's modal mode sets `pointer-events: none` on <body> while the
        // menu is open, leaving the menu as the only touch target on screen.
        // Browser touch-target adjustment then snaps outside taps near the
        // menu onto it, so on a phone the menu can't be dismissed. Non-modal
        // keeps the page interactive, so an outside tap lands on real content
        // and dismisses the menu.
        modal={!isMobile}
        onOpenChange={(open) => {
          setMenuOpen(open);
          if (!open) setProjectPickerOpen(false);
        }}
      >
        <DropdownMenuTrigger asChild>
          {trigger ?? (
            <MobileHeaderButton
              type="button"
              variant="ghost"
              size={isMobile ? "icon" : "icon-xs"}
              aria-label="Conversation actions"
              data-testid="header-conversation-actions"
              className="mobile-touch-target shrink-0 border-none text-muted-foreground hover:text-foreground max-md:size-11"
            >
              <EllipsisIcon className={isMobile ? "size-5" : "size-3.5"} />
            </MobileHeaderButton>
          )}
        </DropdownMenuTrigger>
        <DropdownMenuContent
          align={isMobile ? "end" : "start"}
          collisionPadding={isMobile ? 16 : undefined}
          className={cn(
            "min-w-56",
            MOBILE_GLASS_SURFACE,
            isMobile && "w-[calc(100vw-2rem)] max-w-[calc(100vw-2rem)]",
          )}
        >
          {isMobile && !projectPickerOpen && (
            <SessionMenuHeader title={label} workingDirectory={workingDirectory} />
          )}
          {projectPickerOpen ? (
            <>
              <DropdownMenuItem
                data-testid="header-project-picker-back"
                className={itemClass}
                onSelect={(event) => {
                  event.preventDefault();
                  setProjectPickerOpen(false);
                }}
              >
                <ChevronLeftIcon className="size-3.5" />
                Back
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <ProjectPicker currentProject={currentProject} onSelect={handleProjectSelect} />
            </>
          ) : (
            mainItems
          )}
        </DropdownMenuContent>
      </DropdownMenu>

      {archiveWorktreePrompt.dialog}
      {worktreeDetailsOpen && (
        <WorktreeDetailsDialog
          sessionId={conversation.id}
          open={worktreeDetailsOpen}
          onOpenChange={setWorktreeDetailsOpen}
        />
      )}
      <Dialog open={stopOpen} onOpenChange={setStopOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Stop session?</DialogTitle>
            <DialogDescription>
              This stops <span className="font-medium">{label}</span>. Its conversation and history
              are kept.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={() => setStopOpen(false)}>
              Cancel
            </Button>
            <Button
              type="button"
              variant="destructive"
              data-testid="header-stop-session-confirm"
              disabled={stopSession.isPending}
              onClick={() => {
                setStopOpen(false);
                stopSession.mutate(conversation.id, {
                  onError: () => toast.error("Couldn't stop the session"),
                });
              }}
            >
              Stop session
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <Dialog open={renameOpen} onOpenChange={setRenameOpen}>
        <DialogContent>
          <form onSubmit={submitRename}>
            <DialogHeader>
              <DialogTitle>Rename session</DialogTitle>
              <DialogDescription>Choose a short name that is easy to find later.</DialogDescription>
            </DialogHeader>
            <input
              autoFocus
              aria-label="Session name"
              data-testid="header-rename-conversation-input"
              maxLength={USER_SESSION_TITLE_MAX_CHARS}
              value={renameTitle}
              onChange={(event) => setRenameTitle(event.target.value)}
              onKeyDown={(event: KeyboardEvent<HTMLInputElement>) => {
                if (event.key === "Escape") setRenameOpen(false);
              }}
              className="mt-4 h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm outline-none focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/50"
            />
            <DialogFooter className="mt-4 border-t-0 bg-transparent mobile-app:flex-col-reverse mobile-app:gap-1">
              <Button
                type="button"
                variant="ghost"
                className="mobile-touch-target mobile-app:w-full"
                onClick={() => setRenameOpen(false)}
              >
                Cancel
              </Button>
              <Button
                type="submit"
                className="mobile-touch-target mobile-app:w-full"
                disabled={!renameTitle.trim() || rename.isPending}
              >
                Rename
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <Dialog
        open={deleteOpen}
        onOpenChange={(open) => {
          setDeleteOpen(open);
          if (!open) setDeleteBranch(false);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete conversation?</DialogTitle>
            <DialogDescription>
              <span className="font-medium break-all">{label}</span> and all of its history will be
              removed. This cannot be undone.
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
                  data-testid="header-delete-branch-checkbox"
                  checked={deleteBranch}
                  onChange={(event) => setDeleteBranch(event.target.checked)}
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
                      data-testid="header-delete-branch-worktree-path"
                      className="mt-0.5 block break-all font-mono text-xs text-muted-foreground"
                    >
                      {branchWorktree}
                    </span>
                  )}
                </span>
              </label>
            </div>
          )}
          <DialogFooter className="border-t-0 bg-transparent">
            <Button
              type="button"
              variant="ghost"
              onClick={() => setDeleteOpen(false)}
              disabled={deleteConversation.isPending}
            >
              Cancel
            </Button>
            <Button
              type="button"
              variant="destructive"
              onClick={confirmDelete}
              disabled={deleteConversation.isPending}
            >
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
