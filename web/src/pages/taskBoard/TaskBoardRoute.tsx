import { boardExitProps } from "@/lib/boardFrame";
import { Link } from "@/lib/routing";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { taskBoardAvailable } from "@/lib/capabilities";
import { useSessionNavigationPreferences } from "@/hooks/useSessionNavigationPreferences";
import { PageScroll } from "@/components/PageScroll";
import { Spinner } from "@/components/ui/spinner";
import { TaskBoardPage } from "./TaskBoardPage";
import { t } from "./taskBoardText";
import "./taskBoard.css";

export { TaskProgressIcon } from "./TaskProgressIcon";
function TaskBoardContent() {
  const info = useServerInfo();
  const { showTaskBoard } = useSessionNavigationPreferences();
  if (info === "loading")
    return <Spinner aria-label={t("board.loading")} className="m-auto size-5" />;
  if (!taskBoardAvailable(info))
    return <h1 className="p-6 text-lg font-semibold">{t("board.serverOff")}</h1>;
  if (!showTaskBoard)
    return (
      <div className="space-y-3 p-6">
        <h1 className="text-lg font-semibold">{t("board.userOff")}</h1>
        <Link
          {...boardExitProps()}
          to="/settings/general#task-board-settings"
          className="text-sm underline underline-offset-2"
        >
          {t("board.enable")}
        </Link>
      </div>
    );
  return <TaskBoardPage />;
}
export function TaskBoardRoute() {
  return (
    <PageScroll maxWidthClassName="max-w-none" contentClassName="px-3 sm:px-6">
      <TaskBoardContent />
    </PageScroll>
  );
}
