import {
  forwardRef,
  useEffect,
  useId,
  useRef,
  useState,
  type ComponentPropsWithoutRef,
  type ReactNode,
} from "react";
import {
  BanIcon,
  ChevronDownIcon,
  EyeIcon,
  FilePenIcon,
  FolderIcon,
  GitForkIcon,
  HandIcon,
  LaptopIcon,
  Loader2Icon,
  MonitorCloudIcon,
  ShieldOffIcon,
  SparklesIcon,
  ZapIcon,
  ZapOffIcon,
  type LucideIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { MobileHeaderButton } from "@/shell/MobileHeaderButton";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { LevelGlyph } from "@/components/ui/level-meter";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { useMenuGuardedTooltip } from "@/hooks/useMenuGuardedTooltip";
import { cn } from "@/lib/utils";
import { permissionModeConcept, type PermissionModeConcept } from "@/lib/nativeHarnessModes";
import type { CodexPermissionReadback } from "@/lib/codexApprovalMode";
import {
  COMPOSER_COLLAPSED_LABEL_CLASS,
  COMPOSER_CONTENT_INSET_CLASS,
  COMPOSER_TRAY_INSET_CLASS,
  COMPOSER_WORKSPACE_COLLAPSED_LABEL_CLASS,
  useCollapsedWorkspaceLabels,
} from "./ChatComposer";

const NO_DISABLED_OPTIONS: readonly string[] = [];

// Consumer-facing harness names whose permission vocabulary is identical to a
// native one. Presentation-only aliases so the shared native table stays native.
const PERMISSION_HARNESS_ALIASES: Record<string, string> = {
  "claude-sdk": "claude-native",
  codex: "codex-native",
};

/** One glyph per shared permission concept; `default` is the neutral fallback. */
const PERMISSION_CONCEPT_ICONS: Record<PermissionModeConcept, LucideIcon> = {
  manual: HandIcon,
  automatic: SparklesIcon,
  "edit-automatic": FilePenIcon,
  "read-only": EyeIcon,
  deny: BanIcon,
  "full-access": ShieldOffIcon,
  default: HandIcon,
};

function permissionConceptFor(
  harness: string | null | undefined,
  value: string | null | undefined,
): PermissionModeConcept {
  return permissionModeConcept(
    harness ? (PERMISSION_HARNESS_ALIASES[harness] ?? harness) : harness,
    value,
  );
}

/**
 * The workspace tray docked above the composer card. A tray nests inside the
 * card's outer edges by the shared inset rather than sharing its border box
 * (see the layout contract in ChatComposer); its chips sit on the card's
 * content inset line via the shared content inset; a chip's box starts there, and its
 * own px-1 keeps the glyph off the edge.
 */
export function ComposerWorkspaceBar({
  className,
  children,
  trailingContent,
  ...props
}: ComponentPropsWithoutRef<"div"> & { trailingContent?: ReactNode }) {
  const barRef = useRef<HTMLDivElement>(null);
  useCollapsedWorkspaceLabels(barRef);
  return (
    <div
      ref={barRef}
      className={cn(
        "composer-context-bar group/composer-workspace relative z-0 -mb-px flex h-7 mobile-app:h-auto mobile-app:flex-wrap min-w-0 items-center gap-0.5 rounded-t-2xl border border-b-0 border-border py-0.5 md:h-[37px] md:gap-2 md:py-1.5",
        COMPOSER_CONTENT_INSET_CLASS,
        COMPOSER_TRAY_INSET_CLASS,
        className,
      )}
      {...props}
    >
      {children}
      {trailingContent}
    </div>
  );
}

/**
 * Tooltip text only reaches assistive tech while the tooltip is open, so text
 * that adds to the accessible name also gets a persistent hidden description.
 */
export function useTooltipDescription(name: string | undefined, details: ReactNode) {
  const id = useId();
  if (typeof details !== "string" || details === name) {
    return { describedBy: undefined, description: null };
  }
  return {
    describedBy: id,
    description: (
      <span id={id} className="sr-only">
        {details}
      </span>
    ),
  };
}

export const ComposerWorkspaceTrigger = forwardRef<
  HTMLButtonElement,
  ComponentPropsWithoutRef<"button"> & {
    kind: "directory" | "worktree";
    label: string;
    icon?: ReactNode;
    /** Hide the visible label; it stays the accessible name. */
    iconOnly?: boolean;
    /** Hover details, e.g. the full path; hidden while its menu is open. */
    tooltip: ReactNode;
    menuOpen: boolean;
  }
>(function ComposerWorkspaceTrigger(
  { kind, label, icon, iconOnly = false, tooltip, menuOpen, className, ...props },
  ref,
) {
  const Icon = kind === "directory" ? FolderIcon : GitForkIcon;
  const { describedBy, description } = useTooltipDescription(props["aria-label"] ?? label, tooltip);
  return (
    <ComposerMenuTooltip
      menuOpen={menuOpen}
      content={tooltip}
      // The wrapper is the bar's flex item, so it carries the width cap.
      className="max-w-[calc(50%-0.25rem)]"
      contentClassName="max-w-[min(36rem,calc(100vw-2rem))] break-all max-md:hidden"
    >
      <button
        ref={ref}
        type="button"
        // Icon-only while the bar is collapsed, so the label is the name.
        aria-label={label}
        aria-describedby={describedBy}
        className={cn(
          "relative inline-flex h-6 min-w-10 mobile-touch-target mobile-app:h-auto max-w-full cursor-pointer items-center gap-1 rounded-md border border-transparent bg-transparent px-1 text-xs leading-4 font-normal text-muted-foreground transition-colors hover:bg-muted hover:text-foreground disabled:cursor-default disabled:opacity-50 md:min-w-11",
          className,
        )}
        {...props}
      >
        {icon ?? <Icon className="size-3.5 shrink-0" />}
        {!iconOnly && (
          <span
            data-workspace-collapse-label=""
            className={cn("min-w-0 truncate text-left", COMPOSER_WORKSPACE_COLLAPSED_LABEL_CLASS)}
          >
            {label}
          </span>
        )}
        <ChevronDownIcon className="size-3 shrink-0 opacity-60" />
      </button>
      {description}
    </ComposerMenuTooltip>
  );
});

export type HostStatusDotStatus = "online" | "offline" | "unknown" | "starting";

const HOST_RING_CLASS = "border-[1.5px] border-muted-foreground";

const HOST_STATUS_DOT_CLASS: Record<HostStatusDotStatus, string> = {
  online: "bg-success",
  offline: HOST_RING_CLASS,
  unknown: HOST_RING_CLASS,
  // The ring's gap keeps "starting" distinct when motion is reduced.
  starting: `${HOST_RING_CLASS} border-t-transparent animate-spin motion-reduce:animate-none`,
};

/**
 * Host liveness dot shared by the composer's host chip and its host menu, so
 * the two agree: a ring while not running, a spinning ring while starting,
 * and a filled green dot once online.
 */
export function HostStatusDot({
  status,
  testId,
}: {
  status: HostStatusDotStatus;
  testId?: string;
}) {
  return (
    <span
      aria-hidden
      data-status={status}
      className={cn("size-2 shrink-0 rounded-full", HOST_STATUS_DOT_CLASS[status])}
      data-testid={testId}
    />
  );
}

export const ComposerHostTrigger = forwardRef<
  HTMLButtonElement,
  ComponentPropsWithoutRef<"button"> & {
    label: string;
    status?: HostStatusDotStatus;
    cloud?: boolean;
    appearance?: "composer" | "header";
    testIdPrefix?: string;
  }
>(function ComposerHostTrigger(
  {
    label,
    status,
    cloud = false,
    appearance = "composer",
    testIdPrefix = "composer",
    className,
    ...props
  },
  ref,
) {
  const Icon = cloud ? MonitorCloudIcon : LaptopIcon;
  const Trigger = appearance === "header" ? MobileHeaderButton : "button";
  return (
    <Trigger
      {...(appearance === "header" ? { glass: false } : {})}
      ref={ref}
      type="button"
      aria-label={label}
      className={cn(
        "flex h-8 mobile-touch-target w-11 shrink-0 cursor-pointer items-center justify-center gap-0.5 rounded-lg bg-transparent pl-1 pr-2 text-muted-foreground transition-colors hover:bg-muted/70 hover:text-foreground data-[state=open]:bg-muted data-[state=open]:text-foreground dark:hover:bg-muted/50 disabled:cursor-default md:h-7",
        appearance === "header" &&
          "max-md:data-[state=open]:bg-transparent max-md:focus:bg-transparent max-md:focus-visible:ring-0",
        className,
      )}
      {...props}
    >
      <span className="flex items-center">
        {status && (
          <span className="flex size-4 shrink-0 items-center justify-center">
            <HostStatusDot status={status} testId={`${testIdPrefix}-host-status`} />
          </span>
        )}
        <Icon
          className={cn("size-4 shrink-0", appearance === "header" && "size-5")}
          data-testid={`${testIdPrefix}-host-icon`}
        />
      </span>
    </Trigger>
  );
});

export function ComposerPermissionPicker({
  label,
  value,
  harness,
  selectedValue,
  options,
  disabledOptions = NO_DISABLED_OPTIONS,
  disabled = false,
  disabledReason,
  iconOnly = false,
  loading = false,
  interactiveWhileLoading = false,
  onSelect,
  readback,
  testIdPrefix = "composer",
  openNonce = 0,
}: {
  label: string;
  value: string;
  harness?: string | null;
  selectedValue?: string | null;
  options: readonly { value: string; label: string; concept?: string; description?: string }[];
  disabledOptions?: readonly string[];
  disabled?: boolean;
  /** Hover text explaining why the picker is disabled. */
  disabledReason?: string;
  /** Hide the visible value; it stays in the accessible name. */
  iconOnly?: boolean;
  loading?: boolean;
  interactiveWhileLoading?: boolean;
  onSelect: (value: string) => void;
  readback?: CodexPermissionReadback;
  testIdPrefix?: string;
  /** Opens the menu each time this changes to a new nonzero value. */
  openNonce?: number;
}) {
  const writeDisabled = disabled || (loading && !interactiveWhileLoading);
  const optionConcept = (mode: string | null | undefined): PermissionModeConcept => {
    if (harness === "acp" || harness?.startsWith("acp:")) {
      const concept = options.find((option) => option.value === mode)?.concept;
      return concept && concept in PERMISSION_CONCEPT_ICONS
        ? (concept as PermissionModeConcept)
        : "default";
    }
    return permissionConceptFor(harness, mode);
  };
  const selectedConcept = optionConcept(selectedValue);
  const SelectedConceptIcon = PERMISSION_CONCEPT_ICONS[selectedConcept];
  const inert = !readback && writeDisabled;
  const [open, setOpen] = useState(false);
  const appliedOpenNonce = useRef(openNonce);
  useEffect(() => {
    if (!openNonce || openNonce === appliedOpenNonce.current) return;
    appliedOpenNonce.current = openNonce;
    if (!inert) setOpen(true);
  }, [openNonce, inert]);
  return (
    <DropdownMenu open={open} onOpenChange={setOpen}>
      <DropdownMenuTrigger asChild disabled={inert}>
        <button
          type="button"
          disabled={inert}
          aria-busy={loading || undefined}
          className={cn(
            "flex h-8 mobile-touch-target mobile-app:h-auto min-w-0 w-auto cursor-pointer items-center justify-center gap-1 rounded-lg bg-transparent px-2 text-foreground transition-colors hover:bg-muted/70 dark:hover:bg-muted/50 disabled:cursor-default disabled:opacity-50 md:h-7",
            loading && "disabled:opacity-100",
          )}
          aria-label={`${label}: ${value}`}
          title={disabled && disabledReason ? disabledReason : `${label}: ${value}`}
          data-testid={`${testIdPrefix}-permission-chip`}
          data-composer-action="permission"
          data-mobile-icon-button
          data-permission-concept={selectedConcept}
        >
          <SelectedConceptIcon
            aria-hidden="true"
            className={cn(
              "size-3 shrink-0",
              selectedConcept === "full-access" ? "text-destructive" : "text-muted-foreground",
            )}
          />
          {!iconOnly && (
            <span
              data-composer-permission-label
              className={cn(
                "min-w-0 truncate text-ui font-normal max-md:hidden",
                COMPOSER_COLLAPSED_LABEL_CLASS,
              )}
            >
              {value}
            </span>
          )}
          <ChevronDownIcon className="size-4 shrink-0 opacity-60" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="start"
        className="w-max min-w-[13.75rem] max-w-[calc(100vw-2rem)]"
        data-testid={`${testIdPrefix}-permission-menu`}
        onCloseAutoFocus={(event) => {
          // A closing animation must not steal focus from a newly opened menu.
          if (document.activeElement?.closest('[role="menu"][data-state="open"]')) {
            event.preventDefault();
          }
        }}
      >
        {readback && (
          <div
            className="max-w-sm space-y-2 border-b border-border px-2 py-2 text-xs whitespace-normal break-words"
            data-testid={`${testIdPrefix}-permission-readback`}
          >
            <div className="font-medium">{readback.heading}</div>
            <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1">
              {readback.rows.map((row) => (
                <div key={row.label} className="contents">
                  <dt className="text-muted-foreground">{row.label}</dt>
                  <dd className="min-w-0 break-words">{row.value}</dd>
                </div>
              ))}
            </dl>
            <p className="text-muted-foreground">{readback.note}</p>
            {writeDisabled && (
              <p className="text-muted-foreground">
                {disabledReason ?? "Permission changes are unavailable in this session state"}
              </p>
            )}
          </div>
        )}
        <div className="px-2 py-1 text-xs text-muted-foreground">
          {readback ? "Change permission mode" : label}
        </div>
        <DropdownMenuRadioGroup
          value={selectedValue ?? undefined}
          onValueChange={(next) => {
            if (!writeDisabled) onSelect(next);
          }}
        >
          {options.map((option) => {
            const concept = optionConcept(option.value);
            const ConceptIcon = PERMISSION_CONCEPT_ICONS[concept];
            return (
              <DropdownMenuRadioItem
                key={option.value}
                value={option.value}
                data-testid={`${testIdPrefix}-permission-option-${option.value}`}
                data-permission-concept={concept}
                disabled={writeDisabled || disabledOptions.includes(option.value)}
                title={
                  disabledOptions.includes(option.value)
                    ? `Start a new ${option.label} session to use this mode`
                    : option.description
                }
                className="whitespace-normal break-words"
              >
                {/* The shared menu row paints every svg muted; ! keeps the
                    full-access glyph danger-coloured despite that rule. */}
                <ConceptIcon
                  aria-hidden="true"
                  className={concept === "full-access" ? "text-destructive!" : undefined}
                />
                {option.label}
                {disabledOptions.includes(option.value) && (
                  <span className="ml-1 text-xs text-muted-foreground">
                    {harness === "antigravity-native"
                      ? "Needs a new session"
                      : "Start a new session"}
                  </span>
                )}
              </DropdownMenuRadioItem>
            );
          })}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/** Two Standard/Fast choices toggle directly; other tiers use an icon-triggered menu. */
export function ComposerSpeedPicker({
  value,
  options,
  onSelect,
  disabled = false,
  testIdPrefix = "composer",
}: {
  value: string;
  options: readonly { value: string; label: string }[];
  onSelect: (value: string) => void;
  disabled?: boolean;
  testIdPrefix?: string;
}) {
  if (!options.some((option) => option.value !== "standard")) return null;
  const toggle =
    options.length === 2 &&
    options.some((option) => option.value === "standard") &&
    options.some((option) => option.value === "fast");
  const label = options.find((option) => option.value === value)?.label ?? value;
  const active = value !== "standard";
  const Icon = active ? ZapIcon : ZapOffIcon;
  const trigger = (
    <button
      type="button"
      disabled={disabled}
      aria-label={`Speed: ${label}`}
      aria-pressed={toggle ? active : undefined}
      title={toggle ? `Speed: ${label} — use ${active ? "Standard" : "Fast"}` : `Speed: ${label}`}
      data-testid={`${testIdPrefix}-speed-trigger`}
      onClick={toggle ? () => onSelect(active ? "standard" : "fast") : undefined}
      className={cn(
        "flex size-8 shrink-0 cursor-pointer items-center justify-center rounded-lg hover:bg-muted/70 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring disabled:cursor-default disabled:opacity-50 md:size-7",
        active ? "text-primary" : "text-muted-foreground",
      )}
    >
      <Icon className="size-4" aria-hidden="true" />
    </button>
  );
  if (toggle) return trigger;
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild disabled={disabled}>
        {trigger}
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="start"
        className="max-w-[calc(100vw-2rem)]"
        data-testid={`${testIdPrefix}-speed-menu`}
      >
        <DropdownMenuRadioGroup value={value} onValueChange={onSelect}>
          {options.map((option) => (
            <DropdownMenuRadioItem
              key={option.value}
              value={option.value}
              className="mobile-touch-target whitespace-normal break-words"
              data-testid={`${testIdPrefix}-speed-option-${option.value}`}
            >
              {option.label}
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/**
 * Label/value rows for a composer trigger's config tooltip. The bold key
 * separates each row's label from its value.
 */
export function ComposerConfigTooltipRows({
  rows,
}: {
  rows: readonly { label: string; value: string }[];
}) {
  return (
    <>
      {rows.map((row) => (
        <span key={row.label}>
          <span className="font-semibold">{row.label}:</span> {row.value}
        </span>
      ))}
    </>
  );
}

/**
 * Hover tooltip for a composer control that opens a menu. It stays hidden
 * while the menu is open and doesn't reopen as focus returns on close.
 */
export function ComposerMenuTooltip({
  menuOpen,
  content,
  variant = "default",
  testId,
  className,
  contentClassName,
  children,
}: {
  menuOpen: boolean;
  content: ReactNode;
  variant?: "default" | "session-info";
  testId?: string;
  /** Classes for the wrapper that receives the hover. */
  className?: string;
  /** Classes for the tooltip surface. */
  contentClassName?: string;
  children: ReactNode;
}) {
  const guardedTooltip = useMenuGuardedTooltip(menuOpen);
  return (
    <TooltipProvider>
      <Tooltip open={guardedTooltip.open} onOpenChange={guardedTooltip.onOpenChange}>
        <TooltipTrigger asChild>
          <span className={cn("flex min-w-0", className)} {...guardedTooltip.triggerProps}>
            {children}
          </span>
        </TooltipTrigger>
        <TooltipContent
          side="top"
          className={cn(
            "max-w-80 flex-col items-start gap-0.5 px-3 py-2",
            variant === "session-info" &&
              "w-64 max-w-[calc(100vw-2rem)] items-stretch rounded-lg bg-popover p-2.5 text-popover-foreground whitespace-normal shadow-menu ring-1 ring-foreground/10",
            contentClassName,
          )}
          data-testid={testId}
        >
          {content}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

export const ComposerHarnessTrigger = forwardRef<
  HTMLButtonElement,
  Omit<ComponentPropsWithoutRef<typeof Button>, "children"> & {
    label: string;
    model: string;
    loading?: boolean;
    pending?: boolean;
    /** Yellow dot warning, e.g. a member session with an unavailable member. */
    alert?: boolean;
    effort?: string;
    /** Current rung and ladder size; stays visible when the text collapses. */
    effortLevel?: { level: number; total: number; title?: string };
    icon?: ReactNode;
    testIdPrefix?: string;
    labelClassName?: string;
    appearance?: "composer" | "header";
  }
>(function ComposerHarnessTrigger(
  {
    label,
    model,
    effort,
    effortLevel,
    icon,
    loading = false,
    pending = false,
    alert = false,
    testIdPrefix = "composer",
    labelClassName,
    appearance = "composer",
    className,
    ...props
  },
  ref,
) {
  const Trigger = appearance === "header" ? MobileHeaderButton : Button;
  return (
    <Trigger
      {...(appearance === "header" ? { glass: false } : {})}
      ref={ref}
      type="button"
      variant="ghost"
      size="sm"
      aria-label={label}
      aria-busy={loading || undefined}
      className={cn(
        "h-auto min-h-8 mobile-touch-target min-w-0 w-auto max-w-full shrink gap-1 rounded-lg border-0 px-2 py-0 text-ui leading-5 font-normal text-muted-foreground hover:text-foreground md:min-h-7",
        className,
      )}
      {...props}
    >
      {icon}
      {loading && (
        <Loader2Icon
          role="status"
          className="size-3 shrink-0 animate-spin"
          aria-label="Loading model"
          data-testid={`${testIdPrefix}-model-loading`}
        />
      )}
      {pending && !loading && (
        <Loader2Icon
          className="size-3 shrink-0 animate-spin"
          aria-label="Model change pending"
          data-testid={`${testIdPrefix}-model-pending`}
        />
      )}
      {/* Without an icon the text is the only identification, so it stays put
          when the row collapses. */}
      <span
        className={cn(
          "inline-flex min-w-0 flex-nowrap items-baseline gap-1",
          icon != null && `max-md:hidden ${COMPOSER_COLLAPSED_LABEL_CLASS}`,
          labelClassName,
          appearance === "header" && "sr-only",
        )}
        data-testid={`${testIdPrefix}-agent-config-value`}
      >
        {model && (
          <span
            className="min-w-0 truncate text-left text-ui leading-5 font-medium text-foreground"
            title={model}
            data-testid={`${testIdPrefix}-agent-model-value`}
          >
            {model}
          </span>
        )}
        {effort && (
          <span
            className="shrink-0 text-ui leading-5 font-normal text-muted-foreground"
            data-testid={`${testIdPrefix}-agent-effort-value`}
          >
            {effort}
          </span>
        )}
      </span>
      {effortLevel && effortLevel.total > 0 && (
        <span className="shrink-0" data-testid={`${testIdPrefix}-agent-effort-glyph`}>
          <LevelGlyph
            level={effortLevel.level}
            total={effortLevel.total}
            title={effortLevel.title ?? (effort ? `Effort: ${effort}` : "Effort: Default")}
          />
        </span>
      )}
      {alert && (
        <span
          role="status"
          aria-label="A member is unavailable"
          data-testid={`${testIdPrefix}-members-alert`}
          className="size-1.5 shrink-0 rounded-full bg-warning"
        />
      )}
      {appearance !== "header" && <ChevronDownIcon className="size-4 shrink-0 opacity-60" />}
    </Trigger>
  );
});
