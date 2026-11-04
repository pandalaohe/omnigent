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
// Behavior by declared-terminal shape:
//   - One declared name → creates it directly on click.
//   - Native-wrapper session with several names → these are the host's
//     installed shells (zsh/bash/fish), declared with `$SHELL` first
//     (see `native_shell_terminal_spec`). Renders a SPLIT button:
//     clicking the primary opens the default (`declared[0]`, the user's
//     `$SHELL`), and a caret opens a picker of all installed shells.
//   - SDK agent with several names → distinct-purpose terminals with no
//     "default", so a plain dropdown to pick which one to launch.

import { ChevronDownIcon, PlusIcon } from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { useSessionAgent } from "@/hooks/useAgents";
import { useSessionTerminalAccess } from "@/hooks/useSessionTerminalAccess";
import { showToast } from "@/components/ui/toast";
import { terminalTabKey, useCreateTerminal } from "@/hooks/useTerminals";
import { useTerminalFirst } from "./TerminalFirstContext";

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
  const create = useCreateTerminal(conversationId);
  const { canCreateElevated } = useSessionTerminalAccess(conversationId);
  // Native-wrapper sessions declare the host's installed shells as their
  // terminals (default `$SHELL` first); SDK agents declare distinct-purpose
  // terminals with no default. A single declaration with admin support also
  // keeps a primary action while its caret offers the admin option.
  const isNativeWrapper = useTerminalFirst()?.isNativeWrapper ?? false;
  const declared = agent?.terminals ?? [];
  // The iff gate, UI side: no declared terminals → no affordance.
  if (declared.length === 0) return null;

  const launch = (name: string, elevated = false) => {
    create.mutate(elevated ? { terminal: name, elevated: true } : name, {
      onSuccess: (info) => onCreated?.(terminalTabKey(info)),
    });
  };

  const isShellPicker =
    (isNativeWrapper && declared.length > 1) || (declared.length === 1 && canCreateElevated);

  // Single declared name: create directly on click. Multiple: the
  // DropdownMenuTrigger wrapper below owns the click instead — except the
  // native shell-picker split button, whose primary segment launches the
  // default shell directly (`declared[0]`) and whose caret owns the dropdown.
  const onTriggerClick =
    declared.length === 1
      ? () => launch(declared[0])
      : isShellPicker
        ? () => launch(declared[0])
        : undefined;
  const trigger =
    variant === "row" ? (
      <button
        type="button"
        aria-label="New shell"
        disabled={create.isPending}
        className="flex w-full items-center gap-2 px-2 py-1.5 text-left text-muted-foreground hover:bg-accent/60 hover:text-foreground disabled:cursor-default disabled:opacity-50"
        onClick={onTriggerClick}
      >
        <PlusIcon className="size-3.5 shrink-0" />
        <span className="text-sm">
          {create.isError ? `Failed: ${create.error.message}` : "New shell"}
        </span>
      </button>
    ) : (
      <button
        type="button"
        aria-label="New shell"
        disabled={create.isPending}
        className="cursor-pointer rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground disabled:cursor-default disabled:opacity-50"
        onClick={onTriggerClick}
      >
        <PlusIcon className="size-3.5" />
      </button>
    );

  // The row variant carries its own label (and inline error text), so
  // the tooltip is icon-only chrome.
  const withTooltip = (child: React.ReactNode) =>
    variant === "row" ? (
      child
    ) : (
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>{child}</TooltipTrigger>
          <TooltipContent side="bottom">
            {create.isError ? `Failed: ${create.error.message}` : "New shell"}
          </TooltipContent>
        </Tooltip>
      </TooltipProvider>
    );

  if (declared.length === 1 && !canCreateElevated) return withTooltip(trigger);

  // Dropdown listing every declared terminal. In the shell-picker case the
  // first entry is the `$SHELL` default, so it is labeled as such.
  const menu = (
    <DropdownMenuContent align={variant === "row" ? "start" : "end"}>
      {declared.map((name, i) => (
        <DropdownMenuItem key={name} onSelect={() => launch(name)}>
          {isShellPicker && i === 0 ? `${name} (default)` : name}
        </DropdownMenuItem>
      ))}
      {canCreateElevated && (
        <DropdownMenuItem className="max-md:min-h-11" onSelect={() => launch(declared[0], true)}>
          Admin
        </DropdownMenuItem>
      )}
    </DropdownMenuContent>
  );

  // SDK agent with multiple distinct-purpose terminals: the whole control is
  // the dropdown trigger (no privileged default to click).
  if (!isShellPicker) {
    return (
      <DropdownMenu>
        {withTooltip(<DropdownMenuTrigger asChild>{trigger}</DropdownMenuTrigger>)}
        {menu}
      </DropdownMenu>
    );
  }

  // Native shell picker: split button. `trigger` (primary) launches the
  // default shell; the adjacent caret opens the installed-shell picker.
  const caret = (
    <DropdownMenuTrigger asChild>
      <button
        type="button"
        aria-label="Choose shell"
        disabled={create.isPending}
        className={
          variant === "row"
            ? "flex shrink-0 items-center px-2 py-1.5 text-muted-foreground hover:bg-accent/60 hover:text-foreground disabled:cursor-default disabled:opacity-50"
            : "cursor-pointer rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground disabled:cursor-default disabled:opacity-50"
        }
      >
        <ChevronDownIcon className="size-3.5" />
      </button>
    </DropdownMenuTrigger>
  );

  return (
    <DropdownMenu>
      <div className={variant === "row" ? "flex w-full items-center" : "flex items-center"}>
        {withTooltip(trigger)}
        {caret}
      </div>
      {menu}
    </DropdownMenu>
  );
}

/** Direct mobile header action; the drawer also retains its existing shell picker. */
export function NewAdminTerminalMenuItem({
  conversationId,
  onCreated,
}: {
  conversationId: string;
  onCreated?: (key: string) => void;
}) {
  const { data: agent } = useSessionAgent(conversationId);
  const { canCreateElevated } = useSessionTerminalAccess(conversationId);
  const create = useCreateTerminal(conversationId);
  const name = agent?.terminals?.[0];
  if (!canCreateElevated || !name) return null;
  return (
    <DropdownMenuItem
      className="min-h-11 gap-2.5 px-2.5 py-2 text-ui"
      disabled={create.isPending}
      onSelect={() => {
        // The menu unmounts on selection; keep completion attached to the promise.
        void create
          .mutateAsync({ terminal: name, elevated: true })
          .then((terminal) => onCreated?.(terminalTabKey(terminal)))
          .catch((error: Error) => showToast(`Couldn't open an admin terminal: ${error.message}`));
      }}
    >
      Admin{create.isError && <span role="alert">{create.error.message}</span>}
    </DropdownMenuItem>
  );
}
