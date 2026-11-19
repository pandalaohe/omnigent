import {
  Archive,
  ArrowBigUp,
  ArrowDown,
  ArrowDownToLine,
  ArrowLeft,
  ArrowRight,
  ArrowRightToLine,
  ArrowUp,
  ArrowUpToLine,
  AtSign,
  Bell,
  Bookmark,
  Bot,
  Brain,
  Bug,
  Check,
  ChevronUp,
  ChevronsDown,
  ChevronsUp,
  ClipboardPaste,
  Clock,
  Cloud,
  Code,
  Coffee,
  Command,
  Copy,
  CornerDownLeft,
  Cpu,
  Delete,
  Eye,
  File,
  Flag,
  Folder,
  GitBranch,
  GitCommitHorizontal,
  GitMerge,
  GitPullRequest,
  Globe,
  Hash,
  Heart,
  Keyboard,
  KeyboardOff,
  List,
  ListEnd,
  Lock,
  LockOpen,
  Option,
  Play,
  Redo2,
  RefreshCw,
  Rocket,
  RotateCcw,
  RotateCw,
  Save,
  Search,
  Server,
  Space,
  Sparkles,
  Square,
  SquareTerminal,
  Star,
  TextCursorInput,
  Undo2,
  X,
  Zap,
} from "lucide-react";
import { ClaudeIcon } from "@/components/icons/ClaudeIcon";
import { CodexIcon } from "@/components/icons/CodexIcon";
import { CursorIcon } from "@/components/icons/CursorIcon";
import { KimiIcon } from "@/components/icons/KimiIcon";
import { OpenCodeIcon } from "@/components/icons/OpenCodeIcon";
import { PiIcon } from "@/components/icons/PiIcon";
import { cn } from "@/lib/utils";

export const TERMINAL_KEY_ICON_PAGES = [
  {
    "arrow-up": ArrowUp,
    "arrow-down": ArrowDown,
    "arrow-left": ArrowLeft,
    "arrow-right": ArrowRight,
    enter: CornerDownLeft,
    tab: ArrowRightToLine,
    escape: X,
    delete: Delete,
    "page-up": ChevronsUp,
    "page-down": ChevronsDown,
    home: ArrowUpToLine,
    end: ArrowDownToLine,
    space: Space,
    command: Command,
    option: Option,
    shift: ArrowBigUp,
    control: ChevronUp,
    keyboard: Keyboard,
    "keyboard-off": KeyboardOff,
    "square-terminal": SquareTerminal,
    "rotate-ccw": RotateCcw,
    "rotate-cw": RotateCw,
    "list-end": ListEnd,
    "text-cursor-input": TextCursorInput,
  },
  {
    terminal: SquareTerminal,
    "git-branch": GitBranch,
    "git-commit-horizontal": GitCommitHorizontal,
    "git-merge": GitMerge,
    "git-pull-request": GitPullRequest,
    code: Code,
    bug: Bug,
    play: Play,
    stop: Square,
    refresh: RefreshCw,
    search: Search,
    folder: Folder,
    file: File,
    save: Save,
    undo: Undo2,
    redo: Redo2,
    copy: Copy,
    "clipboard-paste": ClipboardPaste,
    "agent-claude": ClaudeIcon,
    "agent-codex": CodexIcon,
    "agent-opencode": OpenCodeIcon,
    "agent-pi": PiIcon,
    "agent-kimi": KimiIcon,
    "agent-cursor": CursorIcon,
  },
  {
    star: Star,
    heart: Heart,
    flag: Flag,
    bookmark: Bookmark,
    zap: Zap,
    rocket: Rocket,
    sparkles: Sparkles,
    bot: Bot,
    brain: Brain,
    coffee: Coffee,
    cpu: Cpu,
    server: Server,
    cloud: Cloud,
    globe: Globe,
    lock: Lock,
    "lock-open": LockOpen,
    eye: Eye,
    bell: Bell,
    clock: Clock,
    list: List,
    check: Check,
    hash: Hash,
    "at-sign": AtSign,
    archive: Archive,
  },
] as const;

const icons = Object.assign({ backspace: Delete }, ...TERMINAL_KEY_ICON_PAGES);

export function TerminalKeyContent({
  button,
  className = "size-3.5 shrink-0",
}: {
  button: { display?: "text" | "icon"; icon?: string; label: string };
  className?: string;
}) {
  const Icon =
    button.icon && Object.hasOwn(icons, button.icon)
      ? icons[button.icon as keyof typeof icons]
      : undefined;
  if (button.display === "icon" && Icon) return <Icon className={className} aria-hidden />;
  return (
    <span
      className={cn("max-w-full", Array.from(button.label).length === 1 ? "shrink-0" : "truncate")}
    >
      {button.label}
    </span>
  );
}
