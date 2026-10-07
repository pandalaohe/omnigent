import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useKeyboardShortcutsVersion } from "@/components/KeyboardShortcut";
import { Command, CommandGroup, CommandItem, CommandList } from "@/components/ui/command";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import type { Conversation } from "@/hooks/useConversations";
import {
  eventMatchesShortcut,
  hasCustomShortcutBindings,
  isShortcutActionEnabled,
  isShortcutRecordingActive,
  resolveShortcutBindings,
  shortcutAriaKeys,
  shortcutBindingLabels,
  type ShortcutChord,
} from "@/lib/keyboardShortcutPreferences";
import {
  cancelBrowserRecentSessionSwitch,
  onBrowserRecentSessionInput,
  setBrowserRecentSessionSwitchSupported,
} from "@/lib/nativeBridge";
import { useNavigate } from "@/lib/routing";

import { conversationDisplayLabel, getConversationAgentType } from "./sidebarNav";

const RECENT_SESSION_LIMIT = 5;

interface RecentSession {
  id: string;
  label: string;
  agent: string;
}

function recentSessions(conversations: readonly Conversation[] | undefined): RecentSession[] {
  const seen = new Set<string>();
  return [...(conversations ?? [])]
    .filter((conversation) => !conversation.archived && conversation.provisional !== true)
    .sort((first, second) => second.updated_at - first.updated_at)
    .filter((conversation) => {
      if (seen.has(conversation.id)) return false;
      seen.add(conversation.id);
      return true;
    })
    .slice(0, RECENT_SESSION_LIMIT)
    .map((conversation) => ({
      id: conversation.id,
      label: conversationDisplayLabel(conversation),
      agent: getConversationAgentType(conversation),
    }));
}

