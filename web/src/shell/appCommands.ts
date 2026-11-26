// The app-wide commands the ⌘K palette lists in its Actions group, and how a
// typed query filters them.

import { useMemo } from "react";
import {
  CalendarClockIcon,
  InboxIcon,
  type LucideIcon,
  PanelLeftIcon,
  PanelRightIcon,
  SettingsIcon,
  SquarePenIcon,
} from "lucide-react";

import {
  newSessionShortcutKeys,
  rightSidebarToggleShortcutKeys,
  settingsShortcutKeys,
  sidebarToggleShortcutKeys,
} from "@/components/KeyboardShortcut";
import type { ProjectSummary } from "@/hooks/useConversations";
import type { UseNewSessionTargetResult } from "@/hooks/useNewSessionTarget";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { useLocation, useNavigate } from "@/lib/routing";
import { useSettingsRoute } from "@/shell/settingsNav";
import { settingsSearchCommand } from "@/shell/settingsSearch/settingsSearchCommand";

export interface ActionCommand {
  id: string;
  label: string;
  /** Mirrors the icon on the equivalent button elsewhere in the UI. */
  icon: LucideIcon;
  /** Extra terms the client-side filter matches against (beyond the label). */
  keywords: string[];
  run: () => void;
  /** Chord shown beside the row when the same action has a shortcut. */
  shortcut?: readonly string[];
}

export interface AppCommandOptions {
  /** Flip the left (Conversations) sidebar, owned by AppShell. */
  onToggleLeftSidebar: () => void;
  /** Flip the right (Workspace) sidebar, owned by AppShell. */
  onToggleRightSidebar: () => void;
  /** False on grid routes, where ⌘⌥] belongs to the grid's own workspace. */
  rightSidebarShortcutEnabled: boolean;
  newSession?: Pick<UseNewSessionTargetResult, "target" | "route">;
  projects?: readonly ProjectSummary[];
}

/** Commands available everywhere, in the order the palette lists them. */
export function useAppCommands({
  onToggleLeftSidebar,
  onToggleRightSidebar,
  rightSidebarShortcutEnabled,
  newSession,
  projects,
}: AppCommandOptions): ActionCommand[] {
  const navigate = useNavigate();
  const target = newSession?.target;
  const targetRoute = newSession?.route ?? "/";
  const { inSettings } = useSettingsRoute();
  const { pathname, search } = useLocation();
  const mobile = useIsMobileViewport();
  return useMemo<ActionCommand[]>(
    () => [
      {
        id: "new-chat",
        label:
          target?.kind === "project"
            ? `New session in ${target.projectName}`
            : target
              ? "New session — No Project"
              : "New chat",
        icon: SquarePenIcon,
        keywords: ["compose", "start", "new session"],
        run: () => navigate(targetRoute),
        shortcut: newSessionShortcutKeys(),
      },
      ...(projects ?? [])
        .filter(
          (project) =>
            target?.kind !== "project" ||
            (project.id !== target.projectId && project.name !== target.projectName),
        )
        .map((project) => ({
          id: `new-session-project:${project.id ?? project.name}`,
          label: `New session in ${project.name}`,
          icon: SquarePenIcon,
          keywords: ["compose", "start", "new session", "project"],
          run: () => navigate(`/?project=${encodeURIComponent(project.name)}`),
        })),
      {
        id: "go-inbox",
        label: "Go to Inbox",
        icon: InboxIcon,
        keywords: ["notifications", "comments", "needs response"],
        run: () => navigate("/inbox"),
      },
      {
        id: "go-tasks",
        label: "Go to Automations",
        icon: CalendarClockIcon,
        keywords: ["scheduled", "recurring", "cron", "automation", "schedule"],
        run: () => navigate("/tasks"),
      },
      {
        id: "go-settings",
        label: "Go to Settings",
        icon: SettingsIcon,
        keywords: ["preferences", "configuration", "account"],
        run: () => navigate("/settings"),
        shortcut: settingsShortcutKeys(),
      },
      settingsSearchCommand({
        settingsLocation: inSettings ? { pathname, search } : null,
        mobile,
        navigate,
      }),
      {
        id: "toggle-left-sidebar",
        label: "Toggle conversations sidebar",
        icon: PanelLeftIcon,
        keywords: ["panel", "left", "sessions list"],
        run: onToggleLeftSidebar,
        shortcut: sidebarToggleShortcutKeys(),
      },
      {
        id: "toggle-right-sidebar",
        label: "Toggle workspace sidebar",
        icon: PanelRightIcon,
        keywords: ["panel", "right", "files", "terminal"],
        run: onToggleRightSidebar,
        shortcut: rightSidebarShortcutEnabled ? rightSidebarToggleShortcutKeys() : undefined,
      },
    ],
    [
      navigate,
      pathname,
      search,
      inSettings,
      mobile,
      onToggleLeftSidebar,
      onToggleRightSidebar,
      projects,
      rightSidebarShortcutEnabled,
      target,
      targetRoute,
    ],
  );
}

/** The commands whose label or a keyword contains `query`, ignoring case; all of them for a blank query. */
export function filterAppCommands(
  commands: readonly ActionCommand[],
  query: string,
): readonly ActionCommand[] {
  const q = query.trim().toLowerCase();
  if (q === "") return commands;
  return commands.filter(
    (command) =>
      command.label.toLowerCase().includes(q) ||
      command.keywords.some((keyword) => keyword.toLowerCase().includes(q)),
  );
}
