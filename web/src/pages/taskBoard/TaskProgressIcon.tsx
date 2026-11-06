import {
  BanIcon,
  CheckIcon,
  HandIcon,
  ListTodoIcon,
  PlayIcon,
  TriangleAlertIcon,
} from "lucide-react";
import type { TaskProgress } from "./taskBoardData";
import { t } from "./taskBoardText";

const icons = {
  "needs-action": [ListTodoIcon, "text-muted-foreground"],
  "in-process": [PlayIcon, "text-blue-500"],
  completed: [CheckIcon, "text-emerald-600"],
  failed: [TriangleAlertIcon, "text-destructive"],
  cancelled: [BanIcon, "text-muted-foreground"],
} as const;
export function TaskProgressIcon({
  progress = "needs-action",
  waiting,
}: {
  progress?: TaskProgress;
  waiting?: boolean;
}) {
  const [Icon, color] = waiting ? [HandIcon, "text-amber-600"] : icons[progress];
  return (
    <Icon
      className={`size-4 shrink-0 ${color}`}
      role="img"
      aria-label={t(waiting ? "waiting.title" : `progress.${progress}`)}
    />
  );
}