export function RecentSessionsSwitcher({
  conversations,
  activeSessionId,
  enabled,
}: {
  conversations: readonly Conversation[] | undefined;
  activeSessionId: string | null;
  enabled: boolean;
}) {
  const navigate = useNavigate();
  const shortcutsVersion = useKeyboardShortcutsVersion();
  const available = useMemo(() => recentSessions(conversations), [conversations]);
  const availableRef = useRef(available);
  const activeSessionIdRef = useRef(activeSessionId);
  const itemsRef = useRef<RecentSession[]>([]);
  const selectedIndexRef = useRef(0);
  const openRef = useRef(false);
  // The chord that opened the switcher, so release commits against the modifier
  // the user is actually holding.
  const activeBindingRef = useRef<ShortcutChord | null>(null);
  const [items, setItems] = useState<RecentSession[]>([]);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [open, setOpen] = useState(false);

  availableRef.current = available;
  activeSessionIdRef.current = activeSessionId;

  const setSelection = useCallback((index: number) => {
    selectedIndexRef.current = index;
    setSelectedIndex(index);
  }, []);

  const close = useCallback(() => {
    openRef.current = false;
    setOpen(false);
  }, []);

  const cancel = useCallback(() => {
    close();
    itemsRef.current = [];
    setItems([]);
  }, [close]);

  const commit = useCallback(
    (index = selectedIndexRef.current) => {
      const destination = itemsRef.current[index]?.id;
      cancel();
      if (destination && destination !== activeSessionIdRef.current) {
        navigate(`/c/${destination}`);
      }
    },
    [cancel, navigate],
  );

  useEffect(() => {
    if (!enabled) {
      cancel();
      return;
    }

    // Shift reverses the cycle, so it is stripped before matching a custom
    // chord; the default Ctrl+Tab test already allows it.
    const matchTrigger = (event: globalThis.KeyboardEvent): ShortcutChord | null => {
      if (event.getModifierState?.("AltGraph")) return null;
      if (isShortcutRecordingActive() || !isShortcutActionEnabled("recentSessions")) return null;
      if (!hasCustomShortcutBindings("recentSessions")) {
        if (event.key === "Tab" && event.ctrlKey && !event.altKey && !event.metaKey) {
          return { code: "Tab", modifiers: ["control"] };
        }
        return null;
      }
      return (
        resolveShortcutBindings("recentSessions").find((binding) =>
          eventMatchesShortcut(
            {
              code: event.code,
              key: event.key,
              ctrlKey: event.ctrlKey,
              metaKey: event.metaKey,
              altKey: event.altKey,
              shiftKey: false,
            },
            binding,
          ),
        ) ?? null
      );
    };

    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape" && openRef.current) {
        event.preventDefault();
        event.stopPropagation();
        cancel();
        return;
      }
      const binding = matchTrigger(event);
      if (!binding) return;

      const direction = event.shiftKey ? -1 : 1;
      if (!openRef.current) {
        const nextItems = availableRef.current;
        if (nextItems.length === 0) return;
        event.preventDefault();
        event.stopPropagation();
        itemsRef.current = nextItems;
        setItems(nextItems);
        const activeIndex = nextItems.findIndex(
          (session) => session.id === activeSessionIdRef.current,
        );
        const initialIndex =
          activeIndex === -1
            ? direction === 1
              ? 0
              : nextItems.length - 1
            : (activeIndex + direction + nextItems.length) % nextItems.length;
        setSelection(initialIndex);
        activeBindingRef.current = binding;
        openRef.current = true;
        setOpen(true);
        return;
      }

      event.preventDefault();
      event.stopPropagation();
      const count = itemsRef.current.length;
      if (count === 0) return;
      setSelection((selectedIndexRef.current + direction + count) % count);
    };

    const onKeyUp = (event: globalThis.KeyboardEvent) => {
      if (!openRef.current) return;
      const binding = activeBindingRef.current;
      if (!binding) return;
      const heldModifiers = shortcutAriaKeys(binding).split("+").slice(0, -1);
      if (heldModifiers.includes(event.key)) commit();
    };

    const onBlur = () => {
      if (openRef.current) cancel();
    };
    const unsubscribeBrowserInput = onBrowserRecentSessionInput((input) => {
      window.dispatchEvent(
        new KeyboardEvent(input.type, {
          key: input.key,
          code: input.code,
          ctrlKey: input.ctrlKey,
          shiftKey: input.shiftKey,
          altKey: input.altKey,
          metaKey: input.metaKey,
          repeat: input.repeat,
          bubbles: true,
          cancelable: true,
        }),
      );
      if (input.type === "keydown" && input.key === "Tab" && !openRef.current) {
        void cancelBrowserRecentSessionSwitch();
      }
    });
    // Intercept Ctrl+Tab only while the default gesture is live: a rebound or
    // disabled switcher would otherwise swallow it inside embedded Browser pages.
    void setBrowserRecentSessionSwitchSupported(
      enabled &&
        isShortcutActionEnabled("recentSessions") &&
        !hasCustomShortcutBindings("recentSessions"),
    );

    window.addEventListener("keydown", onKeyDown, true);
    window.addEventListener("keyup", onKeyUp, true);
    window.addEventListener("blur", onBlur);
    return () => {
      window.removeEventListener("keydown", onKeyDown, true);
      window.removeEventListener("keyup", onKeyUp, true);
      window.removeEventListener("blur", onBlur);
      unsubscribeBrowserInput();
      void setBrowserRecentSessionSwitchSupported(false);
    };
  }, [cancel, commit, enabled, setSelection, shortcutsVersion]);

  const selectedId = items[selectedIndex]?.id ?? "";
  const recentBinding = activeBindingRef.current ?? resolveShortcutBindings("recentSessions")[0];
  const recentLabels = recentBinding ? shortcutBindingLabels(recentBinding) : [];
  const cycleKey = recentLabels[recentLabels.length - 1] ?? "Tab";
  const releaseModifier = recentLabels.slice(0, -1).join(" ") || "Ctrl";

  return (
    <Dialog open={open} onOpenChange={(nextOpen) => !nextOpen && cancel()}>
      <DialogContent
        aria-describedby={undefined}
        className="top-1/4 translate-y-0 overflow-hidden rounded-xl p-0 sm:max-w-lg"
        showCloseButton={false}
      >
        <DialogTitle className="sr-only">Recent sessions</DialogTitle>
        <Command
          value={selectedId}
          onValueChange={(id) => {
            const index = items.findIndex((session) => session.id === id);
            if (index !== -1) setSelection(index);
          }}
          shouldFilter={false}
          vimBindings={false}
          loop
          label="Recent sessions"
        >
          <CommandList>
            <CommandGroup heading="Recent sessions">
              {items.map((session, index) => (
                <CommandItem
                  key={session.id}
                  value={session.id}
                  onSelect={() => commit(index)}
                  className="items-center pl-6"
                >
                  <span className="min-w-0 flex-1 truncate text-left">{session.label}</span>
                  <span className="ml-2 shrink-0 text-sm text-muted-foreground">
                    {session.agent}
                  </span>
                </CommandItem>
              ))}
            </CommandGroup>
          </CommandList>
          <div className="border-t border-border px-3 py-2 text-xs text-muted-foreground">
            Press {cycleKey} to cycle · Release {releaseModifier} to switch · Esc to cancel
          </div>
        </Command>
      </DialogContent>
    </Dialog>
  );
}
