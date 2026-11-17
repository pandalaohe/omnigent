// "+ New shell" affordance shared by the shell surfaces
// (InlineTerminalsSection, TerminalsPanel, and MainTerminalView's
// strip on native-wrapper sessions).
//
// Gated on the agent's terminal access: the button renders ONLY when
// the session agent's spec declares a non-empty `terminals:` block
// (read via `useSessionAgent().terminals` — the same gate the server
// enforces on POST /resources/terminals and that controls the agent's
// own sys_terminal_* tool surface). An agent without terminal access
// never sees the button. Admin terminals require separate per-instance
// consent before the agent can read or type into them.
//
// The menu opens the default session shell; Options selects host, shell,
// elevation and initial agent access together.

import { PlusIcon } from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useSessionAgent } from "@/hooks/useAgents";
import { ShellMenuEntry } from "./ShellMenuEntry";

interface NewTerminalButtonProps {
  conversationId: string;
  /**
   * Called with the created terminal's tab key (e.g.
   * `"terminal:terminal_shell_u-abc123"`) so the host surface can
   * focus the new terminal. Optional — surfaces without an active-key
   * concept omit it.
   */
  onCreated?: (terminalKey: string) => void;
  /**
   * Visual shape. `"icon"` (default) is the compact tooltip-wrapped
   * plus used in headers and tab strips; `"row"` renders as a
   * full-width list row (plus icon + "New shell" label) so the
   * affordance sits at the end of the Shells list like a virtual
   * entry rather than floating in empty space.
   */
  variant?: "icon" | "row";
}

export function NewTerminalButton({
  conversationId,
  onCreated,
  variant = "icon",
}: NewTerminalButtonProps) {
  const { data: agent } = useSessionAgent(conversationId);
  if (!agent?.terminals?.length) return null;
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          aria-label="New shell"
          className="flex items-center gap-2 rounded p-1 text-muted-foreground hover:bg-muted"
        >
          <PlusIcon className="size-3.5" />
          {variant === "row" && "New shell"}
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <ShellMenuEntry conversationId={conversationId} onCreated={onCreated} />
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
