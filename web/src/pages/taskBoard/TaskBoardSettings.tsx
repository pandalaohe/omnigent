import { SettingRow } from "@/components/SettingsFields";
import { Switch } from "@/components/ui/switch";
import { useSessionNavigationPreferences } from "@/hooks/useSessionNavigationPreferences";
import { writeSessionNavigationPreferences } from "@/lib/sessionNavigationPreferences";
import { t } from "./taskBoardText";

export function TaskBoardSettings() {
  const preferences = useSessionNavigationPreferences();
  return (
    <SettingRow label={t("settings.show")} hint={t("settings.hint")}>
      <Switch
        aria-label={t("settings.show")}
        checked={preferences.showTaskBoard}
        onCheckedChange={(showTaskBoard) =>
          writeSessionNavigationPreferences({ ...preferences, showTaskBoard })
        }
      />
    </SettingRow>
  );
}
