import { SettingRow } from "@/components/SettingsFields";
import { Switch } from "@/components/ui/switch";
import { useSessionNavigationPreferences } from "@/hooks/useSessionNavigationPreferences";
import { writeSessionNavigationPreferences } from "@/lib/sessionNavigationPreferences";
import { t } from "./taskBoardText";
import { useBoardPreferences } from "./taskBoardPreferences";
import { useTaskBoardData } from "./taskBoardData";
import { TaskBoardSources } from "./TaskBoardSources";
import "./taskBoard.css";

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

export function TaskBoardSourceSettings() {
  const data = useTaskBoardData();
  const [preferences, update] = useBoardPreferences();
  return (
    <>
      {data.isError && (
        <p role="alert">
          {t("board.loadFailed")}{" "}
          <button type="button" onClick={() => void data.refetch()}>
            {t("board.retry")}
          </button>
        </p>
      )}
      <TaskBoardSources
        sources={data.data?.sources ?? []}
        preferences={preferences}
        update={update}
      />
    </>
  );
}
