import { useEffect, useRef } from "react";
import { useLocation, useNavigate } from "@/lib/routing";
import { supportsBrowser } from "@/lib/nativeBridge";
import { SettingsGroup } from "@/components/SettingsGroup";
import { BrowserPlacesSettings } from "@/components/BrowserPane/BrowserPlacesSettings";
import { BrowserExtensionsControl } from "@/components/BrowserExtensionsControl";
import { BrowserAccessControl } from "@/components/BrowserAccessControl";
import { BrowserLoginControl, OpenLinksInAppControl } from "./legacyControls";

const tabs = [
  ["history", "History"],
  ["bookmarks", "Bookmarks"],
  ["signin", "Sign-in & site data"],
  ["extensions", "Extensions"],
  ["access", "Access"],
] as const;
export function BrowserSection() {
  const location = useLocation(),
    navigate = useNavigate(),
    activeRef = useRef<HTMLButtonElement>(null);
  const requested = new URLSearchParams(location.search).get("tab");
  const active = tabs.some(([id]) => id === requested) ? requested : "history";
  useEffect(() => {
    activeRef.current?.scrollIntoView?.({ block: "nearest", inline: "nearest" });
  }, [active]);
  if (!supportsBrowser()) return <p>Browser settings are in the desktop app</p>;
  function select(tab: string) {
    const params = new URLSearchParams(location.search);
    params.set("tab", tab);
    navigate(`${location.pathname}?${params}${location.hash}`);
  }
  return (
    <div className="space-y-6">
      <div
        role="tablist"
        aria-label="Browser settings"
        className="flex w-fit max-w-full gap-1 overflow-x-auto rounded-lg bg-muted p-1"
      >
        {tabs.map(([id, label], index) => (
          <button
            type="button"
            key={id}
            ref={id === active ? activeRef : undefined}
            role="tab"
            aria-selected={active === id}
            aria-controls={`browser-${id}`}
            id={`tab-${id}`}
            tabIndex={id === active ? 0 : -1}
            className={`shrink-0 rounded-md px-3 py-2 text-sm focus-visible:ring-2 focus-visible:ring-ring ${active === id ? "bg-background font-medium shadow-xs" : "text-muted-foreground"}`}
            onClick={() => select(id)}
            onKeyDown={(event) => {
              const n =
                event.key === "ArrowRight"
                  ? (index + 1) % 5
                  : event.key === "ArrowLeft"
                    ? (index + 4) % 5
                    : event.key === "Home"
                      ? 0
                      : event.key === "End"
                        ? 4
                        : -1;
              if (n >= 0) {
                event.preventDefault();
                select(tabs[n][0]);
                document.getElementById(`tab-${tabs[n][0]}`)?.focus();
              }
            }}
          >
            {label}
          </button>
        ))}
      </div>
      <div
        role="tabpanel"
        id={`browser-${active}`}
        aria-labelledby={`tab-${active}`}
        className="space-y-6"
      >
        {(active === "history" || active === "bookmarks") && (
          <SettingsGroup title={active === "history" ? "History" : "Bookmarks"}>
            <BrowserPlacesSettings />
          </SettingsGroup>
        )}
        {active === "signin" && (
          <SettingsGroup title="Stay signed in">
            <BrowserLoginControl />
          </SettingsGroup>
        )}
        {active === "extensions" && (
          <SettingsGroup title="Extensions">
            <BrowserExtensionsControl />
          </SettingsGroup>
        )}
        {active === "access" && (
          <>
            <SettingsGroup title="Links">
              <OpenLinksInAppControl />
            </SettingsGroup>
            <SettingsGroup title="Internal addresses">
              <BrowserAccessControl />
            </SettingsGroup>
          </>
        )}
      </div>
    </div>
  );
}
