import { useEffect, useRef, useState, type ReactNode } from "react";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import {
  SECTION_GLOSSES,
  SECTION_LABELS,
  SECTION_ORDER,
  type RegistryItem,
  type RegistryKey,
} from "@/hooks/useAgentRegistry";

export function AgentPicker({
  items,
  taken,
  supports,
  onPick,
  trigger,
  isLoading = false,
  isError = false,
  onRetry,
  title = "Add exception",
  description = "Choose an agent or harness for a different setting.",
}: {
  items: RegistryItem[];
  taken: ReadonlySet<RegistryKey>;
  supports?: (item: RegistryItem) => string | null;
  onPick: (key: RegistryKey) => void;
  trigger: ReactNode;
  isLoading?: boolean;
  isError?: boolean;
  onRetry?: () => void;
  title?: string;
  description?: string;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [showUndeployed, setShowUndeployed] = useState(false);
  const [showUnsupported, setShowUnsupported] = useState(false);
  const searchRef = useRef<HTMLInputElement>(null);
  const isMobile = useIsMobileViewport();
  useEffect(() => {
    if (open) searchRef.current?.focus();
    else {
      setQuery("");
      setShowUndeployed(false);
      setShowUnsupported(false);
    }
  }, [open]);

  const matches = items.filter((item) =>
    `${item.label} ${item.harness ?? ""} ${SECTION_LABELS[item.section]}`
      .toLowerCase()
      .includes(query.toLowerCase().trim()),
  );
  const hiddenCount = matches.filter((item) => !item.deployed).length;
  const visible = matches.filter((item) => showUndeployed || item.deployed);
  const unsupported = visible.filter((item) => supports?.(item));
  const supported = visible.filter((item) => !supports?.(item));
  const row = (item: RegistryItem, reason: string | null = null) => (
    <button
      key={item.key}
      type="button"
      disabled={taken.has(item.key) || !!reason}
      onClick={() => {
        onPick(item.key);
        setOpen(false);
      }}
      className="flex min-h-11 w-full items-center justify-between gap-3 rounded-lg px-3 py-2 text-left text-sm hover:bg-accent focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-ring disabled:cursor-not-allowed disabled:opacity-55"
    >
      <span className="min-w-0">
        <span className="block truncate font-medium">{item.label}</span>
        <span className="block text-xs text-muted-foreground">{reason ?? item.description}</span>
      </span>
      <span className="flex shrink-0 items-center gap-1 text-xs text-muted-foreground">
        {!item.deployed && <span>not deployed</span>}
        {taken.has(item.key) ? (
          <span>✓ already listed</span>
        ) : (
          <span>{SECTION_LABELS[item.section]}</span>
        )}
      </span>
    </button>
  );
  const content = (
    <>
      {isMobile ? (
        <DialogHeader className="border-b px-5 py-4">
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
      ) : (
        <div className="border-b px-4 py-3 text-sm font-medium">{title}</div>
      )}
      <div className="px-5 py-3">
        <Input
          ref={searchRef}
          aria-label="Search agents"
          placeholder="Search agents"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
      </div>
      <div className="min-h-0 overflow-y-auto px-2 pb-4" role="list" aria-label="Agent choices">
        {isLoading && <p className="px-3 py-3 text-sm text-muted-foreground">Loading agents…</p>}
        {isError && (
          <div className="flex items-center justify-between gap-3 px-3 py-3 text-sm">
            <span>Couldn't load agents</span>
            <Button type="button" variant="outline" size="sm" onClick={onRetry}>
              Retry
            </Button>
          </div>
        )}
        {SECTION_ORDER.map((section) => {
          const rows = supported.filter((item) => item.section === section);
          if (rows.length === 0) return null;
          return (
            <section key={section} aria-label={SECTION_LABELS[section]} className="px-1 py-2">
              <div className="sticky top-0 z-10 bg-popover px-3 pb-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                {SECTION_LABELS[section]}
              </div>
              {SECTION_GLOSSES[section] && (
                <p className="px-3 pb-2 text-xs text-muted-foreground">
                  {SECTION_GLOSSES[section]}
                </p>
              )}
              {rows.map((item) => row(item))}
            </section>
          );
        })}
        {unsupported.length > 0 && (
          <section className="border-t px-1 py-2">
            <button
              type="button"
              className="min-h-11 w-full px-3 text-left text-sm font-medium"
              aria-expanded={showUnsupported}
              onClick={() => setShowUnsupported(!showUnsupported)}
            >
              Not supported here ({unsupported.length})
            </button>
            {showUnsupported && unsupported.map((item) => row(item, supports?.(item) ?? null))}
          </section>
        )}
        {hiddenCount > 0 && !showUndeployed && (
          <button
            type="button"
            className="min-h-11 w-full border-t px-4 text-left text-sm text-muted-foreground hover:text-foreground"
            onClick={() => setShowUndeployed(true)}
          >
            Show {hiddenCount} not deployed
          </button>
        )}
        {!isLoading && !isError && visible.length === 0 && hiddenCount === 0 && (
          <p className="px-4 py-4 text-sm text-muted-foreground">No agents match your search.</p>
        )}
      </div>
    </>
  );
  return isMobile ? (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>{trigger}</DialogTrigger>
      <DialogContent className="top-auto bottom-0 left-0 max-h-[min(640px,90dvh)] w-full max-w-full translate-x-0 translate-y-0 gap-0 overflow-hidden rounded-b-none p-0">
        {content}
      </DialogContent>
    </Dialog>
  ) : (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>{trigger}</PopoverTrigger>
      <PopoverContent
        align="start"
        sideOffset={6}
        role="dialog"
        aria-label={title}
        className="max-h-[min(640px,80dvh)] w-[min(560px,calc(100vw-2rem))] gap-0 overflow-hidden p-0"
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          searchRef.current?.focus();
        }}
      >
        {content}
      </PopoverContent>
    </Popover>
  );
}
