import { isFramePath } from "@/lib/boardFrame";
import { Link } from "@/lib/routing";
import { lazy, Suspense, useState } from "react";
import { useProjects } from "@/hooks/useConversations";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Spinner } from "@/components/ui/spinner";

const Composer = lazy(() =>
  import("@/shell/NewChatDialog").then((module) => ({ default: module.NewChatLandingScreen })),
);

interface SessionAction {
  kind: "new-session";
  label: string;
  project?: string;
  prompt: string;
}

export function taskActions(value: unknown): SessionAction[] {
  if (!Array.isArray(value)) return [];
  return value.slice(0, 8).filter((entry): entry is SessionAction => {
    if (!entry || typeof entry !== "object") return false;
    return (
      entry.kind === "new-session" &&
      typeof entry.label === "string" &&
      entry.label.trim().length > 0 &&
      [...entry.label].length <= 64 &&
      typeof entry.prompt === "string" &&
      [...entry.prompt].length <= 2000 &&
      (entry.project === undefined ||
        (typeof entry.project === "string" && [...entry.project].length <= 128))
    );
  });
}

export function TaskBoardActions({ actions }: { actions: SessionAction[] }) {
  const [creating, setCreating] = useState(false);
  const [draft, setDraft] = useState<{ label: string; project: string; prompt: string } | null>(
    null,
  );
  const { data: projects, isLoading } = useProjects({
    enabled: !isFramePath() && (actions.length > 0 || draft !== null),
  });
  if (isFramePath())
    return actions.length ? (
      <Link to="/" target="_blank" rel="noopener">
        Open in Omnigent
      </Link>
    ) : null;
  return (
    <>
      {actions.map((action, index) => (
        <Button
          key={JSON.stringify([action, index])}
          variant="outline"
          size="sm"
          className="h-auto whitespace-normal break-words text-left"
          disabled={isLoading}
          onClick={() => {
            const project =
              projects?.find((candidate) => candidate.id === action.project) ??
              projects?.find((candidate) => candidate.name === action.project);
            setDraft({ label: action.label, project: project?.name ?? "", prompt: action.prompt });
          }}
        >
          {action.label}
        </Button>
      ))}
      <Dialog open={draft !== null} onOpenChange={(open) => !open && !creating && setDraft(null)}>
        <DialogContent
          className="max-h-[90dvh] overflow-y-auto sm:max-w-4xl"
          aria-describedby={undefined}
        >
          <DialogHeader>
            <DialogTitle>{draft?.label}</DialogTitle>
          </DialogHeader>
          {draft && (
            <Suspense fallback={<Spinner className="mx-auto size-5" />}>
              <Composer seed={draft} onCreatingChange={setCreating} />
            </Suspense>
          )}
        </DialogContent>
      </Dialog>
    </>
  );
}
