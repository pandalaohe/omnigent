import { useSyncExternalStore } from "react";
import { NumericField, SettingRow } from "@/components/SettingsFields";
import { Button } from "@/components/ui/button";
import {
  CHAT_RESERVE_DEFAULT_PX,
  CHAT_RESERVE_MIN_PX,
  CHAT_RESERVE_MAX_PX,
  readInlinePanelChatReserveWidth,
  subscribePanelSizePreferences,
  writePanelSizePreference,
} from "@/lib/panelSizePreferences";

export function ChatReserveWidthSetting() {
  const value = useSyncExternalStore(
    subscribePanelSizePreferences,
    readInlinePanelChatReserveWidth,
    () => CHAT_RESERVE_DEFAULT_PX,
  );
  return (
    <div className="space-y-2">
      <SettingRow
        label="Chat reserve width"
        hint="Space left for chat when a web page opens on this device. The panel yields in a smaller window. Manual resizing keeps its current limits."
      >
        <NumericField
          value={value}
          min={CHAT_RESERVE_MIN_PX}
          max={CHAT_RESERVE_MAX_PX}
          ariaLabel="Chat reserve width"
          disabled={false}
          onCommit={(width) => writePanelSizePreference("inlinePanelChatReserveWidthPx", width)}
        />
        <span className="text-xs text-muted-foreground">px</span>
        <Button
          variant="ghost"
          size="sm"
          disabled={value === CHAT_RESERVE_DEFAULT_PX}
          onClick={() => writePanelSizePreference("inlinePanelChatReserveWidthPx", null)}
        >
          Reset
        </Button>
      </SettingRow>
      <p className="text-xs text-muted-foreground">
        Web pages widen toward their reading width, leaving {value} px for chat; files split the
        current window.
      </p>
    </div>
  );
}
