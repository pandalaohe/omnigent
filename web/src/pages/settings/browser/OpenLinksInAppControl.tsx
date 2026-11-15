import { useState, useId, useCallback } from "react";
import { Switch } from "@/components/ui/switch";
import { SettingsLabel } from "@/components/SettingsLabel";
import { readOpenLinksInApp, writeOpenLinksInApp } from "@/lib/linkOpenPreferences";
import { MOD_KEY } from "@/components/KeyboardShortcut";
/**
 * Where a plain click on a web link in chat content opens — desktop shells
 * with the embedded browser only. Off keeps the current behavior (the
 * default external browser); on routes the link into the conversation's
 * in-app browser tabs. Shift-click always stays external.
 */
export function OpenLinksInAppControl() {
  const [enabled, setEnabled] = useState(readOpenLinksInApp);
  const labelId = useId();
  const descriptionId = useId();
  const toggle = useCallback((next: boolean) => {
    setEnabled(next);
    writeOpenLinksInApp(next);
  }, []);

  return (
    <div className="flex items-start justify-between gap-6">
      <SettingsLabel
        label="Open links in the in-app browser"
        note="Keep web links beside the chat."
        labelId={labelId}
        descriptionId={descriptionId}
        className="flex-1"
        description={`Open chat links in-app, reusing matching tabs. ${MOD_KEY}+click also opens in-app; Shift+click opens your default browser.`}
      />
      <Switch
        aria-labelledby={labelId}
        aria-describedby={descriptionId}
        checked={enabled}
        onCheckedChange={toggle}
        data-testid="open-links-in-app-toggle"
        className="mt-0.5 shrink-0"
        componentId="settings.general.open_links_in_app"
      />
    </div>
  );
}
