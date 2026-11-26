import { useState, useCallback } from "react";
import { Switch } from "@/components/ui/switch";
import { readOpenLinksInApp, writeOpenLinksInApp } from "@/lib/linkOpenPreferences";
import { SettingsEntryLabel, SettingsItem } from "../search/SettingsItem";
import { OPEN_LINKS_IN_APP_ENTRY } from "../search/catalog/browser";
import { settingsControlProps } from "../search/settingsControl";
/**
 * Where a plain click on a web link in chat content opens — desktop shells
 * with the embedded browser only. Off keeps the current behavior (the
 * default external browser); on routes the link into the conversation's
 * in-app browser tabs. Shift-click always stays external.
 */
export function OpenLinksInAppControl() {
  const [enabled, setEnabled] = useState(readOpenLinksInApp);
  const toggle = useCallback((next: boolean) => {
    setEnabled(next);
    writeOpenLinksInApp(next);
  }, []);

  return (
    <SettingsItem
      entry={OPEN_LINKS_IN_APP_ENTRY}
      className="flex items-start justify-between gap-6"
    >
      <SettingsEntryLabel
        entry={OPEN_LINKS_IN_APP_ENTRY}
        className="flex-1"
        note="Keep web links beside the chat."
      />
      <Switch
        {...settingsControlProps(OPEN_LINKS_IN_APP_ENTRY)}
        checked={enabled}
        onCheckedChange={toggle}
        data-testid="open-links-in-app-toggle"
        className="mt-0.5 shrink-0"
        componentId="settings.general.open_links_in_app"
      />
    </SettingsItem>
  );
}
